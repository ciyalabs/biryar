/**
 * Marketplace names.
 *
 * The coverage test is the one that matters. A marketplace added to the API
 * without a name here would render its raw Amazon id to a seller — correct,
 * ugly, and exactly the kind of thing nobody notices until a customer asks
 * what A1PA6795UKMFR9 means.
 *
 * It reads the API's own document rather than a copy, so adding a
 * marketplace server-side fails here until it is named.
 */

import { describe, expect, it } from "vitest";

import document from "../openapi.json" with { type: "json" };
import { MARKETPLACE_NAMES, marketplaceName } from "./marketplaces";

function marketplacesTheApiKnows(): string[] {
  const schemas = (document as { components: { schemas: Record<string, { enum?: string[] }> } })
    .components.schemas;
  const enumerated = schemas.Marketplace?.enum;
  if (!enumerated) throw new Error("the document has no Marketplace enum");
  return enumerated;
}

describe("names", () => {
  it("renders a place rather than an id", () => {
    expect(marketplaceName("A1PA6795UKMFR9")).toBe("Germany");
  });

  it("falls back to the id rather than to nothing", () => {
    // An unknown id is still more use than a blank: it can be searched for.
    expect(marketplaceName("A0NEWMARKET")).toBe("A0NEWMARKET");
  });

  it("has something to show when there is no connection", () => {
    expect(marketplaceName(null)).toBe("—");
    expect(marketplaceName(undefined)).toBe("—");
  });
});

describe("coverage", () => {
  it("names every marketplace the API can return", () => {
    const missing = marketplacesTheApiKnows().filter(
      (id) => !(id in MARKETPLACE_NAMES),
    );
    expect(missing).toEqual([]);
  });
});
