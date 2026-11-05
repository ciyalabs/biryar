/**
 * Routing, and the gate.
 *
 * `/me` is the single source of truth for whether someone is signed in. A
 * token in storage is not evidence — it may be expired or revoked, and the
 * only way to know is to ask. So the app asks once on load and shows the
 * sign-in screen if the answer is no.
 */

import { useQuery } from "@tanstack/react-query";
import { Navigate, Route, Routes } from "react-router-dom";

import { api, ApiError, session } from "./api/client";
import { Home } from "./pages/Home";
import { SignIn } from "./pages/SignIn";

export function App() {
  const me = useQuery({
    queryKey: ["me"],
    queryFn: api.me,
    enabled: session.isSignedIn,
  });

  if (session.isSignedIn && me.isLoading) {
    return <p className="centred muted">Checking your session…</p>;
  }

  const signedIn =
    session.isSignedIn &&
    me.data !== undefined &&
    !(me.error instanceof ApiError && me.error.isUnauthenticated);

  return (
    <Routes>
      <Route
        path="/sign-in"
        element={signedIn ? <Navigate to="/" replace /> : <SignIn />}
      />
      <Route
        path="/"
        element={
          signedIn && me.data ? (
            <Home user={me.data} />
          ) : (
            <Navigate to="/sign-in" replace />
          )
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
