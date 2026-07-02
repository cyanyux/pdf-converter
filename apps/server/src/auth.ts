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
export function apiKeyAuth(): MiddlewareHandler {
  const key = config.apiKey;
  return async (c, next) => {
    if (!key) return next();
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
 * Fail fast if the server would expose a non-localhost interface without a key.
 * Reconciles "auth off by default" with the tunnel-exposure risk.
 */
export function assertSafeBind(): void {
  if (isExposedBind(config.host) && !config.apiKey) {
    throw new Error(
      `Refusing to bind ${config.host} without API_KEY set. ` +
        "Set API_KEY to expose the service, or keep HOST=127.0.0.1 behind a reverse proxy.",
    );
  }
}
