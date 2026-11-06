/**
 * What a seller sees first.
 *
 * The first question is whether Biryar can reach their Amazon account: it is
 * the only thing that matters before any number exists, and it is the thing
 * a seller has to be told about on their own screen rather than discovering
 * because the figures quietly stopped moving. The profit figures follow it,
 * and only once it is answered.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError, type User } from "../api/client";
import { marketplaceName } from "../marketplaces";
import { Profit } from "./Profit";

export function Home({ user }: { user: User }) {
  const queries = useQueryClient();

  const connection = useQuery({
    queryKey: ["connection"],
    queryFn: () => api.connection(),
  });

  const connect = useMutation({
    mutationFn: () => api.startAuthorisation(),
    onSuccess: ({ url }) => {
      // Amazon's consent page, which is on Amazon rather than here.
      window.location.href = url;
    },
  });

  // Self-authorisation: the only route that works for a private Draft
  // application, whose consent URL answers MD9100 because it has no OAuth
  // configuration at all. Offered beside the OAuth button rather than
  // instead of it, because a hosted deployment needs the other one.
  const [sellerId, setSellerId] = useState("");
  const [token, setToken] = useState("");
  const paste = useMutation({
    mutationFn: () =>
      api.storeCredential({
        marketplace: connection.data?.marketplace,
        selling_partner_id: sellerId.trim(),
        refresh_token: token.trim(),
      }),
    onSuccess: () => {
      // Never kept around once it has been sealed server-side.
      setToken("");
      void queries.invalidateQueries({ queryKey: ["connection"] });
    },
  });

  const signOut = useMutation({
    mutationFn: api.signOut,
    onSettled: () => queries.invalidateQueries(),
  });

  return (
    <main className="page">
      <header className="bar">
        <div>
          <strong>Biryar</strong>
          <span className="muted"> · {user.display_name}</span>
        </div>
        <button
          className="link"
          onClick={() => signOut.mutate()}
          disabled={signOut.isPending}
        >
          Sign out
        </button>
      </header>

      <section className="card">
        <h2>Amazon</h2>

        {connection.isLoading && <p className="muted">Checking…</p>}

        {connection.error && (
          <p className="error" role="alert">
            {connection.error instanceof ApiError
              ? connection.error.message
              : "Could not check the connection."}
          </p>
        )}

        {connection.data && !connection.data.connected && (
          <>
            <p>
              No Amazon account is connected yet, so there is nothing to
              measure.
            </p>
            <button
              onClick={() => connect.mutate()}
              disabled={connect.isPending}
            >
              {connect.isPending ? "Opening Amazon…" : "Connect Amazon"}
            </button>
            {connect.error && (
              <p className="error" role="alert">
                {connect.error instanceof ApiError
                  ? connect.error.message
                  : "Could not start the connection."}
              </p>
            )}

            {user.role === "owner" && (
              <details>
                <summary className="small">
                  Self-hosting? Paste a refresh token instead
                </summary>
                <p className="small muted">
                  A private application in Draft has no OAuth setup, so the
                  button above will answer <code>MD9100</code>. Authorise the
                  application against your own account in the Solution
                  Provider Portal and paste what it gives you. It is sealed
                  here and never shown again.
                </p>
                <p className="small">
                  <label>
                    Merchant token{" "}
                    <input
                      value={sellerId}
                      onChange={(e) => setSellerId(e.target.value)}
                      placeholder="A1..."
                    />
                  </label>
                </p>
                <p className="small">
                  <label>
                    Refresh token{" "}
                    <input
                      type="password"
                      value={token}
                      onChange={(e) => setToken(e.target.value)}
                      placeholder="Atzr|..."
                    />
                  </label>
                </p>
                <button
                  onClick={() => paste.mutate()}
                  disabled={
                    paste.isPending || !sellerId.trim() || !token.trim()
                  }
                >
                  {paste.isPending ? "Saving…" : "Save credential"}
                </button>
                {paste.error && (
                  <p className="error" role="alert">
                    {paste.error instanceof ApiError
                      ? paste.error.message
                      : "Could not save the credential."}
                  </p>
                )}
              </details>
            )}
          </>
        )}

        {connection.data?.connected && (
          <dl className="facts">
            <dt>Marketplace</dt>
            <dd>{marketplaceName(connection.data.marketplace)}</dd>
            <dt>Seller</dt>
            <dd>{connection.data.selling_partner_id ?? "—"}</dd>
            <dt>Status</dt>
            <dd>{connection.data.status ?? "—"}</dd>
            {connection.data.last_error && (
              <>
                <dt>Last problem</dt>
                <dd className="error">{connection.data.last_error}</dd>
              </>
            )}
          </dl>
        )}
      </section>

      {/* Below the connection, because until Amazon is connected there is
          nothing for it to measure and the empty state would be confusing
          rather than informative. */}
      {connection.data?.connected && <Profit user={user} />}

      <p className="muted small">
        Signed in as {user.email} · {user.role}
      </p>
    </main>
  );
}
