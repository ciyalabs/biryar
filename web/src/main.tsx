/**
 * Entry point.
 *
 * Two providers and nothing else. React Query handles the one genuinely
 * hard part of a UI over an API — caching, refetching, and the fact that
 * every request can be loading, failed or stale — so pages can describe
 * what they show rather than managing it.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { ApiError } from "./api/client";
import "./index.css";

const queries = new QueryClient({
  defaultOptions: {
    queries: {
      // Retrying a 401 cannot help: the session is gone, and three more
      // attempts only delay the sign-in screen.
      retry: (failures, error) =>
        error instanceof ApiError && error.status < 500 ? false : failures < 2,
      staleTime: 30_000,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queries}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
