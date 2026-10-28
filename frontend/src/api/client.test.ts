import { it, expect, vi, afterEach } from "vitest";
import { api, resetSession, onExpired } from "./client";
afterEach(() => {
  vi.unstubAllGlobals();
  resetSession();
});
it("rejects a body that finishes parsing after the account has changed", async () => {
  let resolve!: (value: unknown) => void;
  const body = new Promise((r) => {
    resolve = r;
  });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => body }),
  );
  const pending = api("/auth/me");
  await Promise.resolve();
  await Promise.resolve();
  resetSession();
  resolve({ user_id: "old-account" });
  await expect(pending).rejects.toMatchObject({ name: "AbortError" });
});
it("an old 401 body cannot log out a newly established account", async () => {
  let resolve!: (value: unknown) => void;
  const body = new Promise((r) => {
    resolve = r;
  });
  const expired = vi.fn();
  const dispose = onExpired(expired);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: false, status: 401, json: () => body }),
  );
  const pending = api("/auth/me");
  await Promise.resolve();
  await Promise.resolve();
  resetSession();
  resolve({ error: { code: "identity_expired" } });
  await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  expect(expired).not.toHaveBeenCalled();
  dispose();
});
