import { open, readFile } from "node:fs/promises";
import { PDFDocument } from "pdf-lib";

export type PreflightReason =
  | "not_pdf"
  | "empty_pdf"
  | "encrypted_pdf"
  | "invalid_pdf"
  | "invalid_office"
  | "office_needs_markdown"
  | "truncated";

export interface PreflightResult {
  ok: boolean;
  reason?: PreflightReason;
  pages?: number;
}

/**
 * Validate an uploaded PDF cheaply before enqueuing so agents/UI get a fast,
 * synchronous 400 instead of a slow async job error: magic bytes, then a
 * structural parse for encryption + page count. (Encrypted PDFs throw here.)
 */
export async function preflightPdf(path: string): Promise<PreflightResult> {
  let buf: Buffer;
  try {
    buf = await readFile(path);
  } catch {
    return { ok: false, reason: "invalid_pdf" };
  }
  if (buf.length < 5 || buf.subarray(0, 5).toString("latin1") !== "%PDF-") {
    return { ok: false, reason: "not_pdf" };
  }
  try {
    const doc = await PDFDocument.load(buf, { ignoreEncryption: false });
    const pages = doc.getPageCount();
    if (pages === 0) return { ok: false, reason: "empty_pdf" };
    return { ok: true, pages };
  } catch (e) {
    const msg = String(e instanceof Error ? e.message : e).toLowerCase();
    if (msg.includes("encrypt")) return { ok: false, reason: "encrypted_pdf" };
    return { ok: false, reason: "invalid_pdf" };
  }
}

/**
 * Validate an Office upload (docx/xlsx/pptx) by its ZIP container magic (PK\x03\x04),
 * reading only the first bytes rather than the whole file.
 */
export async function preflightOffice(path: string): Promise<PreflightResult> {
  let fh;
  try {
    fh = await open(path);
  } catch {
    return { ok: false, reason: "invalid_office" };
  }
  try {
    const { bytesRead, buffer } = await fh.read(Buffer.alloc(4), 0, 4, 0);
    if (bytesRead >= 2 && buffer[0] === 0x50 && buffer[1] === 0x4b) return { ok: true };
    return { ok: false, reason: "invalid_office" };
  } finally {
    await fh.close();
  }
}
