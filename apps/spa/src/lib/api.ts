import type { CreateJobsResponse, HealthResponse, Job, Locale, Mode } from "@pdf-ocr/shared";

const BASE = "/api/v1";
const API_KEY_STORAGE = "pdfOcrApiKey";

function authHeaders(): Record<string, string> {
  const key = localStorage.getItem(API_KEY_STORAGE);
  return key ? { "X-API-Key": key } : {};
}

export async function createJobs(
  files: File[],
  modes: Mode[],
  locale: Locale,
): Promise<CreateJobsResponse> {
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  for (const m of modes) fd.append("modes", m);
  fd.append("locale", locale);
  const res = await fetch(`${BASE}/jobs`, { method: "POST", body: fd, headers: authHeaders() });
  if (!res.ok) throw new Error(`server error ${res.status}`);
  return (await res.json()) as CreateJobsResponse;
}

export async function getJob(id: string): Promise<Job | null> {
  const res = await fetch(`${BASE}/jobs/${id}?_t=${Date.now()}`, {
    cache: "no-store",
    headers: authHeaders(),
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`job ${res.status}`);
  return (await res.json()) as Job;
}

export async function cancelJob(id: string): Promise<void> {
  await fetch(`${BASE}/jobs/${id}/cancel`, { method: "POST", headers: authHeaders() });
}

export async function deleteJob(id: string): Promise<void> {
  await fetch(`${BASE}/jobs/${id}`, { method: "DELETE", headers: authHeaders() });
}

export function downloadUrl(job: Job): string {
  const name = job.result?.originalName
    ? `?name=${encodeURIComponent(job.result.originalName)}`
    : "";
  return `${BASE}/download/${job.id}${name}`;
}

export function downloadFilename(job: Job): string {
  const base = job.result?.originalName?.replace(/\.[^./]+$/, "") ?? job.id;
  if (job.mode === "pdf") return job.result?.originalName ?? `${base}.pdf`;
  return `${base}.${job.mode === "markdown" ? "zip" : "docx"}`;
}

export async function fetchPreview(id: string): Promise<{ content: string; filename: string }> {
  const res = await fetch(`${BASE}/preview/${id}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`preview ${res.status}`);
  return (await res.json()) as { content: string; filename: string };
}

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch(`${BASE}/health`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`health ${res.status}`);
  return (await res.json()) as HealthResponse;
}

/** Trigger a browser download for a completed job. */
export function triggerDownload(job: Job): void {
  const a = document.createElement("a");
  a.href = downloadUrl(job);
  a.download = downloadFilename(job);
  document.body.appendChild(a);
  a.click();
  a.remove();
}
