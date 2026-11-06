/**
 * The number, and what is standing between the seller and it.
 *
 * Two figures, deliberately not merged. Gross contribution needs nothing the
 * seller supplies, so it is always shown. Net profit is shown only when
 * every sold unit has a cost on record; otherwise the space says which SKUs
 * are missing one, because that list is the thing that turns the figure on.
 *
 * Showing a confident "profit" built on units treated as free would be the
 * single most expensive thing this screen could do, so the unpriced case is
 * the one the layout is designed around rather than an error state bolted
 * to the side.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError, type User } from "../api/client";

/** Money, as a figure a person reads rather than a decimal string. */
function money(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (Number.isNaN(number)) return "—";
  return number.toLocaleString(undefined, {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 2,
  });
}

export function Profit({ user }: { user: User }) {
  const queries = useQueryClient();
  const [report, setReport] = useState<string | null>(null);

  const profit = useQuery({ queryKey: ["profit"], queryFn: () => api.profit() });
  const skus = useQuery({ queryKey: ["skus"], queryFn: () => api.profitBySku() });

  const importCosts = useMutation({
    mutationFn: api.importCosts,
    onSuccess: (result) => {
      const lines = [`Recorded ${result.recorded} cost entries.`];
      if (result.skipped.length > 0) {
        lines.push(
          `${result.skipped.length} row(s) could not be read: ` +
            result.skipped
              .map((row) => `line ${row.line} (${row.reason})`)
              .join(", "),
        );
      }
      if (result.unmatchable.length > 0) {
        lines.push(
          `${result.unmatchable.length} cost(s) have no SKU, so no sale will ` +
            `find them: ${result.unmatchable.join(", ")}. Add a sku column ` +
            `and import again.`,
        );
      }
      setReport(lines.join(" "));
      // The figures are derived from these rows on read, so both change.
      void queries.invalidateQueries({ queryKey: ["profit"] });
      void queries.invalidateQueries({ queryKey: ["skus"] });
    },
    onError: (error) =>
      setReport(
        error instanceof ApiError
          ? error.message
          : "The cost file could not be imported.",
      ),
  });

  async function onFile(file: File | undefined): Promise<void> {
    if (!file) return;
    setReport(null);
    importCosts.mutate(await file.text());
  }

  if (profit.isLoading) return <p className="muted">Working out the figures…</p>;
  if (profit.error || !profit.data) {
    return <p className="error">Could not read the ledger.</p>;
  }

  const it = profit.data;
  const unpriced = it.units_without_cost > 0;

  return (
    <section className="card">
      <h2>Profit</h2>
      {it.units === 0 ? (
        <p className="muted">
          No sales stored yet. Once Amazon is connected, the next fetch brings
          them in.
        </p>
      ) : (
        <>
          <p className="small muted">
            {it.units} units over {it.orders} orders, {it.start} to {it.end}.
          </p>

          <dl className="facts">
            <dt>Revenue</dt>
            <dd>{money(it.revenue)}</dd>

            <dt>Amazon kept</dt>
            <dd>{money(it.selling_fees)}</dd>

            <dt>Postage</dt>
            <dd>{money(it.postage)}</dd>

            <dt>
              <strong>Gross contribution</strong>
            </dt>
            <dd>
              <strong>{money(it.gross_contribution)}</strong>
            </dd>

            <dt>Cost of goods</dt>
            <dd>{money(it.cogs)}</dd>

            <dt>Overhead</dt>
            <dd>{money(it.overhead)}</dd>

            <dt>
              <strong>Net profit</strong>
            </dt>
            <dd>
              {unpriced ? (
                <span className="muted">not yet knowable</span>
              ) : (
                <strong>{money(it.net_profit)}</strong>
              )}
            </dd>
          </dl>

          {unpriced && (
            <p className="small">
              {it.units_without_cost} sold unit
              {it.units_without_cost === 1 ? "" : "s"} have no cost on record,
              so net profit is left blank rather than counting them as free.
              Missing: <code>{it.skus_missing_cost.join(", ")}</code>
            </p>
          )}
        </>
      )}

      {user.role === "owner" && (
        <p className="small">
          <label>
            Import a cost file (CSV):{" "}
            <input
              type="file"
              accept=".csv,text/csv"
              disabled={importCosts.isPending}
              onChange={(event) => void onFile(event.target.files?.[0])}
            />
          </label>
        </p>
      )}
      {report && <p className="small">{report}</p>}

      {skus.data && skus.data.rows.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>SKU</th>
              <th>Units</th>
              <th>Revenue</th>
              <th>Contribution</th>
            </tr>
          </thead>
          <tbody>
            {skus.data.rows.map((row) => (
              <tr key={row.sku}>
                <td>
                  {row.title || row.sku}
                  {row.units_without_cost > 0 && (
                    <span className="muted small"> · no cost</span>
                  )}
                </td>
                <td>{row.units}</td>
                <td>{money(row.revenue)}</td>
                <td>{money(row.contribution)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
