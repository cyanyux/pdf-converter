# CLAUDE.md

See [AGENTS.md](AGENTS.md) for full guidance — architecture, the Vite+ `vp`
toolchain, and how to drive the OCR service via MCP or the REST API.

Quick reference:

- `vp check` — format + lint + type-check (run before committing); `vp test` — tests; `vp dev` — SPA + API.
- Python worker (`worker/`): `ruff check worker/src`, `mypy`, `pytest`.
- OCR is async: `POST /api/v1/jobs` → poll `GET /api/v1/jobs/{id}` → `GET /api/v1/download/{id}`.
  Machine-readable schema at `/openapi.json`; MCP server at `apps/server/src/mcp-stdio.ts`.
- SQLite schema `db/schema.sql` is the TS↔Python contract; shared Zod types in `packages/shared`.
