/**
 * Amazon's marketplace ids, and what to call them in front of a person.
 *
 * The API speaks ids because that is what Amazon's own documentation and
 * every support conversation use, and a contract that invented its own
 * names would have to be translated back at the first problem. A seller
 * should never see one, though: "A1PA6795UKMFR9" is not a place.
 */

export const MARKETPLACE_NAMES: Record<string, string> = {
  A1PA6795UKMFR9: "Germany",
  A1F83G8C2ARO7P: "United Kingdom",
  A13V1IB3VIYZZH: "France",
  APJ6JRA9NG5V4: "Italy",
  A1RKKUPIHCS9HS: "Spain",
  A1805IZSGTT6HS: "Netherlands",
  ATVPDKIKX0DER: "United States",
};

/** A readable name, falling back to the id rather than to nothing. */
export function marketplaceName(id: string | null | undefined): string {
  if (!id) return "—";
  return MARKETPLACE_NAMES[id] ?? id;
}
