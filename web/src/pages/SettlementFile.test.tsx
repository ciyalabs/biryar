/**
 * The two things this screen must never soften.
 *
 * A file that does not reconcile, and a fee that was not recognised, are
 * both reasons to distrust a figure. Amazon publishes this report's column
 * names and not its fee vocabulary, so both are expected rather than
 * exceptional — and a screen that showed a confident number anyway would be
 * the most expensive thing in the product.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { api, type SettlementFileReport } from "../api/client";
import { SettlementFile } from "./SettlementFile";

const CLEAN: SettlementFileReport = {
  settlement_id: "555",
  period_start: "2026-09-01",
  period_end: "2026-09-15",
  deposit_date: "2026-09-17",
  currency: "EUR",
  stated_total: "16.43",
  counted_total: "16.43",
  reconciles: true,
  difference: "0",
  rows_read: 2,
  units: 1,
  orders: 1,
  revenue: "20.00",
  selling_fees: "-3.57",
  cogs: "-8.00",
  overhead: "0",
  gross_contribution: "16.43",
  net_profit: "8.43",
  units_without_cost: 0,
  skus_missing_cost: [],
  unmapped: [],
  latency_ms: 1,
} as SettlementFileReport;

function upload(report: SettlementFileReport) {
  vi.spyOn(api, "readSettlementFile").mockResolvedValue(report);
  const queries = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const { container } = render(
    <QueryClientProvider client={queries}>
      <SettlementFile />
    </QueryClientProvider>,
  );
  const input = container.querySelector('input[type="file"]')!;
  const file = new File(["settlement"], "settlement.txt", { type: "text/plain" });
  fireEvent.change(input, { target: { files: [file] } });
}

describe("the settlement file screen", () => {
  it("says so when the file does not add up to Amazon's payout", async () => {
    upload({
      ...CLEAN,
      reconciles: false,
      counted_total: "10.00",
      difference: "-6.43",
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /does not reconcile/i,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/should not be trusted/i);
  });

  it("lists fees it did not recognise rather than hiding them", async () => {
    upload({
      ...CLEAN,
      unmapped: [{ name: "order/somethingnew/neverseen", total: "-1.50" }],
    });

    expect(
      await screen.findByText(/were not recognised/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/somethingnew/)).toBeInTheDocument();
  });

  it("leaves net profit blank while a unit is unpriced", async () => {
    upload({
      ...CLEAN,
      cogs: null,
      net_profit: null,
      units_without_cost: 1,
      skus_missing_cost: ["SKU-1"],
    });

    expect(await screen.findByText("not yet knowable")).toBeInTheDocument();
    // Still answered, because it needs nothing the seller supplies.
    expect(screen.getByText("€16.43")).toBeInTheDocument();
  });

  it("confirms a clean file against the stated payout", async () => {
    upload(CLEAN);

    expect(await screen.findByText(/Adds up to/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
