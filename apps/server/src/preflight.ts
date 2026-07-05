import { open, readFile, stat } from "node:fs/promises";
import { PDFDocument, ParseSpeeds } from "pdf-lib";
import { config } from "./config.ts";

export type PreflightReason =
  | "not_pdf"
  | "empty_pdf"
  | "encrypted_pdf"
  | "invalid_pdf"
  | "truncated";

export interface PreflightResult {
  ok: boolean;
  reason?: PreflightReason;
  pages?: number;
}

/** Bytes scanned from the tail of an oversized PDF for the trailer's `/Encrypt` reference. */
const ENCRYPT_SCAN_TAIL_BYTES = 64 * 1024;

/**
 * Validate an uploaded PDF cheaply before enqueuing so agents/UI get a fast,
 * synchronous 400 instead of a slow async job error: check the %PDF- magic by
 * reading only the first bytes, then — for files up to preflightMaxBytes — a
 * structural parse for encryption + page count. Larger PDFs are accepted
 * unparsed (the worker validates them) so pdf-lib's full in-memory parse can't
 * OOM the request handler. (Encrypted PDFs throw during the parse path.)
 */
export async function preflightPdf(path: string): Promise<PreflightResult> {
  let size: number;
  let fh;
  try {
    ({ size } = await stat(path));
    fh = await open(path);
  } catch {
    return { ok: false, reason: "invalid_pdf" };
  }
  try {
    const { bytesRead, buffer } = await fh.read(Buffer.alloc(5), 0, 5, 0);
    if (bytesRead < 5 || buffer.toString("latin1") !== "%PDF-") {
      return { ok: false, reason: "not_pdf" };
    }
    // Skip the pdf-lib structural parse for very large files: it reads the entire PDF into
    // memory, so a 500MB upload (x concurrent requests) could exhaust it. Still do a cheap,
    // bounded encryption check — an encrypted PDF carries `/Encrypt` in its trailer (near
    // EOF), either as an indirect ref (`/Encrypt 5 0 R`) or an inline dict (`/Encrypt<<…>>`),
    // so scan only the tail (O(tail) memory) to keep the fast synchronous `encrypted_pdf`
    // rejection the small-file path gives. The value form avoids the `/EncryptMetadata`
    // false-friend; a miss still fails in the worker.
    if (size > config.preflightMaxBytes) {
      const tailLen = Math.min(size, ENCRYPT_SCAN_TAIL_BYTES);
      const tail = Buffer.alloc(tailLen);
      await fh.read(tail, 0, tailLen, size - tailLen);
      if (/\/Encrypt\s*(?:<<|\d+\s+\d+\s+R)/.test(tail.toString("latin1"))) {
        return { ok: false, reason: "encrypted_pdf" };
      }
      return { ok: true };
    }
  } finally {
    await fh.close();
  }
  let buf: Buffer;
  try {
    buf = await readFile(path);
  } catch {
    return { ok: false, reason: "invalid_pdf" };
  }
  try {
    // Read-only preflight: parse fast and don't rewrite the Info dict. ignoreEncryption stays
    // false so an encrypted PDF still throws and is classified below.
    const doc = await PDFDocument.load(buf, {
      ignoreEncryption: false,
      parseSpeed: ParseSpeeds.Fastest,
      updateMetadata: false,
    });
    const pages = doc.getPageCount();
    if (pages === 0) return { ok: false, reason: "empty_pdf" };
    return { ok: true, pages };
  } catch (e) {
    const msg = String(e instanceof Error ? e.message : e).toLowerCase();
    if (msg.includes("encrypt")) return { ok: false, reason: "encrypted_pdf" };
    return { ok: false, reason: "invalid_pdf" };
  }
}
