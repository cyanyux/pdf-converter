import { randomUUID } from "node:crypto";
import { createReadStream, existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { rm, unlink } from "node:fs/promises";
import { join } from "node:path";
import { Readable } from "node:stream";
import type { HttpBindings } from "@hono/node-server";
import {
  type CreateJobsResponse,
  isTerminal,
  Locale,
  Mode,
  type HealthResponse,
} from "@pdf-ocr/shared";
import { zipSync } from "fflate";
import { Hono } from "hono";
import { streamSSE } from "hono/streaming";
import { rateLimiter } from "hono-rate-limiter";
import { z } from "zod";
import { apiKeyAuth } from "./auth.ts";
import { config } from "./config.ts";
import type { JobStore } from "./db.ts";
import { preflightPdf } from "./preflight.ts";
import { safeResolve, sanitizeDownloadName } from "./safe-resolve.ts";
import type { ProgressHub } from "./sse.ts";
import type { UploadedFile } from "./upload.ts";
import { parseMultipart } from "./upload.ts";

type Env = { Bindings: HttpBindings };

async function cleanup(files: UploadedFile[]): Promise<void> {
  await Promise.all(files.map((f) => unlink(f.path).catch(() => {})));
}

function fileResponse(path: string, downloadName: string, mime: string): Response {
  const web = Readable.toWeb(createReadStream(path)) as unknown as ReadableStream<Uint8Array>;
  return new Response(web, {
    headers: {
      "content-type": mime,
      "content-disposition": `attachment; filename="${downloadName}"`,
      "cache-control": "no-store",
    },
  });
}

export function createApp(store: JobStore, hub: ProgressHub): Hono<Env> {
  const app = new Hono<Env>();

  app.use("/api/*", apiKeyAuth());
  app.use(
    "/api/v1/jobs",
    rateLimiter({
      windowMs: 60_000,
      limit: 60,
      standardHeaders: "draft-7",
      keyGenerator: (c) => c.req.header("x-api-key") ?? c.req.header("authorization") ?? "anon",
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
    return c.json(body);
  });

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
        const unsub = hub.subscribe((snap) => {
          const done = snap.completed.get(id);
          const job = done ?? snap.active.get(id);
          if (job) void stream.writeSSE({ event: "job", data: JSON.stringify(job) });
          if (done) {
            unsub();
            resolve();
          }
        });
        stream.onAbort(() => {
          unsub();
          resolve();
        });
      });
    });
  });

  app.post("/api/v1/jobs", async (c) => {
    if (store.queueDepth() >= config.maxQueueDepth) {
      return c.json({ error: "queue_full" }, 429);
    }
    if (store.activeCount() >= config.maxActiveJobsPerKey) {
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

    const created: CreateJobsResponse["jobs"] = [];
    const skipped: CreateJobsResponse["skipped"] = [];

    for (const f of parsed.files) {
      if (f.truncated) {
        await unlink(f.path).catch(() => {});
        skipped.push({ filename: f.filename, reason: "too_large" });
        continue;
      }
      if (!f.filename.toLowerCase().endsWith(".pdf")) {
        await unlink(f.path).catch(() => {});
        skipped.push({ filename: f.filename, reason: "not_pdf" });
        continue;
      }
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
        const id = store.enqueue({
          mode,
          filename: f.filename,
          locale,
          uploadPath: f.path,
          groupId: gid,
        });
        created.push({ id, filename: f.filename, mode, groupId: gid });
      }
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

  app.get("/api/v1/download/:id", (c) => {
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
      if (!existsSync(path)) return c.json({ error: "not_found" }, 404);
      return fileResponse(path, `${base}.pdf`, "application/pdf");
    }
    if (job.mode === "word") {
      const path = join(dir, `${dl}.docx`);
      if (!existsSync(path)) return c.json({ error: "not_found" }, 404);
      return fileResponse(
        path,
        `${base}.docx`,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      );
    }
    // markdown → zip the folder (md + images), excluding any docx sibling.
    // Outputs are small (a markdown file + a few images), so an in-memory zip is fine.
    if (!existsSync(dir)) return c.json({ error: "not_found" }, 404);
    const files: Record<string, Uint8Array> = {};
    for (const name of readdirSync(dir, { recursive: true }) as string[]) {
      const full = join(dir, name);
      if (name.toLowerCase().endsWith(".docx") || !statSync(full).isFile()) continue;
      files[name] = new Uint8Array(readFileSync(full));
    }
    const zipped = zipSync(files, { level: 9 });
    return new Response(zipped, {
      headers: {
        "content-type": "application/zip",
        "content-disposition": `attachment; filename="${base}.zip"`,
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
    const { readFile } = await import("node:fs/promises");
    const content = await readFile(path, "utf8");
    return c.json({ content, filename: `${job.result.downloadId}.md` });
  });

  return app;
}
