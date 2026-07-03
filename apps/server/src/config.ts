import { resolve } from "node:path";

/** Repo root: env override, else the process CWD (repo root in dev, /app in the image). */
const root = process.env.PDF_OCR_ROOT ?? process.cwd();

function num(name: string, fallback: number): number {
  const v = process.env[name];
  const n = v == null ? NaN : Number(v);
  return Number.isFinite(n) ? n : fallback;
}

export const config = {
  root,
  dbPath: process.env.PDF_OCR_DB ?? resolve(root, "data/pdf-ocr.db"),
  schemaPath: process.env.PDF_OCR_SCHEMA ?? resolve(root, "db/schema.sql"),
  uploadsDir: process.env.PDF_OCR_UPLOADS ?? resolve(root, "data/uploads"),
  outputsDir: process.env.PDF_OCR_OUTPUTS ?? resolve(root, "data/outputs"),
  /** Built SPA served in production; ignored in dev (Vite dev server owns it). */
  staticDir: process.env.PDF_OCR_STATIC ?? resolve(root, "apps/spa/dist"),

  port: num("PORT", 8000),
  host: process.env.HOST ?? "127.0.0.1",

  /** When set, all /api + MCP requests must present it. Empty = open (local dev). */
  apiKey: process.env.API_KEY ?? "",

  maxUploadBytes: num("PDF_OCR_MAX_UPLOAD_MB", 500) * 1024 * 1024,
  maxFilesPerRequest: num("PDF_OCR_MAX_FILES", 20),
  /** Reject new jobs when the queue is at/above this depth (429). */
  maxQueueDepth: num("PDF_OCR_MAX_QUEUE", 100),
  /**
   * Global backpressure cap on concurrent non-terminal jobs. The service uses a
   * single shared API_KEY, so this is a whole-service ceiling — not a per-tenant /
   * per-key quota (store.activeCount() is global).
   */
  maxActiveJobs: num("PDF_OCR_MAX_ACTIVE", 20),
  /**
   * Above this size, preflightPdf skips the in-memory pdf-lib structural parse
   * (only checks the %PDF- magic) so a huge upload can't OOM the request handler;
   * the worker validates such PDFs instead.
   */
  preflightMaxBytes: num("PDF_OCR_PREFLIGHT_MAX_MB", 100) * 1024 * 1024,

  /** Worker considered dead if its heartbeat is older than this. */
  workerStaleSeconds: num("PDF_OCR_WORKER_STALE_S", 30),
  /** Output/upload/job retention in seconds. */
  maxFileAgeSeconds: num("PDF_OCR_MAX_FILE_AGE", 3600),
  jobMaxAgeSeconds: num("PDF_OCR_JOB_MAX_AGE", 7200),
} as const;

/** True when the server binds a non-loopback interface (tunnel / LAN exposure). */
export function isExposedBind(host: string): boolean {
  return host !== "127.0.0.1" && host !== "localhost" && host !== "::1";
}

/** Everything the upload endpoint accepts. */
export const ACCEPTED_EXTS: readonly string[] = [".pdf"];
