import { z } from "zod";

/**
 * Shared contract types (Zod schemas) imported by BOTH the SPA and the Hono
 * server — the single source of truth for API payloads, with no codegen.
 * The SQLite row shapes mirrored on the Python side live in db/schema.sql;
 * these are the HTTP/JSON representations the browser and agents see.
 */

export const MODES = ["pdf", "markdown", "word"] as const;
export const Mode = z.enum(MODES);
export type Mode = z.infer<typeof Mode>;

export const LOCALES = ["zh-TW", "zh-CN", "en"] as const;
export const Locale = z.enum(LOCALES);
export type Locale = z.infer<typeof Locale>;

export const JOB_STATUSES = [
  "queued",
  "processing",
  "saving",
  "done",
  "error",
  "cancelled",
  "cancel_requested",
] as const;
export const JobStatus = z.enum(JOB_STATUSES);
export type JobStatus = z.infer<typeof JobStatus>;

/** Terminal states — polling/SSE stops once a job reaches one of these. */
export const TERMINAL_STATUSES: readonly JobStatus[] = ["done", "error", "cancelled"];
export function isTerminal(status: JobStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

export const Progress = z.object({
  current: z.number().int().nonnegative(),
  total: z.number().int().nonnegative(),
  percent: z.number().int().min(0).max(100),
  /** stage label (queued | processing | saving | ...) */
  status: z.string(),
  /** localized human-readable message produced by the worker */
  message: z.string(),
  /** epoch seconds */
  updatedAt: z.number(),
});
export type Progress = z.infer<typeof Progress>;

export const JobResult = z.object({
  totalPages: z.number().int().nonnegative(),
  /** artifact id used to build download/preview URLs */
  downloadId: z.string(),
  imagesCount: z.number().int().nonnegative().optional(),
  originalName: z.string().optional(),
  /** non-fatal warning (e.g. some pages failed) */
  warning: z.string().optional(),
  /** backend the worker routed this job to (surfaced as a chip in the UI) */
  engine: z.enum(["pp-ocrv6", "paddleocr-vl", "docling", "none"]).optional(),
  /** non-fatal informational notice (e.g. the source PDF was already searchable) */
  notice: z.enum(["already_searchable"]).optional(),
});
export type JobResult = z.infer<typeof JobResult>;

export const Job = z.object({
  id: z.string(),
  groupId: z.string().nullable(),
  mode: Mode,
  filename: z.string(),
  locale: Locale,
  status: JobStatus,
  attempts: z.number().int().nonnegative(),
  createdAt: z.number(),
  updatedAt: z.number(),
  heartbeatAt: z.number().nullable(),
  progress: Progress.nullable(),
  result: JobResult.nullable(),
  error: z.string().nullable(),
});
export type Job = z.infer<typeof Job>;

/** POST /api/v1/jobs — multipart body carries the files; these are the fields. */
export const CreateJobsFields = z.object({
  modes: z.array(Mode).min(1),
  locale: Locale.default("zh-TW"),
});
export type CreateJobsFields = z.infer<typeof CreateJobsFields>;

export const CreateJobsResponse = z.object({
  jobs: z.array(
    z.object({ id: z.string(), filename: z.string(), mode: Mode, groupId: z.string().nullable() }),
  ),
  skipped: z.array(z.object({ filename: z.string(), reason: z.string() })),
});
export type CreateJobsResponse = z.infer<typeof CreateJobsResponse>;

export const HealthResponse = z.object({
  status: z.enum(["ok", "degraded"]),
  worker: z.object({
    alive: z.boolean(),
    heartbeatAt: z.number().nullable(),
    activeModel: z.string().nullable(),
  }),
  gpu: z.record(z.string(), z.unknown()).nullable(),
  queueDepth: z.number().int().nonnegative(),
});
export type HealthResponse = z.infer<typeof HealthResponse>;

export const ErrorResponse = z.object({ error: z.string(), detail: z.string().optional() });
export type ErrorResponse = z.infer<typeof ErrorResponse>;
