import { mkdirSync } from "node:fs";
import { serve } from "@hono/node-server";
import { serveStatic } from "@hono/node-server/serve-static";
import type { HttpBindings } from "@hono/node-server";
import { Hono } from "hono";
import { createApp } from "./app.ts";
import { warnIfInsecureBind } from "./auth.ts";
import { config } from "./config.ts";
import { JobStore } from "./db.ts";
import { ProgressHub } from "./sse.ts";

warnIfInsecureBind();
for (const dir of [config.uploadsDir, config.outputsDir]) {
  mkdirSync(dir, { recursive: true });
}

const store = new JobStore();
const hub = new ProgressHub(store);

const app = new Hono<{ Bindings: HttpBindings }>();
app.route("/", createApp(store, hub));
// Production: serve the built SPA + client-side-routing fallback. In dev the
// Vite dev server owns the SPA and proxies /api here, so these are inert.
app.use("/*", serveStatic({ root: config.staticDir }));
app.get("*", serveStatic({ path: "index.html", root: config.staticDir }));

const server = serve({ fetch: app.fetch, port: config.port, hostname: config.host }, (info) => {
  // eslint-disable-next-line no-console
  console.log(
    `[server] http://${config.host}:${info.port}  (api-key ${config.apiKey ? "on" : "off"})`,
  );
});

function shutdown(): void {
  server.close();
  store.close();
  process.exit(0);
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
