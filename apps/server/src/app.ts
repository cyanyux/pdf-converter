import { randomUUID } from "node:crypto";
import { existsSync, readdirSync, statSync } from "node:fs";
import { open, readFile, rm, stat, unlink } from "node:fs/promises";
import { extname, join } from "node:path";
import { Readable } from "node:stream";
import type { HttpBindings } from "@hono/node-server";
import {
  type CreateJobsResponse,
  Engine,
  isTerminal,
  Locale,
  Mode,
  type HealthResponse,
} from "@pdf-converter/shared";
import { zip } from "fflate";
import { Hono } from "hono";
import { streamSSE } from "hono/streaming";
import { rateLimiter } from "hono-rate-limiter";
import { z } from "zod";
import { apiKeyAuth } from "./auth.ts";
import { config } from "./config.ts";
import type { JobStore } from "./db.ts";
import { buildOpenApiDoc, SWAGGER_HTML } from "./openapi.ts";
import { preflightPdf } from "./preflight.ts";
import { contentDisposition, safeResolve, sanitizeDownloadName } from "./safe-resolve.ts";
import type { HubSnapshot, ProgressHub } from "./sse.ts";
import type { UploadedFile } from "./upload.ts";
import { parseMultipart } from "./upload.ts";

type Env = { Bindings: HttpBindings };

async function cleanup(files: UploadedFile[]): Promise<void> {
  await Promise.all(files.map((f) => unlink(f.path).catch(() => {})));
}

async function fileResponse(
  path: string,
  downloadName: string,
  mime: string,
  method: string,
): Promise<Response> {
  // Open the file first, then take content-length from the open handle's fstat and
  // stream from that same handle. On Linux an open fd survives an unlink, so a
  // retention-GC rmtree between now and stream consumption can't make the body
  // shorter than the promised content-length (the earlier statSync-then-stream form
  // could). The stream owns the handle (autoClose) so the fd is released on end,
  // error, or client abort — no leak. ENOENT here → the caller's 404.
  //
  // HEAD never opens a stream: @hono/node-server discards the body of a HEAD
  // response WITHOUT draining it, so autoClose never fires and the fd would leak
  // until GC (uptime monitors / link-preview bots probe with HEAD). Headers-only.
  if (method === "HEAD") {
    const st = await stat(path); // ENOENT → the caller's 404, same as open()
    return new Response(null, {
      headers: {
        "content-type": mime,
        "content-length": String(st.size),
        "content-disposition": contentDisposition(downloadName),
        "cache-control": "no-store",
      },
    });
  }
  const handle = await open(path, "r");
  let web: ReadableStream<Uint8Array>;
  let size: number;
  try {
    size = (await handle.stat()).size;
    web = Readable.toWeb(
      handle.createReadStream({ autoClose: true }),
    ) as unknown as ReadableStream<Uint8Array>;
  } catch (e) {
    await handle.close().catch(() => {});
    throw e;
  }
  return new Response(web, {
    headers: {
      "content-type": mime,
      "content-length": String(size),
      "content-disposition": contentDisposition(downloadName),
      "cache-control": "no-store",
    },
  });
}

export function createApp(store: JobStore, hub: ProgressHub): Hono<Env> {
  const app = new Hono<Env>();

  app.use("/api/*", apiKeyAuth(["/api/v1/health"]));
  // The OpenAPI doc + Swagger UI describe the whole (authenticated) API, so gate
  // them behind the same key. apiKeyAuth is a no-op when API_KEY is unset, so
  // they stay open for local dev / discovery.
  app.use("/openapi.json", apiKeyAuth());
  app.use("/docs", apiKeyAuth());
  app.use(
    "/api/v1/jobs",
    rateLimiter({
      windowMs: 60_000,
      limit: 60,
      standardHeaders: "draft-7",
      keyGenerator: (c) => c.req.header("x-api-key") ?? c.req.header("authorization") ?? "anon",
      // Return the JSON {error} shape every other error on this endpoint uses (and that
      // openapi.ts documents for 429), not the library's default text/plain body.
      handler: (c) => c.json({ error: "rate_limited" }, 429),
    }),
  );

  app.get("/api/v1/health", (c) => {
    const hb = store.heartbeat();
    const now = Date.now() / 1000;
    const alive = hb != null && now - hb.updatedAt < config.workerStaleSeconds;
    const body: HealthResponse = {
      status: alive ? "ok" : "degraded",
      worker: { alive, heartbeatAt: hb?.updatedAt ?? null, activeModel: hb?.activeModel ?? null },
      gpu: hb?.gpu ?? null,
      queueDepth: store.queueDepth(),
    };
    // ?worker=required makes a dead worker a hard failure (503) so the container
    // HEALTHCHECK goes red when the worker is down. The default (no param) stays
    // 200 so the SPA status UI can render "degraded" without treating it as an error.
    if (c.req.query("worker") === "required" && !alive) return c.json(body, 503);
    return c.json(body);
  });

  app.get("/openapi.json", (c) => c.json(buildOpenApiDoc()));
  app.get("/docs", (c) => c.html(SWAGGER_HTML));

  app.get("/api/v1/jobs", (c) => c.json({ jobs: store.list() }));

  app.get("/api/v1/jobs/:id", (c) => {
    const job = store.get(c.req.param("id"));
    return job ? c.json(job) : c.json({ error: "not_found" }, 404);
  });

  app.get("/api/v1/jobs/:id/events", (c) => {
    const id = c.req.param("id");
    const initial = store.get(id);
    if (!initial) return c.json({ error: "not_found" }, 404);
    return streamSSE(c, async (stream) => {
      await stream.writeSSE({ event: "job", data: JSON.stringify(initial) });
      if (isTerminal(initial.status)) return;
      await new Promise<void>((resolve) => {
        let settled = false;
        let unsub = () => {};
        const finish = () => {
          if (settled) return;
          settled = true;
          unsub();
          resolve();
        };
        // Serve this job out of the hub's already-computed snapshot rather than a
        // per-subscriber store.get() every tick (the N+1 the hub exists to avoid). A job
        // that finishes before it is ever observed in an `active` tick (the sub-first-tick
        // race) lands in `completed` on the tick it leaves the active set, or is caught by
        // the fresh get() below; either way the stream can't hang open.
        const emit = (snap: HubSnapshot) => {
          const job = snap.active.get(id) ?? snap.completed.get(id);
          if (job) void stream.writeSSE({ event: "job", data: JSON.stringify(job) });
          if (job && isTerminal(job.status)) finish();
          else if (!snap.active.has(id) && !snap.completed.has(id)) {
            // Gone from both maps: either never active (already terminal at subscribe
            // time, handled by the get() below) or deleted. Fall back to a direct read.
            const now = store.get(id);
            if (!now || isTerminal(now.status)) finish();
          }
        };
        unsub = hub.subscribe(emit);
        stream.onAbort(finish);
        // Close the race window between the initial get() above and subscribe().
        const now = store.get(id);
        if (!now || isTerminal(now.status)) {
          if (now) void stream.writeSSE({ event: "job", data: JSON.stringify(now) });
          finish();
        }
      });
    });
  });

  app.post("/api/v1/jobs", async (c) => {
    if (store.queueDepth() >= config.maxQueueDepth) {
      return c.json({ error: "queue_full" }, 429);
    }
    if (store.activeCount() >= config.maxActiveJobs) {
      return c.json({ error: "too_many_active_jobs" }, 429);
    }

    let parsed;
    try {
      parsed = await parseMultipart(c);
    } catch (e) {
      return c.json(
        { error: "upload_failed", detail: String(e instanceof Error ? e.message : e) },
        400,
      );
    }

    const modesRaw = (parsed.fields.modes ?? [])
      .flatMap((s) => s.split(","))
      .map((s) => s.trim())
      .filter(Boolean);
    const modesResult = z.array(Mode).safeParse(modesRaw);
    const locale = Locale.safeParse(parsed.fields.locale?.[0]).data ?? "zh-TW";
    if (!modesResult.success || modesResult.data.length === 0) {
      await cleanup(parsed.files);
      return c.json({ error: "invalid_modes" }, 400);
    }
    const modes = [...new Set(modesResult.data)];

    // engine pins markdown routing (single-value field; default 'auto'). Validate before any
    // upload is retained, same as invalid_modes: a bad value or an engine set without a
    // markdown target must not leave files on the uploads volume.
    const engineResult = Engine.safeParse(parsed.fields.engine?.[0] ?? "auto");
    if (!engineResult.success) {
      await cleanup(parsed.files);
      return c.json({ error: "invalid_engine" }, 400);
    }
    const engine = engineResult.data;
    // engine only governs markdown (pdf → pp-ocrv6, word → VL are fixed), so pinning one
    // without asking for markdown is a client mistake, not a silent no-op.
    if (engine !== "auto" && !modes.includes("markdown")) {
      await cleanup(parsed.files);
      return c.json({ error: "engine_requires_markdown_mode" }, 400);
    }

    const created: CreateJobsResponse["jobs"] = [];
    const skipped: CreateJobsResponse["skipped"] = [];

    for (const f of parsed.files) {
      if (f.truncated) {
        await unlink(f.path).catch(() => {});
        skipped.push({ filename: f.filename, reason: "too_large" });
        continue;
      }
      const ext = extname(f.filename).toLowerCase();

      if (ext === ".pdf") {
        const pf = await preflightPdf(f.path);
        if (!pf.ok) {
          await unlink(f.path).catch(() => {});
          skipped.push({ filename: f.filename, reason: pf.reason ?? "invalid_pdf" });
          continue;
        }
        // markdown + word from one upload share a group so the worker runs VL once.
        const dual = modes.includes("markdown") && modes.includes("word");
        const groupId = dual ? randomUUID() : null;
        for (const mode of modes) {
          const gid = mode === "markdown" || mode === "word" ? groupId : null;
          // Persist the requested engine on the markdown row only; pdf/word are fixed-route,
          // so they always store 'auto'.
          const id = store.enqueue({
            mode,
            filename: f.filename,
            locale,
            engine: mode === "markdown" ? engine : "auto",
            uploadPath: f.path,
            groupId: gid,
          });
          created.push({ id, filename: f.filename, mode, groupId: gid });
        }
      } else {
        await unlink(f.path).catch(() => {});
        skipped.push({ filename: f.filename, reason: "unsupported" });
      }
    }

    if (parsed.filesLimitHit) {
      // Files past the per-request cap are dropped by busboy before they ever reach
      // parsed.files; surface that instead of letting them vanish silently.
      skipped.push({
        filename: "(additional files)",
        reason: `too_many_files (max ${config.maxFilesPerRequest} per request)`,
      });
    }

    if (created.length === 0) return c.json({ error: "no_valid_files", skipped }, 400);
    const body: CreateJobsResponse = { jobs: created, skipped };
    return c.json(body);
  });

  app.post("/api/v1/jobs/:id/cancel", (c) => {
    const status = store.requestCancel(c.req.param("id"));
    return status === null ? c.json({ error: "not_found" }, 404) : c.json({ status });
  });

  app.delete("/api/v1/jobs/:id", async (c) => {
    const removed = store.remove(c.req.param("id"));
    if (!removed) return c.json({ error: "not_found" }, 404);
    if (removed.downloadId) {
      try {
        await rm(safeResolve(config.outputsDir, removed.downloadId), {
          recursive: true,
          force: true,
        });
      } catch {
        /* best-effort output cleanup */
      }
    }
    return c.json({ status: "deleted" });
  });

  app.get("/api/v1/download/:id", async (c) => {
    const job = store.get(c.req.param("id"));
    if (!job || job.status !== "done" || !job.result) return c.json({ error: "not_found" }, 404);
    const dl = job.result.downloadId;
    let dir: string;
    try {
      dir = safeResolve(config.outputsDir, dl);
    } catch {
      return c.json({ error: "invalid_path" }, 400);
    }
    const base = sanitizeDownloadName(
      job.result.originalName ? job.result.originalName.replace(/\.[^.]+$/, "") : dl,
    );
    if (job.mode === "pdf") {
      const path = join(dir, `${dl}.pdf`);
      try {
        return await fileResponse(path, `${base}.pdf`, "application/pdf", c.req.method);
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code === "ENOENT") {
          return c.json({ error: "not_found" }, 404);
        }
        throw e;
      }
    }
    if (job.mode === "word") {
      const path = join(dir, `${dl}.docx`);
      try {
        return await fileResponse(
          path,
          `${base}.docx`,
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          c.req.method,
        );
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code === "ENOENT") {
          return c.json({ error: "not_found" }, 404);
        }
        throw e;
      }
    }
    // markdown → zip the folder (md + images), excluding any docx sibling. Outputs are small
    // (a markdown file + a few images); an in-memory zip is fine, but bound the aggregate size
    // so a pathological document can't stall the event loop or OOM the process.
    if (!existsSync(dir)) return c.json({ error: "not_found" }, 404);
    // Hoisted so GET and HEAD send the same content-disposition.
    const mdDisposition = contentDisposition(`${base}-markdown.zip`);
    // HEAD is re-dispatched here by Hono, but building the zip (readdir + read-every-file
    // + in-memory deflate) just to have @hono/node-server discard the body is wasted work
    // — and unlike fileResponse's HEAD guard, this branch would do it all. Answer
    // headers-only. content-length is omitted: it's unknowable without building the zip,
    // and HEAD is permitted to omit it.
    if (c.req.method === "HEAD") {
      return new Response(null, {
        headers: {
          "content-type": "application/zip",
          "content-disposition": mdDisposition,
          "cache-control": "no-store",
        },
      });
    }
    let entries: string[];
    try {
      entries = readdirSync(dir, { recursive: true }) as string[];
    } catch (e) {
      // Only a vanished dir (retention GC rmtree'd it between the existsSync guard and
      // here) is a 404. A real fault like EACCES on a permission-broken dir must not be
      // masked as not_found — rethrow so Hono surfaces it as a 500.
      if ((e as NodeJS.ErrnoException).code === "ENOENT") {
        return c.json({ error: "not_found" }, 404);
      }
      throw e;
    }
    const MAX_ZIP_BYTES = Math.min(config.maxUploadBytes, 200 * 1024 * 1024);
    const files: Record<string, Uint8Array> = {};
    let total = 0;
    for (const name of entries) {
      const full = join(dir, name);
      try {
        const st = statSync(full);
        if (name.toLowerCase().endsWith(".docx") || !st.isFile()) continue;
        total += st.size;
        if (total > MAX_ZIP_BYTES) return c.json({ error: "output_too_large" }, 413);
        // Human-facing entry names: on disk the artifact is keyed by download id (the
        // cross-language worker contract), but nobody wants to extract "<uuid>.md" loose into
        // their cwd. Rename the main markdown to the document's name and root everything under
        // one "<base>-markdown/" folder — self-contained on extraction, and the name says what
        // format is inside (the .zip alone doesn't, unlike the .pdf/.docx downloads). Image
        // refs in the .md are relative ("imgs/..."), so the rename + prefix keep them resolving.
        const entryName = name === `${dl}.md` ? `${base}.md` : name;
        // Async read so a folder of large images doesn't block the event loop.
        files[`${base}-markdown/${entryName}`] = new Uint8Array(await readFile(full));
      } catch (e) {
        // Only skip an entry that vanished mid-scan (retention GC rmtree'd the dir).
        // A real fault like EACCES must propagate rather than be silently dropped.
        if ((e as NodeJS.ErrnoException).code === "ENOENT") continue;
        throw e;
      }
    }
    if (Object.keys(files).length === 0) return c.json({ error: "not_found" }, 404);
    // level 1: images are already compressed, so a higher level just burns CPU
    // for no meaningful size win. Async zip() keeps the deflate off the event loop.
    const zipped = await new Promise<Uint8Array>((resolveZip, rejectZip) => {
      zip(files, { level: 1 }, (err, data) => (err ? rejectZip(err) : resolveZip(data)));
    });
    return new Response(zipped, {
      headers: {
        "content-type": "application/zip",
        "content-disposition": mdDisposition,
        "cache-control": "no-store",
      },
    });
  });

  app.get("/api/v1/preview/:id", async (c) => {
    const job = store.get(c.req.param("id"));
    if (!job || job.status !== "done" || !job.result) return c.json({ error: "not_found" }, 404);
    if (job.mode !== "markdown") return c.json({ error: "not_previewable" }, 400);
    let path: string;
    try {
      path = join(
        safeResolve(config.outputsDir, job.result.downloadId),
        `${job.result.downloadId}.md`,
      );
    } catch {
      return c.json({ error: "invalid_path" }, 400);
    }
    if (!existsSync(path)) return c.json({ error: "not_found" }, 404);
    const content = await readFile(path, "utf8");
    return c.json({ content, filename: `${job.result.downloadId}.md` });
  });

  return app;
}
