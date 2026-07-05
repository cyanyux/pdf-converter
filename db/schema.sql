-- Canonical SQLite schema — the cross-language contract between the TypeScript
-- server (node:sqlite) and the Python GPU worker (sqlite3). Both open the same
-- WAL database file. Keep this file the single source of truth; changing a
-- column requires updating apps/server/src/db.ts and worker/src/worker/store.py
-- and is guarded by the schema round-trip test.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
  id            TEXT PRIMARY KEY,             -- server-generated UUID (never client-supplied)
  group_id      TEXT,                          -- links md+word jobs from one dual-export upload
  mode          TEXT NOT NULL,                 -- 'pdf' | 'markdown' | 'word'
  filename      TEXT NOT NULL,                 -- original upload filename (display only)
  locale        TEXT NOT NULL DEFAULT 'zh-TW', -- 'zh-TW' | 'zh-CN' | 'en'
  status        TEXT NOT NULL DEFAULT 'queued',-- queued|processing|saving|done|error|cancelled|cancel_requested
  attempts      INTEGER NOT NULL DEFAULT 0,    -- reaper increments; poison-pill guard
  upload_path   TEXT,                          -- path to the streamed upload on the uploads volume
  download_id   TEXT,                          -- output artifact id (dir/file stem)
  result_json   TEXT,                          -- JSON JobResult when done
  error         TEXT,                          -- error message when status='error'
  created_at    REAL NOT NULL,                 -- epoch seconds
  updated_at    REAL NOT NULL,
  heartbeat_at  REAL                           -- worker-updated while actively processing this job
);

CREATE TABLE IF NOT EXISTS progress (
  job_id      TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  current     INTEGER NOT NULL DEFAULT 0,
  total       INTEGER NOT NULL DEFAULT 0,
  percent     INTEGER NOT NULL DEFAULT 0,
  status      TEXT    NOT NULL DEFAULT 'queued',
  message     TEXT    NOT NULL DEFAULT '',
  updated_at  REAL    NOT NULL
);

-- Single-row worker liveness/telemetry, read by the server /health endpoint.
CREATE TABLE IF NOT EXISTS worker_heartbeat (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  updated_at   REAL NOT NULL,
  active_model TEXT,           -- 'ppocr' | 'vl' | null (which model child is loaded)
  gpu_json     TEXT           -- JSON: device, vram_total_mb, vram_used_mb, hpi, etc.
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_group ON jobs (group_id);
CREATE INDEX IF NOT EXISTS idx_jobs_upload ON jobs (upload_path);
