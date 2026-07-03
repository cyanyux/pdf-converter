import { randomUUID } from "node:crypto";
import { createWriteStream } from "node:fs";
import { mkdir, unlink } from "node:fs/promises";
import { extname, join } from "node:path";
import type { HttpBindings } from "@hono/node-server";
import busboy from "busboy";
import type { Context } from "hono";
import { ACCEPTED_EXTS, config } from "./config.ts";

export interface UploadedFile {
  /** absolute path on the uploads volume */
  path: string;
  filename: string;
  /** true if the stream hit the size limit and was cut short */
  truncated: boolean;
}

export interface ParsedMultipart {
  fields: Record<string, string[]>;
  files: UploadedFile[];
  /** true if the request carried more files than the per-request cap allows */
  filesLimitHit: boolean;
}

/**
 * Stream a multipart request straight to the uploads volume (never buffering
 * whole files in memory), enforcing per-file size and file-count caps before
 * data is retained. Uses the raw Node request from @hono/node-server bindings.
 */
export function parseMultipart(c: Context<{ Bindings: HttpBindings }>): Promise<ParsedMultipart> {
  const req = c.env.incoming;
  return new Promise<ParsedMultipart>((resolve, reject) => {
    let bb: busboy.Busboy;
    try {
      bb = busboy({
        headers: req.headers,
        limits: {
          fileSize: config.maxUploadBytes,
          files: config.maxFilesPerRequest,
          // Cap non-file parts so a crafted multipart body can't exhaust memory
          // via unbounded field count/size (files carry the payload, not fields).
          fields: 20,
          fieldSize: 100 * 1024,
          fieldNameSize: 200,
          parts: config.maxFilesPerRequest + 40,
        },
      });
    } catch (e) {
      reject(e instanceof Error ? e : new Error(String(e)));
      return;
    }

    const fields: Record<string, string[]> = {};
    const files: UploadedFile[] = [];
    const writes: Promise<void>[] = [];
    const openStreams = new Set<ReturnType<typeof createWriteStream>>();
    const pendingPaths = new Set<string>();
    let filesLimitHit = false;
    let settled = false;

    // Client aborts mid-upload, busboy/stream errors: reject instead of hanging
    // forever (Node's pipe does NOT forward a source error to the destination, so
    // without this the resolving `close` event never fires), and delete any partial
    // files already opened on disk.
    const fail = (err: unknown) => {
      if (settled) return;
      settled = true;
      req.unpipe(bb);
      bb.destroy();
      for (const ws of openStreams) ws.destroy();
      const paths = [...pendingPaths];
      // Destroying the streams above rejects the in-flight per-file write promises.
      // The `close` handler that would normally await them is skipped once settled,
      // so consume them here with allSettled (which never throws) — otherwise those
      // rejections are unhandled and crash the process — then remove the partial
      // files before rejecting.
      void Promise.allSettled(writes)
        .then(() => Promise.all(paths.map((p) => unlink(p).catch(() => {}))))
        .finally(() => {
          reject(err instanceof Error ? err : new Error(String(err)));
        });
    };

    bb.on("field", (name, value) => {
      (fields[name] ??= []).push(value);
    });

    // Files past the cap: busboy stops emitting `file` and fires this once.
    bb.on("filesLimit", () => {
      filesLimitHit = true;
    });

    // Parts past the `parts` cap: busboy stops emitting further `file`/`field` events and
    // fires this once. Surface it the same way as filesLimit so any dropped uploads show up
    // in `skipped[]` instead of vanishing silently (field parts are separately capped well
    // below this, so hitting the parts cap effectively means files were dropped).
    bb.on("partsLimit", () => {
      filesLimitHit = true;
    });

    bb.on("file", (_name, stream, info) => {
      // Store the upload under a whitelisted extension (.pdf) or else .bin — this is just a
      // sane on-disk suffix / defense against odd names; the worker dispatches by job.mode,
      // NOT by file extension, so this suffix is not load-bearing for routing.
      const ext = extname(info.filename || "").toLowerCase();
      const safeExt = ACCEPTED_EXTS.includes(ext) ? ext : ".bin";
      const path = join(config.uploadsDir, `${randomUUID()}${safeExt}`);
      pendingPaths.add(path);
      let truncated = false;
      stream.on("limit", () => {
        truncated = true;
      });
      const ws = createWriteStream(path);
      openStreams.add(ws);
      writes.push(
        new Promise<void>((res, rej) => {
          ws.on("finish", () => {
            openStreams.delete(ws);
            pendingPaths.delete(path);
            files.push({ path, filename: info.filename || "upload.pdf", truncated });
            res();
          });
          ws.on("error", rej);
          stream.on("error", rej);
          stream.pipe(ws);
        }),
      );
    });

    bb.on("error", fail);
    bb.on("close", () => {
      if (settled) return;
      Promise.all(writes)
        .then(() => {
          if (settled) return;
          settled = true;
          resolve({ fields, files, filesLimitHit });
        })
        .catch(fail);
    });

    // Attach abort/error listeners BEFORE the async mkdir so an abort that arrives
    // during it can't be missed. On a completed request `req.complete` is true; on a
    // client abort it is false and busboy never emits `close`, so this settles us.
    req.on("error", fail);
    req.on("close", () => {
      if (!req.complete) fail(new Error("request aborted before completion"));
    });

    // Ensure the uploads dir exists before piping (per-file write streams are created
    // lazily in the `file` handler); skip the pipe if an abort already failed us.
    mkdir(config.uploadsDir, { recursive: true })
      .then(() => {
        if (!settled) req.pipe(bb);
      })
      .catch(fail);
  });
}
