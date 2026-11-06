/**
 * Real figures before committing to anything.
 *
 * Everything else here needs an Amazon application, which needs a developer
 * registration, which is a review with a human on the other end. Someone
 * deciding whether Biryar is worth their evening cannot find out until
 * Amazon replies — unless they can drop in a file they already have.
 *
 * Two things this screen must be honest about, both shown rather than
 * buried: whether the file reconciles against the payout Amazon states, and
 * which fees were not recognised. Amazon publishes the report's column
 * names but not the values of amount-type and amount-description, so an
 * unfamiliar fee is expected rather than exceptional, and a figure that
 * quietly omitted one would be worse than no figure.
 */

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError, type SettlementFileReport } from "../api/client";

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

export function SettlementFile() {
  const [report, setReport] = useState<SettlementFileReport | null>(null);

  const read = useMutation({
    mutationFn: api.readSettlementFile,
    onSuccess: setReport,
    onError: () => setReport(null),
  });

  async function onFile(file: File | undefined): Promise<void> {
    if (!file) return;
    read.mutate(await file.text());
  }

  return (
    <section className="card">
      <h2>Try it with a settlement report</h2>
      <p className="small muted">
        No Amazon application needed. In Seller Central go to{" "}
        <strong>Reports → Payments → All Statements</strong>, download one as{" "}
        <strong>Flat File V2</strong>, and drop it here. Nothing is stored —
        this reads the file, shows you the figures, and keeps none of it.
      </p>

      <p className="small">
        <label>
          Settlement report:{" "}
          <input
            type="file"
            accept=".txt,.tsv,text/tab-separated-values,text/plain"
            disabled={read.isPending}
            onChange={(event) => void onFile(event.target.files?.[0])}
          />
        </label>
      </p>

      {read.isPending && <p className="muted">Reading…</p>}
      {read.error && (
        <p className="error" role="alert">
          {read.error instanceof ApiError
            ? read.error.message
            : "That file could not be read."}
        </p>
      )}

      {report && (
        <>
          <p className="small muted">
            Settlement {report.settlement_id}
            {report.period_start && report.period_end
              ? `, ${report.period_start} to ${report.period_end}`
              : ""}{" "}
            · {report.rows_read} rows · {report.units} units over{" "}
            {report.orders} orders
          </p>

          {/* Shown before the figures, because it decides whether to
              believe them. */}
          {report.reconciles ? (
            <p className="small">
              ✓ Adds up to the {money(report.stated_total)} Amazon states for
              this settlement.
            </p>
          ) : (
            <p className="error" role="alert">
              This file does not reconcile: its amounts come to{" "}
              {money(report.counted_total)}, and it states{" "}
              {money(report.stated_total)} — a difference of{" "}
              {money(report.difference)}. The figures below were read from it
              and should not be trusted until that is explained.
            </p>
          )}

          <dl className="facts">
            <dt>Revenue</dt>
            <dd>{money(report.revenue)}</dd>
            <dt>Amazon kept</dt>
            <dd>{money(report.selling_fees)}</dd>
            <dt>
              <strong>Gross contribution</strong>
            </dt>
            <dd>
              <strong>{money(report.gross_contribution)}</strong>
            </dd>
            <dt>Cost of goods</dt>
            <dd>{money(report.cogs)}</dd>
            <dt>Overhead</dt>
            <dd>{money(report.overhead)}</dd>
            <dt>
              <strong>Net profit</strong>
            </dt>
            <dd>
              {report.units_without_cost > 0 ? (
                <span className="muted">not yet knowable</span>
              ) : (
                <strong>{money(report.net_profit)}</strong>
              )}
            </dd>
          </dl>

          {report.units_without_cost > 0 && (
            <p className="small">
              {report.units_without_cost} unit
              {report.units_without_cost === 1 ? "" : "s"} have no cost on
              record, so net profit is left blank rather than counting them as
              free. Missing:{" "}
              <code>{report.skus_missing_cost.join(", ")}</code>
            </p>
          )}

          {report.unmapped.length > 0 && (
            <p className="small">
              {report.unmapped.length} kind(s) of amount were not recognised.
              They are counted in the total above, not dropped, but they are
              not attributed to a sale:{" "}
              <code>
                {report.unmapped
                  .map((row) => `${row.name} (${money(row.total)})`)
                  .join(", ")}
              </code>
            </p>
          )}
        </>
      )}
    </section>
  );
}
