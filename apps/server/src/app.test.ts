import { mkdtempSync } from "node:fs";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { serve } from "@hono/node-server";
import type { HttpBindings } from "@hono/node-server";
import type { Job } from "@pdf-converter/shared";
import { Hono } from "hono";
import { PDFDocument } from "pdf-lib";
import { afterAll, expect, test } from "vite-plus/test";
import { createApp } from "./app.ts";
import { JobStore } from "./db.ts";
import { ProgressHub } from "./sse.ts";

const SCHEMA = fileURLToPath(new URL("../../../db/schema.sql", import.meta.url));

function newApp() {
  const dir = mkdtempSync(join(tmpdir(), "pdfocr-"));
  const store = new JobStore(join(dir, "t.db"), SCHEMA);
  return createApp(store, new ProgressHub(store));
}

// A minimal valid one-page PDF so preflightPdf() accepts the upload and the POST
// handler reaches the enqueue path. (createApp().request() can't carry a multipart
// body — parseMultipart reads the raw node:http request off c.env.incoming, which
// only @hono/node-server populates — so these tests hit a real ephemeral server.)
async function tinyPdf(): Promise<Uint8Array> {
  const doc = await PDFDocument.create();
  doc.addPage([200, 200]);
  return doc.save();
}

const servers: { close: () => void }[] = [];
afterAll(() => {
  for (const s of servers) s.close();
});

/** Serve a fresh app on an ephemeral port and return its base URL. */
function serveApp(): Promise<string> {
  const dir = mkdtempSync(join(tmpdir(), "pdfocr-"));
  const store = new JobStore(join(dir, "t.db"), SCHEMA);
  const app = new Hono<{ Bindings: HttpBindings }>();
  app.route("/", createApp(store, new ProgressHub(store)));
  return new Promise((resolve) => {
    const server = serve({ fetch: app.fetch, port: 0, hostname: "127.0.0.1" }, (info) => {
      servers.push(server);
      resolve(`http://127.0.0.1:${(info as AddressInfo).port}`);
    });
  });
}

async function submit(base: string, fields: [string, string][]): Promise<Response> {
  const fd = new FormData();
  fd.append("files", new Blob([await tinyPdf()], { type: "application/pdf" }), "doc.pdf");
  for (const [k, v] of fields) fd.append(k, v);
  return fetch(`${base}/api/v1/jobs`, { method: "POST", body: fd });
}

test("GET /api/v1/health returns worker status", async () => {
  const res = await newApp().request("/api/v1/health");
  expect(res.status).toBe(200);
  const body = (await res.json()) as { worker: { alive: boolean }; queueDepth: number };
  expect(body.worker.alive).toBe(false);
  expect(body.queueDepth).toBe(0);
});

test("GET /api/v1/jobs is empty; unknown job is 404", async () => {
  const app = newApp();
  const list = (await (await app.request("/api/v1/jobs")).json()) as { jobs: unknown[] };
  expect(list.jobs).toEqual([]);
  expect((await app.request("/api/v1/jobs/nope")).status).toBe(404);
});

test("GET /openapi.json is a valid OpenAPI document", async () => {
  const doc = (await (await newApp().request("/openapi.json")).json()) as {
    openapi: string;
    paths: Record<string, unknown>;
  };
  expect(doc.openapi).toBe("3.1.0");
  expect(Object.keys(doc.paths).length).toBeGreaterThan(3);
});

test("POST with engine=vl + markdown records engine 'vl' on the job", async () => {
  const base = await serveApp();
  const res = await submit(base, [
    ["modes", "markdown"],
    ["engine", "vl"],
  ]);
  expect(res.status).toBe(200);
  const { jobs } = (await res.json()) as { jobs: { id: string }[] };
  expect(jobs).toHaveLength(1);
  const job = (await (await fetch(`${base}/api/v1/jobs/${jobs[0].id}`)).json()) as Job;
  expect(job.engine).toBe("vl");
});

test("POST engine=docling with word-only modes is 400 engine_requires_markdown_mode", async () => {
  const base = await serveApp();
  const res = await submit(base, [
    ["modes", "word"],
    ["engine", "docling"],
  ]);
  expect(res.status).toBe(400);
  expect((await res.json()) as { error: string }).toEqual({
    error: "engine_requires_markdown_mode",
  });
});

test("POST with an unknown engine is 400 invalid_engine", async () => {
  const base = await serveApp();
  const res = await submit(base, [
    ["modes", "markdown"],
    ["engine", "bogus"],
  ]);
  expect(res.status).toBe(400);
  expect((await res.json()) as { error: string }).toEqual({ error: "invalid_engine" });
});

test("POST without an engine field defaults the job to engine 'auto'", async () => {
  const base = await serveApp();
  const res = await submit(base, [["modes", "markdown"]]);
  expect(res.status).toBe(200);
  const { jobs } = (await res.json()) as { jobs: { id: string }[] };
  const job = (await (await fetch(`${base}/api/v1/jobs/${jobs[0].id}`)).json()) as Job;
  expect(job.engine).toBe("auto");
});

test("dual export markdown+word with engine=vl pins md to vl and leaves word auto", async () => {
  const base = await serveApp();
  const res = await submit(base, [
    ["modes", "markdown"],
    ["modes", "word"],
    ["engine", "vl"],
  ]);
  expect(res.status).toBe(200);
  const { jobs } = (await res.json()) as { jobs: { id: string; mode: string }[] };
  expect(jobs).toHaveLength(2);
  const byMode: Record<string, string> = {};
  for (const j of jobs) {
    const full = (await (await fetch(`${base}/api/v1/jobs/${j.id}`)).json()) as Job;
    byMode[full.mode] = full.engine;
  }
  expect(byMode.markdown).toBe("vl");
  expect(byMode.word).toBe("auto");
});
