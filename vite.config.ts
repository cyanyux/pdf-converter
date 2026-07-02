import { defineConfig } from "vite-plus";

export default defineConfig({
  staged: {
    "*": "vp check --fix",
  },
  fmt: {
    // Legacy (deleted at cutover) + the Python worker (ruff owns Python) are
    // out of scope for Oxfmt.
    ignorePatterns: [
      "dist/**",
      "**/dist/**",
      "static/**",
      "worker/**",
      "*.py",
      "README.md",
      "PLAN-*.md",
      "cookies.txt",
    ],
  },
  lint: {
    ignorePatterns: ["dist/**", "**/dist/**", "static/**", "worker/**"],
    jsPlugins: [{ name: "vite-plus", specifier: "vite-plus/oxlint-plugin" }],
    rules: { "vite-plus/prefer-vite-plus-imports": "error" },
    options: { typeAware: true, typeCheck: true },
  },
  run: {
    cache: true,
  },
});
