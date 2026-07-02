import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { serve } from "@hono/node-server";
import type { HealthResponse } from "@pdf-ocr/shared";
import { Hono } from "hono";

const DB_PATH = process.env.PDF_OCR_DB ?? "./data/pdf-ocr.db";
const SCHEMA_PATH = process.env.PDF_OCR_SCHEMA ?? "./db/schema.sql";
const PORT = Number(process.env.PORT ?? 8000);
const WORKER_STALE_S = 30;

const db = new DatabaseSync(DB_PATH);
db.exec("PRAGMA journal_mode=WAL");
db.exec("PRAGMA busy_timeout=5000");
db.exec(readFileSync(SCHEMA_PATH, "utf8"));

const app = new Hono();

app.get("/api/v1/health", (c) => {
  const hb = db
    .prepare("SELECT updated_at, active_model FROM worker_heartbeat WHERE id = 1")
    .get() as { updated_at: number; active_model: string | null } | undefined;
  const depth = db.prepare("SELECT count(*) AS c FROM jobs WHERE status = 'queued'").get() as {
    c: number;
  };
  const now = Date.now() / 1000;
  const alive = hb != null && now - hb.updated_at < WORKER_STALE_S;
  const body: HealthResponse = {
    status: alive ? "ok" : "degraded",
    worker: {
      alive,
      heartbeatAt: hb?.updated_at ?? null,
      activeModel: hb?.active_model ?? null,
    },
    gpu: null,
    queueDepth: depth.c,
  };
  return c.json(body);
});

serve({ fetch: app.fetch, port: PORT }, (info) => {
  // eslint-disable-next-line no-console
  console.log(`[server] listening on http://127.0.0.1:${info.port}`);
});
