/**
 * The one thing this screen must never do.
 *
 * An unpriced unit has no cost on record. Treating it as free produces a
 * profit figure that is too high by exactly the cost of the goods, and it
 * looks entirely plausible — there is no error, no empty cell, just a
 * number that is wrong in the direction a seller wants to believe. So the
 * API returns null, and this asserts the screen prints no figure for it.
 *
 * The complement matters as much: gross contribution needs nothing the
 * seller supplies, so it must still be shown. A screen that hid both would
 * be safe and useless.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { api, type User } from "../api/client";
import { Profit } from "./Profit";

const OWNER: User = {
  email: "owner@example.com",
  display_name: "Owner",
  role: "owner",
} as User;

function shown(node: React.ReactElement) {
  const queries = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queries}>{node}</QueryClientProvider>,
  );
}

const SOLD_ONE = {
  start: "2026-09-01",
  end: "2026-09-30",
  units: 1,
  orders: 1,
  revenue: "20.00",
  selling_fees: "-3.00",
  postage: "-0.57",
  overhead: "0",
  gross_contribution: "16.43",
  latency_ms: 1,
};

describe("the profit screen", () => {
  it("prints no net profit while a sold unit is unpriced", async () => {
    vi.spyOn(api, "profit").mockResolvedValue({
      ...SOLD_ONE,
      cogs: null,
      net_profit: null,
      units_without_cost: 1,
      skus_missing_cost: ["SKU-UNPRICED"],
    } as Awaited<ReturnType<typeof api.profit>>);
    vi.spyOn(api, "profitBySku").mockResolvedValue({
      rows: [],
      latency_ms: 1,
    } as Awaited<ReturnType<typeof api.profitBySku>>);

    shown(<Profit user={OWNER} />);

    expect(await screen.findByText("not yet knowable")).toBeInTheDocument();
    // Still answered, because it needs nothing the seller supplies.
    expect(screen.getByText("€16.43")).toBeInTheDocument();
    // And the list that turns the other figure on.
    expect(screen.getByText("SKU-UNPRICED")).toBeInTheDocument();
  });

  it("prints the net profit once everything sold has a cost", async () => {
    vi.spyOn(api, "profit").mockResolvedValue({
      ...SOLD_ONE,
      cogs: "-8.00",
      net_profit: "8.4300",
      units_without_cost: 0,
      skus_missing_cost: [],
    } as Awaited<ReturnType<typeof api.profit>>);
    vi.spyOn(api, "profitBySku").mockResolvedValue({
      rows: [],
      latency_ms: 1,
    } as Awaited<ReturnType<typeof api.profitBySku>>);

    shown(<Profit user={OWNER} />);

    // Four decimal places on the wire, two on the screen: money is not
    // rounded by the API, so the formatting is this layer's job.
    expect(await screen.findByText("€8.43")).toBeInTheDocument();
    expect(screen.queryByText("not yet knowable")).not.toBeInTheDocument();
  });

  it("offers no cost import to an assistant", async () => {
    vi.spyOn(api, "profit").mockResolvedValue({
      ...SOLD_ONE,
      cogs: null,
      net_profit: null,
      units_without_cost: 1,
      skus_missing_cost: ["SKU-1"],
    } as Awaited<ReturnType<typeof api.profit>>);
    vi.spyOn(api, "profitBySku").mockResolvedValue({
      rows: [],
      latency_ms: 1,
    } as Awaited<ReturnType<typeof api.profitBySku>>);

    shown(<Profit user={{ ...OWNER, role: "assistant" } as User} />);

    await screen.findByText("not yet knowable");
    // Hiding the control is a courtesy, not the gate: the server refuses an
    // assistant's import with a 403 either way, and test_api_profit.py is
    // what proves that.
    expect(screen.queryByText(/Import a cost file/)).not.toBeInTheDocument();
  });
});
