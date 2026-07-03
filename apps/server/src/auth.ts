import { timingSafeEqual } from "node:crypto";
import type { MiddlewareHandler } from "hono";
import { config, isExposedBind } from "./config.ts";

function safeEqual(a: string, b: string): boolean {
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  if (ab.length !== bb.length) return false;
  return timingSafeEqual(ab, bb);
}

/**
 * Bearer / X-API-Key auth. Open when API_KEY is unset (local dev); enforced
 * otherwise. Protects both the REST API and MCP.
 */
export function apiKeyAuth(publicPaths: readonly string[] = []): MiddlewareHandler {
  const key = config.apiKey;
  const publicSet = new Set(publicPaths);
  return async (c, next) => {
    if (!key) return next();
    // Unauthenticated allowlist (the health probe). The container HEALTHCHECK curls
    // /api/v1/health with no credentials, so gating it would mark a perfectly healthy
    // container unhealthy the moment API_KEY is set.
    if (publicSet.has(c.req.path)) return next();
    const header = c.req.header("authorization") ?? "";
    const bearer = header.toLowerCase().startsWith("bearer ") ? header.slice(7).trim() : "";
    const provided = bearer || c.req.header("x-api-key") || "";
    if (!provided || !safeEqual(provided, key)) {
      return c.json({ error: "unauthorized" }, 401);
    }
    return next();
  };
}

/**
 * Warn loudly if the server exposes a non-localhost interface without a key.
 * (Containers bind 0.0.0.0 by design, so this warns rather than refuses; put the
 * service behind a reverse proxy / access control, or set API_KEY.)
 */
export function warnIfInsecureBind(): void {
  if (isExposedBind(config.host) && !config.apiKey) {
    // eslint-disable-next-line no-console
    console.warn(
      `[server] WARNING: bound to ${config.host} without API_KEY — the API is unauthenticated. ` +
        "Set API_KEY or restrict access via a reverse proxy / network policy.",
    );
  }
}
