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
