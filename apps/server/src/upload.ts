import { randomUUID } from "node:crypto";
import { createWriteStream } from "node:fs";
import { mkdir } from "node:fs/promises";
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
}

/**
 * Stream a multipart request straight to the uploads volume (never buffering
 * whole files in memory), enforcing per-file size and file-count caps before
 * data is retained. Uses the raw Node request from @hono/node-server bindings.
 */
export async function parseMultipart(
  c: Context<{ Bindings: HttpBindings }>,
): Promise<ParsedMultipart> {
  await mkdir(config.uploadsDir, { recursive: true });
  const req = c.env.incoming;
  return new Promise<ParsedMultipart>((resolve, reject) => {
    let bb: busboy.Busboy;
    try {
      bb = busboy({
        headers: req.headers,
        limits: { fileSize: config.maxUploadBytes, files: config.maxFilesPerRequest },
      });
    } catch (e) {
      reject(e instanceof Error ? e : new Error(String(e)));
      return;
    }

    const fields: Record<string, string[]> = {};
    const files: UploadedFile[] = [];
    const writes: Promise<void>[] = [];

    bb.on("field", (name, value) => {
      (fields[name] ??= []).push(value);
    });

    bb.on("file", (_name, stream, info) => {
      // Preserve the real extension so the worker can route by file type.
      const ext = extname(info.filename || "").toLowerCase();
      const safeExt = ACCEPTED_EXTS.includes(ext) ? ext : ".bin";
      const path = join(config.uploadsDir, `${randomUUID()}${safeExt}`);
      let truncated = false;
      stream.on("limit", () => {
        truncated = true;
      });
      const ws = createWriteStream(path);
      writes.push(
        new Promise<void>((res, rej) => {
          ws.on("finish", () => {
            files.push({ path, filename: info.filename || "upload.pdf", truncated });
            res();
          });
          ws.on("error", rej);
          stream.on("error", rej);
          stream.pipe(ws);
        }),
      );
    });

    bb.on("error", reject);
    bb.on("close", () => {
      Promise.all(writes)
        .then(() => resolve({ fields, files }))
        .catch(reject);
    });

    req.pipe(bb);
  });
}
