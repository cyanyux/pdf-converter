// Download-route tests. These exercise the streaming download handlers, in
// particular the two error-handling fixes:
//  - fileResponse opens the file and takes content-length from the open handle's
//    fstat (so a GC race can't make the body shorter than the promised length);
//    a missing file (ENOENT at open) still yields a 404.
//  - the markdown-zip path narrows its catch to ENOENT -> 404 and rethrows any
//    other error (e.g. EACCES) so a real server fault surfaces as 500, not 404.
//
// config.outputsDir is resolved from PDF_CONVERTER_OUTPUTS at import time, so set it
// before importing anything that pulls in ./config.
import { chmodSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const OUTPUTS = mkdtempSync(join(tmpdir(), "pdfocr-out-"));
process.env.PDF_CONVERTER_OUTPUTS = OUTPUTS;

const { expect, test } = await import("vite-plus/test");
const { createApp } = await import("./app.ts");
const { JobStore } = await import("./db.ts");
const { ProgressHub } = await import("./sse.ts");

const SCHEMA = fileURLToPath(new URL("../../../db/schema.sql", import.meta.url));

type Store = InstanceType<typeof JobStore>;

function newApp(): { app: ReturnType<typeof createApp>; store: Store } {
  const dir = mkdtempSync(join(tmpdir(), "pdfocr-"));
  const store = new JobStore(join(dir, "t.db"), SCHEMA);
  return { app: createApp(store, new ProgressHub(store)), store };
}

/** Insert a `done` job directly (the worker normally does this via raw SQL). */
function insertDoneJob(
  store: Store,
  opts: { id: string; mode: string; downloadId: string; originalName?: string },
): void {
  const now = Date.now() / 1000;
  const result = JSON.stringify({
    totalPages: 1,
    downloadId: opts.downloadId,
    originalName: opts.originalName,
  });
  store.db
    .prepare(
      "INSERT INTO jobs(id,mode,filename,locale,status,download_id,result_json,created_at,updated_at) " +
        "VALUES(?,?,?,?,?,?,?,?,?)",
    )
    .run(opts.id, opts.mode, "doc.pdf", "zh-TW", "done", opts.downloadId, result, now, now);
}

test("GET /api/v1/download/:id (pdf) streams with a correct content-length", async () => {
  const { app, store } = newApp();
  const dl = "dl-pdf-1";
  mkdirSync(join(OUTPUTS, dl), { recursive: true });
  const body = Buffer.from("%PDF-1.4 hello world");
  writeFileSync(join(OUTPUTS, dl, `${dl}.pdf`), body);
  insertDoneJob(store, { id: "j1", mode: "pdf", downloadId: dl, originalName: "report.pdf" });

  const res = await app.request("/api/v1/download/j1");
  expect(res.status).toBe(200);
  expect(res.headers.get("content-type")).toBe("application/pdf");
  expect(res.headers.get("content-length")).toBe(String(body.length));
  const got = Buffer.from(await res.arrayBuffer());
  expect(got.length).toBe(body.length);
  expect(got.equals(body)).toBe(true);
});

test("HEAD /api/v1/download/:id (pdf) returns headers without a body (no fd leak)", async () => {
  // @hono/node-server discards a HEAD response's body WITHOUT draining it, so a
  // handle-backed stream would never autoClose — fileResponse must answer HEAD
  // headers-only, never opening a stream.
  const { app, store } = newApp();
  const dl = "dl-pdf-head";
  mkdirSync(join(OUTPUTS, dl), { recursive: true });
  const body = Buffer.from("%PDF-1.4 head probe");
  writeFileSync(join(OUTPUTS, dl, `${dl}.pdf`), body);
  insertDoneJob(store, { id: "jh", mode: "pdf", downloadId: dl, originalName: "r.pdf" });

  const res = await app.request("/api/v1/download/jh", { method: "HEAD" });
  expect(res.status).toBe(200);
  expect(res.headers.get("content-length")).toBe(String(body.length));
  expect(res.headers.get("content-type")).toBe("application/pdf");
  expect(res.body).toBeNull();
});

test("GET /api/v1/download/:id (pdf) is 404 when the output file is missing", async () => {
  const { app, store } = newApp();
  // done job points at a downloadId whose file was never written / was GC'd.
  insertDoneJob(store, { id: "j2", mode: "pdf", downloadId: "dl-missing" });
  const res = await app.request("/api/v1/download/j2");
  expect(res.status).toBe(404);
});

test("GET /api/v1/download/:id (markdown) zips the folder with human-readable names", async () => {
  const { unzipSync } = await import("fflate");
  const { app, store } = newApp();
  const dl = "dl-md-1";
  mkdirSync(join(OUTPUTS, dl, "imgs"), { recursive: true });
  // On disk the artifact is keyed by download id (worker contract)...
  writeFileSync(join(OUTPUTS, dl, `${dl}.md`), "# hi\n![](imgs/p1.png)");
  writeFileSync(join(OUTPUTS, dl, "imgs", "p1.png"), Buffer.from([0x89, 0x50]));
  insertDoneJob(store, { id: "j3", mode: "markdown", downloadId: dl, originalName: "報告.pdf" });
  const res = await app.request("/api/v1/download/j3");
  expect(res.status).toBe(200);
  expect(res.headers.get("content-type")).toBe("application/zip");
  // ...but the zip presents the document's name: one self-contained "-markdown" folder (the
  // suffix says what format is inside), md renamed to the document, images keeping their
  // relative path (so the md's refs still resolve).
  expect(res.headers.get("content-disposition")).toContain("-markdown.zip");
  const entries = Object.keys(unzipSync(new Uint8Array(await res.arrayBuffer())));
  expect(entries.sort()).toEqual(["報告-markdown/imgs/p1.png", "報告-markdown/報告.md"]);
});

test("HEAD /api/v1/download/:id (markdown) returns zip headers without building the zip", async () => {
  // Hono re-dispatches HEAD to the GET handler; the markdown branch must answer
  // headers-only rather than readdir + read-every-file + deflate (body discarded).
  const { app, store } = newApp();
  const dl = "dl-md-head";
  mkdirSync(join(OUTPUTS, dl, "imgs"), { recursive: true });
  writeFileSync(join(OUTPUTS, dl, `${dl}.md`), "# hi\n![](imgs/p1.png)");
  writeFileSync(join(OUTPUTS, dl, "imgs", "p1.png"), Buffer.from([0x89, 0x50]));
  insertDoneJob(store, { id: "j5", mode: "markdown", downloadId: dl, originalName: "報告.pdf" });
  const res = await app.request("/api/v1/download/j5", { method: "HEAD" });
  expect(res.status).toBe(200);
  expect(res.headers.get("content-type")).toBe("application/zip");
  expect(res.headers.get("content-disposition")).toContain("-markdown.zip");
  expect(res.body).toBeNull();
});

test("HEAD /api/v1/download/:id (markdown) is 404 when the output dir is missing", async () => {
  const { app, store } = newApp();
  // done job points at a downloadId whose dir was never written / was GC'd.
  insertDoneJob(store, { id: "j6", mode: "markdown", downloadId: "dl-md-missing" });
  const res = await app.request("/api/v1/download/j6", { method: "HEAD" });
  expect(res.status).toBe(404);
});

// The narrowing fix: a non-ENOENT error (EACCES on a permission-broken dir) must
// surface as a 500, not be masked as 404. Skipped under root, which bypasses the
// permission bits and would make readdir succeed.
const asRoot = typeof process.getuid === "function" && process.getuid() === 0;
test.skipIf(asRoot)(
  "GET /api/v1/download/:id (markdown) rethrows EACCES as 500, not 404",
  async () => {
    const { app, store } = newApp();
    const dl = "dl-md-eacces";
    const outDir = join(OUTPUTS, dl);
    mkdirSync(outDir, { recursive: true });
    writeFileSync(join(outDir, `${dl}.md`), "# hi");
    insertDoneJob(store, { id: "j4", mode: "markdown", downloadId: dl });
    chmodSync(outDir, 0o000); // existsSync still true, readdirSync throws EACCES
    try {
      const res = await app.request("/api/v1/download/j4");
      expect(res.status).toBe(500);
    } finally {
      chmodSync(outDir, 0o755); // restore so temp cleanup can proceed
    }
  },
);
