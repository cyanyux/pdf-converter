import { expect, test } from "vite-plus/test";
import { ApiError } from "./api.ts";

test("ApiError carries the HTTP status and is an Error", () => {
  const err = new ApiError("job 401", 401);
  expect(err).toBeInstanceOf(Error);
  expect(err).toBeInstanceOf(ApiError);
  expect(err.status).toBe(401);
  expect(err.name).toBe("ApiError");
  expect(err.message).toBe("job 401");
});

test("ApiError lets callers distinguish auth rejections from other failures", () => {
  const isAuth = (e: unknown) => e instanceof ApiError && (e.status === 401 || e.status === 403);
  expect(isAuth(new ApiError("job 401", 401))).toBe(true);
  expect(isAuth(new ApiError("job 403", 403))).toBe(true);
  expect(isAuth(new ApiError("job 500", 500))).toBe(false);
  // A network failure surfaces as a plain Error (fetch reject), not an ApiError.
  expect(isAuth(new TypeError("Failed to fetch"))).toBe(false);
});
