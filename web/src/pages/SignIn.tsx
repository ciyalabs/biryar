/**
 * Signing in.
 *
 * The tenant field appears only when the server says it cannot work out
 * which tenant is meant — a self-hosted install has one and never asks. The
 * alternative, always asking, makes every single-tenant user answer a
 * question that has only one possible answer.
 */

import { useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../api/client";

export function SignIn() {
  const queries = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenant, setTenant] = useState("");
  const [needsTenant, setNeedsTenant] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.signIn({ email, password }, tenant || undefined);
      await queries.invalidateQueries();
    } catch (caught) {
      if (caught instanceof ApiError) {
        // 404 means the server could not identify the tenant, which is the
        // one failure the person can actually do something about here.
        if (caught.status === 404) setNeedsTenant(true);
        setError(caught.message);
      } else {
        setError("Something went wrong.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="centred">
      <form className="card" onSubmit={submit}>
        <h1>Biryar</h1>
        <p className="muted">Amazon operations, one decision at a time.</p>

        <label>
          Email
          <input
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>

        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>

        {needsTenant && (
          <label>
            Account
            <input
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
              placeholder="your-company"
            />
          </label>
        )}

        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
