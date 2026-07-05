import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "vite-plus/test";
import { createApp } from "./app.ts";
import { JobStore } from "./db.ts";
import { ProgressHub } from "./sse.ts";

const SCHEMA = fileURLToPath(new URL("../../../db/schema.sql", import.meta.url));

function newApp() {
  const dir = mkdtempSync(join(tmpdir(), "pdfocr-"));
  const store = new JobStore(join(dir, "t.db"), SCHEMA);
  return createApp(store, new ProgressHub(store));
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
