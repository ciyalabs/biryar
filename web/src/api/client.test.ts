/**
 * The API client.
 *
 * `fetch` is stubbed. What is under test is how this reacts to the server's
 * answers — a 401 clearing the session, a network failure not being
 * mistaken for a rejection — and those are the paths a real server will not
 * produce on demand.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, session } from "./client";

function respond(status: number, body?: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  session.clear();
  vi.restoreAllMocks();
});

afterEach(() => {
  session.clear();
});

describe("signing in", () => {
  it("keeps the token so later calls carry it", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        respond(200, {
          token: "a-token",
          expires_at: "2026-11-06T00:00:00Z",
          user: {
            id: "1",
            email: "owner@example.com",
            display_name: "Owner",
            role: "owner",
          },
        }),
      )
      .mockResolvedValueOnce(
        respond(200, {
          id: "1",
          email: "owner@example.com",
          display_name: "Owner",
          role: "owner",
        }),
      );

    await api.signIn({ email: "owner@example.com", password: "x" });
    expect(session.isSignedIn).toBe(true);

    await api.me();
    const headers = fetchMock.mock.calls[1]![1]!.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer a-token");
  });

  it("names the tenant when asked to", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(200, {
        token: "t",
        expires_at: "2026-11-06T00:00:00Z",
        user: { id: "1", email: "a@example.com", display_name: "A", role: "owner" },
      }),
    );

    await api.signIn({ email: "a@example.com", password: "x" }, "acme");

    const headers = fetchMock.mock.calls[0]![1]!.headers as Headers;
    expect(headers.get("X-Biryar-Tenant")).toBe("acme");
  });

  it("reports the server's reason rather than a generic one", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(401, { detail: "Those credentials do not match an account." }),
    );

    await expect(
      api.signIn({ email: "a@example.com", password: "wrong" }),
    ).rejects.toThrow("Those credentials do not match an account.");
    expect(session.isSignedIn).toBe(false);
  });
});

describe("a session that has gone", () => {
  it("is cleared on the first 401, so the app stops pretending", async () => {
    session.set("stale");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(401, { detail: "Not signed in." }),
    );

    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(session.isSignedIn).toBe(false);
  });

  it("is cleared locally even when sign-out itself fails", async () => {
    // The one moment someone is most certain they have left.
    session.set("a-token");
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));

    await expect(api.signOut()).rejects.toBeInstanceOf(ApiError);
    expect(session.isSignedIn).toBe(false);
  });
});

describe("the network", () => {
  it("is not reported as a rejection by the server", async () => {
    // Otherwise someone concludes their password is wrong when their wifi is.
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));

    const error = await api.health().catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
    expect((error as ApiError).isUnauthenticated).toBe(false);
    expect((error as ApiError).message).toMatch(/could not reach/i);
  });
});

describe("a 204", () => {
  it("is not parsed as JSON", async () => {
    session.set("a-token");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(null, { status: 204 }),
    );

    await expect(api.signOut()).resolves.toBeUndefined();
  });
});
