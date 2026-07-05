import { readFile, writeFile } from "node:fs/promises";
import { basename } from "node:path";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ENGINES, LOCALES, MODES as MODE_VALUES } from "@pdf-converter/shared";
import { z } from "zod";

/**
 * MCP server (stdio) exposing the PDF Converter service as tools for local agents
 * (Claude Desktop / CLI). It is a thin client over the REST API — configure the
 * target with PDF_CONVERTER_URL and API_KEY.
 */

const BASE = process.env.PDF_CONVERTER_URL ?? "http://127.0.0.1:8000";
const API_KEY = process.env.API_KEY ?? "";

function headers(extra?: Record<string, string>): Record<string, string> {
  return { ...(API_KEY ? { "X-API-Key": API_KEY } : {}), ...extra };
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: headers(init?.headers as Record<string, string>),
  });
  if (!res.ok) throw new Error(`${path} -> ${res.status} ${await res.text()}`);
  return (await res.json()) as T;
}

function text(data: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
}

// Enum values come from the shared contract so a value added there can't silently
// go missing from this tool surface.
const MODES = z.array(z.enum(MODE_VALUES)).min(1);
const LOCALE = z.enum(LOCALES).default("zh-TW");
const ENGINE = z.enum(ENGINES).default("auto");

const server = new McpServer({ name: "pdf-converter", version: "1.0.0" });

server.tool(
  "submit_pdf",
  "Submit a local PDF file for OCR conversion. modes: pdf (searchable PDF), markdown, word. " +
    "engine pins how the markdown mode is produced: 'auto' (default) lets the server probe the " +
    "PDF and route it; 'docling' forces the exact text-layer extraction and requires a " +
    "born-digital PDF (the job errors on a scanned one); 'vl' forces the visual PaddleOCR-VL " +
    "model. engine applies to markdown only, so pass a non-'auto' value only when 'markdown' is " +
    "in modes. Returns job id(s) to poll.",
  { path: z.string(), modes: MODES, locale: LOCALE, engine: ENGINE },
  async ({ path, modes, locale, engine }) => {
    const fd = new FormData();
    fd.append("files", new Blob([await readFile(path)]), basename(path));
    for (const m of modes) fd.append("modes", m);
    fd.append("locale", locale);
    fd.append("engine", engine);
    const data = await api("/api/v1/jobs", { method: "POST", body: fd });
    return text(data);
  },
);

server.tool(
  "get_job",
  "Get a job's status, progress, and result.",
  { id: z.string() },
  async ({ id }) => text(await api(`/api/v1/jobs/${id}`)),
);

server.tool(
  "wait_for_job",
  "Poll a job until it finishes (done/error/cancelled) or the timeout elapses. Returns the final job.",
  { id: z.string(), timeout_seconds: z.number().int().positive().max(1800).default(600) },
  async ({ id, timeout_seconds }) => {
    const deadline = Date.now() + timeout_seconds * 1000;
    for (;;) {
      const job = await api<{ status: string }>(`/api/v1/jobs/${id}`);
      if (["done", "error", "cancelled"].includes(job.status) || Date.now() > deadline)
        return text(job);
      await new Promise((r) => setTimeout(r, 1500));
    }
  },
);

server.tool(
  "get_markdown",
  "Fetch the Markdown text of a completed markdown job.",
  { id: z.string() },
  async ({ id }) => text(await api(`/api/v1/preview/${id}`)),
);

server.tool(
  "download_result",
  "Download a completed job's artifact (pdf/zip/docx) to a local path.",
  { id: z.string(), out_path: z.string() },
  async ({ id, out_path }) => {
    const res = await fetch(`${BASE}/api/v1/download/${id}`, { headers: headers() });
    if (!res.ok) throw new Error(`download ${res.status}`);
    await writeFile(out_path, Buffer.from(await res.arrayBuffer()));
    return text({ saved: out_path, bytes: Number(res.headers.get("content-length") ?? 0) });
  },
);

server.tool("cancel_job", "Cancel a running job.", { id: z.string() }, async ({ id }) =>
  text(await api(`/api/v1/jobs/${id}/cancel`, { method: "POST" })),
);

const transport = new StdioServerTransport();
await server.connect(transport);
