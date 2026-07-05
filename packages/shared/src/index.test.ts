import { expect, test } from "vite-plus/test";
import { isTerminal, Job, Mode } from "./index.ts";

test("Mode enum accepts valid modes, rejects others", () => {
  expect(Mode.parse("pdf")).toBe("pdf");
  expect(Mode.safeParse("nope").success).toBe(false);
});

test("isTerminal", () => {
  expect(isTerminal("done")).toBe(true);
  expect(isTerminal("error")).toBe(true);
  expect(isTerminal("cancelled")).toBe(true);
  expect(isTerminal("processing")).toBe(false);
  expect(isTerminal("queued")).toBe(false);
});

test("Job parses a full object", () => {
  const j = {
    id: "1",
    groupId: null,
    mode: "pdf",
    filename: "a.pdf",
    locale: "en",
    engine: "auto",
    status: "queued",
    attempts: 0,
    createdAt: 1,
    updatedAt: 1,
    heartbeatAt: null,
    progress: null,
    result: null,
    error: null,
  };
  expect(Job.parse(j).id).toBe("1");
  expect(Job.safeParse({ ...j, mode: "bogus" }).success).toBe(false);
});
