/**
 * The one place this application talks to the server.
 *
 * Every type here comes from `schema.d.ts`, which is generated from the
 * API's own OpenAPI document. Nothing is hand-written, so a route that
 * changes shape breaks the type check here rather than breaking a page at
 * runtime in front of someone.
 *
 * ## Where the token lives
 *
 * In memory, and in `sessionStorage` so a refresh does not sign you out.
 * Not `localStorage`: that persists until explicitly cleared, across
 * windows and indefinitely, which for a bearer token on a shared or
 * borrowed machine means staying signed in long after the person thought
 * they had left.
 *
 * Neither is proof against a script running on this origin. The real
 * defence against that is not storing it differently; it is the server
 * keeping sessions short and revocable, which it does.
 */

import type { paths } from "./schema";

type Json = Record<string, unknown>;

export class ApiError extends Error {
  // Written out rather than as constructor parameter properties: the
  // template enables `erasableSyntaxOnly`, so the TypeScript here has to be
  // removable without transforming anything, and parameter properties
  // generate assignments that are not.
  readonly status: number;
  readonly body?: Json;

  constructor(status: number, message: string, body?: Json) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }

  /** Whether this means "sign in again" rather than "something went wrong". */
  get isUnauthenticated(): boolean {
    return this.status === 401;
  }
}

const TOKEN_KEY = "biryar.session";

function readToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    // Private browsing, or storage disabled. The app still works for this
    // tab; it just cannot survive a refresh.
    return null;
  }
}

function writeToken(token: string | null): void {
  try {
    if (token === null) sessionStorage.removeItem(TOKEN_KEY);
    else sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* see readToken */
  }
}

let token: string | null = readToken();

export const session = {
  get token(): string | null {
    return token;
  },
  set(value: string): void {
    token = value;
    writeToken(value);
  },
  clear(): void {
    token = null;
    writeToken(null);
  },
  get isSignedIn(): boolean {
    return token !== null;
  },
};

/**
 * The API's base URL.
 *
 * Empty by default, so requests are same-origin and a self-hosted install
 * needs no configuration at all. A hosted deployment serving the front end
 * from somewhere else sets VITE_API_URL at build time.
 */
const BASE = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");

async function request<T>(
  path: string,
  init: RequestInit & { tenant?: string } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  if (token !== null) headers.set("Authorization", `Bearer ${token}`);
  if (init.tenant) headers.set("X-Biryar-Tenant", init.tenant);

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, { ...init, headers });
  } catch (cause) {
    // A network failure is not a rejection by the server, and saying so
    // stops someone concluding their password is wrong when their wifi is.
    throw new ApiError(0, "Could not reach Biryar. Check your connection.", {
      cause: String(cause),
    });
  }

  if (response.status === 204) return undefined as T;

  let body: Json | undefined;
  try {
    body = (await response.json()) as Json;
  } catch {
    body = undefined;
  }

  if (!response.ok) {
    const detail =
      typeof body?.detail === "string" ? body.detail : response.statusText;
    if (response.status === 401) session.clear();
    throw new ApiError(response.status, detail, body);
  }

  return body as T;
}

// -- the calls, typed from the document -------------------------------------

type SignInBody =
  paths["/v1/auth/sign-in"]["post"]["requestBody"]["content"]["application/json"];
export type SignInResult =
  paths["/v1/auth/sign-in"]["post"]["responses"]["200"]["content"]["application/json"];
export type User =
  paths["/v1/auth/me"]["get"]["responses"]["200"]["content"]["application/json"];
export type Health =
  paths["/v1/health"]["get"]["responses"]["200"]["content"]["application/json"];
export type Connection =
  paths["/v1/amazon/status"]["get"]["responses"]["200"]["content"]["application/json"];
export type AuthorisationStart =
  paths["/v1/amazon/authorize"]["post"]["responses"]["200"]["content"]["application/json"];

export const api = {
  health: () => request<Health>("/v1/health"),

  async signIn(body: SignInBody, tenant?: string): Promise<SignInResult> {
    const result = await request<SignInResult>("/v1/auth/sign-in", {
      method: "POST",
      body: JSON.stringify(body),
      tenant,
    });
    session.set(result.token);
    return result;
  },

  async signOut(): Promise<void> {
    try {
      await request<void>("/v1/auth/sign-out", { method: "POST" });
    } finally {
      // Clear locally whatever the server said. A sign-out that leaves the
      // token in place because the request failed is the one case where
      // the user is most sure they have left.
      session.clear();
    }
  },

  me: () => request<User>("/v1/auth/me"),

  connection: (marketplace?: string) =>
    request<Connection>(
      `/v1/amazon/status${marketplace ? `?marketplace=${marketplace}` : ""}`,
    ),

  startAuthorisation: (marketplace?: string) =>
    request<AuthorisationStart>(
      `/v1/amazon/authorize${marketplace ? `?marketplace=${marketplace}` : ""}`,
      { method: "POST" },
    ),
};
