import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";
import type {
  Engine,
  Job,
  JobResult,
  JobStatus,
  Locale,
  Mode,
  Progress,
} from "@pdf-converter/shared";
import { config } from "./config.ts";

interface JobRow {
  id: string;
  group_id: string | null;
  mode: string;
  filename: string;
  locale: string;
  engine: string;
  status: string;
  attempts: number;
  upload_path: string | null;
  download_id: string | null;
  result_json: string | null;
  error: string | null;
  created_at: number;
  updated_at: number;
  heartbeat_at: number | null;
}

interface ProgressRow {
  current: number;
  total: number;
  percent: number;
  status: string;
  message: string;
  updated_at: number;
}

const TERMINAL = new Set<JobStatus>(["done", "error", "cancelled"]);

/** Retry a write a few times on SQLITE_BUSY (belt-and-suspenders atop busy_timeout). */
function withRetry<T>(fn: () => T, attempts = 5): T {
  let lastErr: unknown;
  for (let i = 0; i < attempts; i++) {
    try {
      return fn();
    } catch (e) {
      const msg = String(e instanceof Error ? e.message : e);
      if (msg.includes("SQLITE_BUSY") || msg.includes("locked")) {
        lastErr = e;
        continue;
      }
      throw e;
    }
  }
  throw lastErr;
}

function toProgress(p: ProgressRow | undefined): Progress | null {
  if (!p) return null;
  return {
    current: p.current,
    total: p.total,
    percent: p.percent,
    status: p.status,
    message: p.message,
    updatedAt: p.updated_at,
  };
}

function toJob(row: JobRow, prog: ProgressRow | undefined): Job {
  return {
    id: row.id,
    groupId: row.group_id,
    mode: row.mode as Mode,
    filename: row.filename,
    locale: row.locale as Locale,
    engine: row.engine as Engine,
    status: row.status as JobStatus,
    attempts: row.attempts,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    heartbeatAt: row.heartbeat_at,
    progress: toProgress(prog),
    result: row.result_json ? (JSON.parse(row.result_json) as JobResult) : null,
    error: row.error,
  };
}

// jobs LEFT JOIN progress in ONE query (progress has a PK on job_id), so get/list/
// activeJobs don't fan out into a per-row progress SELECT (the old N+1).
const JOB_SELECT =
  "SELECT j.*, p.current AS p_current, p.total AS p_total, p.percent AS p_percent, " +
  "p.status AS p_status, p.message AS p_message, p.updated_at AS p_updated_at " +
  "FROM jobs j LEFT JOIN progress p ON p.job_id = j.id";

interface JoinedRow extends JobRow {
  p_current: number | null;
  p_total: number | null;
  p_percent: number | null;
  p_status: string | null;
  p_message: string | null;
  p_updated_at: number | null;
}

function toJobJoined(row: JoinedRow): Job {
  const prog: ProgressRow | undefined =
    row.p_updated_at == null
      ? undefined
      : {
          current: row.p_current ?? 0,
          total: row.p_total ?? 0,
          percent: row.p_percent ?? 0,
          status: row.p_status ?? "",
          message: row.p_message ?? "",
          updated_at: row.p_updated_at,
        };
  return toJob(row, prog);
}

export interface EnqueueInput {
  mode: Mode;
  filename: string;
  locale: Locale;
  /** requested markdown engine; the caller passes 'auto' for pdf/word rows (see app.ts) */
  engine: Engine;
  uploadPath: string;
  groupId?: string | null;
}

export interface WorkerHeartbeat {
  updatedAt: number;
  activeModel: string | null;
  gpu: Record<string, unknown> | null;
}

export class JobStore {
  readonly db: DatabaseSync;

  constructor(dbPath: string = config.dbPath, schemaPath: string = config.schemaPath) {
    mkdirSync(dirname(dbPath), { recursive: true });
    this.db = new DatabaseSync(dbPath);
    this.db.exec("PRAGMA journal_mode=WAL");
    this.db.exec("PRAGMA busy_timeout=5000");
    this.db.exec("PRAGMA foreign_keys=ON");
    const schemaSql = readFileSync(schemaPath, "utf8");
    this.db.exec(schemaSql);
    this.migrate(schemaSql);
  }

  /**
   * Idempotent schema catch-up for a pre-existing DB, derived from schema.sql itself.
   * schema.sql runs via CREATE TABLE IF NOT EXISTS, so a pre-existing table is never
   * altered by the exec above — a column added to schema.sql later never lands. Rather
   * than hand-maintaining per-column ALTERs here AND in the worker's Store._migrate (a
   * pair that can silently drift), apply schema.sql to a throwaway in-memory DB and
   * ALTER in whatever columns the live tables are missing. schema.sql stays the single
   * source of truth; a new NOT NULL column just needs a DEFAULT there (SQLite can't
   * backfill one without it — that fails loudly HERE at startup, not later at some
   * INSERT). The worker runs the identical algorithm.
   */
  private migrate(schemaSql: string): void {
    interface Col {
      name: string;
      type: string;
      notnull: number;
      dflt_value: string | null;
    }
    const ref = new DatabaseSync(":memory:");
    try {
      ref.exec(schemaSql);
      const tables = ref.prepare("SELECT name FROM sqlite_master WHERE type='table'").all() as {
        name: string;
      }[];
      for (const { name: table } of tables) {
        const have = new Set(
          (this.db.prepare(`PRAGMA table_info(${table})`).all() as unknown as Col[]).map(
            (c) => c.name,
          ),
        );
        for (const col of ref.prepare(`PRAGMA table_info(${table})`).all() as unknown as Col[]) {
          if (have.has(col.name)) continue;
          let ddl = `ALTER TABLE ${table} ADD COLUMN ${col.name} ${col.type}`;
          if (col.notnull) ddl += " NOT NULL";
          if (col.dflt_value != null) ddl += ` DEFAULT ${col.dflt_value}`;
          this.db.exec(ddl);
        }
      }
    } finally {
      ref.close();
    }
  }

  enqueue(input: EnqueueInput): string {
    const id = randomUUID();
    const now = Date.now() / 1000;
    withRetry(() => {
      this.db.exec("BEGIN IMMEDIATE");
      try {
        this.db
          .prepare(
            "INSERT INTO jobs(id,group_id,mode,filename,locale,engine,status,upload_path,created_at,updated_at) " +
              "VALUES(?,?,?,?,?,?,'queued',?,?,?)",
          )
          .run(
            id,
            input.groupId ?? null,
            input.mode,
            input.filename,
            input.locale,
            input.engine,
            input.uploadPath,
            now,
            now,
          );
        this.db
          .prepare(
            "INSERT INTO progress(job_id,current,total,percent,status,message,updated_at) VALUES(?,0,0,0,'queued','',?)",
          )
          .run(id, now);
        this.db.exec("COMMIT");
      } catch (e) {
        this.db.exec("ROLLBACK");
        throw e;
      }
    });
    return id;
  }

  get(id: string): Job | null {
    const row = this.db.prepare(`${JOB_SELECT} WHERE j.id = ?`).get(id) as JoinedRow | undefined;
    return row ? toJobJoined(row) : null;
  }

  list(limit = 200): Job[] {
    const rows = this.db
      .prepare(`${JOB_SELECT} ORDER BY j.created_at DESC LIMIT ?`)
      .all(limit) as unknown as JoinedRow[];
    return rows.map(toJobJoined);
  }

  /** Non-terminal jobs (for the SSE fan-out ticker and the active-jobs cap). */
  activeJobs(): Job[] {
    const rows = this.db
      .prepare(
        `${JOB_SELECT} WHERE j.status NOT IN ('done','error','cancelled') ORDER BY j.created_at`,
      )
      .all() as unknown as JoinedRow[];
    return rows.map(toJobJoined);
  }

  requestCancel(id: string): JobStatus | null {
    return withRetry(() => {
      // Read + write in one BEGIN IMMEDIATE txn, and terminal-guard the UPDATE, so a
      // status the worker commits on its own connection between our SELECT and UPDATE
      // (e.g. 'done' with a downloadable result) can't be clobbered back to a
      // non-terminal 'cancel_requested', which would wedge the job forever.
      this.db.exec("BEGIN IMMEDIATE");
      try {
        const row = this.db.prepare("SELECT status FROM jobs WHERE id = ?").get(id) as
          | { status: JobStatus }
          | undefined;
        if (!row) {
          this.db.exec("COMMIT");
          return null;
        }
        if (TERMINAL.has(row.status)) {
          this.db.exec("COMMIT");
          return row.status;
        }
        const now = Date.now() / 1000;
        // A queued job can be cancelled outright; a running one is asked to stop.
        const next: JobStatus = row.status === "queued" ? "cancelled" : "cancel_requested";
        this.db
          .prepare(
            "UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status NOT IN ('done','error','cancelled')",
          )
          .run(next, now, id);
        this.db
          .prepare(
            "UPDATE progress SET status=?, updated_at=? WHERE job_id=? AND job_id NOT IN " +
              "(SELECT id FROM jobs WHERE status IN ('done','error','cancelled'))",
          )
          .run(next, now, id);
        // Re-read: if the guarded UPDATE was a no-op (a terminal status raced in), the
        // row still holds that terminal state and that's what we return.
        const after = this.db.prepare("SELECT status FROM jobs WHERE id = ?").get(id) as {
          status: JobStatus;
        };
        this.db.exec("COMMIT");
        return after.status;
      } catch (e) {
        this.db.exec("ROLLBACK");
        throw e;
      }
    });
  }

  /** Delete a job row; returns its download_id so the caller can clean output files. */
  remove(id: string): { downloadId: string | null } | null {
    return withRetry(() => {
      const row = this.db.prepare("SELECT download_id FROM jobs WHERE id = ?").get(id) as
        | { download_id: string | null }
        | undefined;
      if (!row) return null;
      this.db.prepare("DELETE FROM jobs WHERE id = ?").run(id);
      return { downloadId: row.download_id };
    });
  }

  queueDepth(): number {
    const r = this.db.prepare("SELECT count(*) AS c FROM jobs WHERE status = 'queued'").get() as {
      c: number;
    };
    return r.c;
  }

  activeCount(): number {
    const r = this.db
      .prepare("SELECT count(*) AS c FROM jobs WHERE status NOT IN ('done','error','cancelled')")
      .get() as { c: number };
    return r.c;
  }

  heartbeat(): WorkerHeartbeat | null {
    const r = this.db
      .prepare("SELECT updated_at, active_model, gpu_json FROM worker_heartbeat WHERE id = 1")
      .get() as
      | { updated_at: number; active_model: string | null; gpu_json: string | null }
      | undefined;
    if (!r) return null;
    return {
      updatedAt: r.updated_at,
      activeModel: r.active_model,
      gpu: r.gpu_json ? (JSON.parse(r.gpu_json) as Record<string, unknown>) : null,
    };
  }

  close(): void {
    this.db.close();
  }
}
