// OKF v0.3 consumer rules (§5, §7, §11.2): how to read values that are absent,
// empty, single or malformed, and what trust and freshness follow from them.

/** §11.2 rule 1: null, an empty string and an empty list are an absent key. */
export function present(value) {
  return !(
    value === null ||
    value === undefined ||
    value === "" ||
    (Array.isArray(value) && value.length === 0)
  );
}

/** §11.2 rule 2: one item is a list with one item. A scalar is never split. */
export function asList(value) {
  if (!present(value)) return [];
  return Array.isArray(value) ? value : [value];
}

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const DATE_TIME = /^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$/;

/**
 * §5: an RFC 3339 date-time with an offset, or a full-date meaning the start
 * of that day in UTC. Milliseconds since the epoch, NaN for any other form.
 */
export function timestamp(value) {
  if (value instanceof Date) return value.getTime();
  if (typeof value !== "string") return NaN;
  const text = value.trim();
  if (DATE.test(text)) return Date.parse(`${text}T00:00:00Z`);
  if (DATE_TIME.test(text)) return Date.parse(text);
  return NaN;
}

/**
 * §5.3: the tier comes only from verification events that are current, that
 * is, not older than the last meaningful change (`generated.at`).
 * @returns {"unverified" | "machine-confirmed" | "human-reviewed"}
 */
export function trustTier(fm) {
  let since = -Infinity;
  const generated = fm.generated;
  if (generated && typeof generated === "object" && present(generated.at)) {
    since = timestamp(generated.at);
    // A change time that cannot be read leaves no event provably current.
    if (Number.isNaN(since)) since = Infinity;
  }
  let tier = "unverified";
  for (const event of asList(fm.verified)) {
    if (!event || typeof event !== "object" || !present(event.by) || !present(event.at)) continue;
    const at = timestamp(event.at);
    if (Number.isNaN(at) || at < since) continue;
    if (String(event.by).startsWith("human:")) return "human-reviewed";
    tier = "machine-confirmed";
  }
  return tier;
}

const STATUSES = new Set(["draft", "stable", "deprecated"]);

/** §5.4: absent means stable; a value outside the vocabulary is not stable. */
export function lifecycle(fm) {
  const status = present(fm.status) ? String(fm.status) : "stable";
  return { status, known: STATUSES.has(status), current: status === "stable" };
}

/**
 * §5.5: stale on or after `stale_after`. A value that does not parse is never
 * fresh, and is reported as such.
 * @returns {boolean | "malformed"}
 */
export function staleness(fm, now = Date.now()) {
  if (!present(fm.stale_after)) return false;
  const at = timestamp(fm.stale_after);
  return Number.isNaN(at) ? "malformed" : now >= at;
}

/** §7: `<producer>/<version>` for agents and tools, `<scheme>:<id>` otherwise. */
export function isActor(value) {
  return (
    typeof value === "string" &&
    (/^[a-z][a-z0-9+.-]*:\S+$/i.test(value) || /^[^\s/:]+\/\S+$/.test(value))
  );
}
