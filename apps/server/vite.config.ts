import { defineConfig } from "vite-plus";

// The Hono server is bundled with tsdown (`vp pack`) → dist/index.mjs, run with
// `node dist/index.mjs`. Node built-ins (node:sqlite, node:fs) and node_modules
// deps are externalized by tsdown; the runtime image ships production deps.
export default defineConfig({
  pack: {
    entry: ["src/index.ts"],
    format: ["esm"],
    dts: false,
    sourcemap: true,
  },
  fmt: {},
  lint: {
    options: { typeAware: true, typeCheck: true },
  },
});
