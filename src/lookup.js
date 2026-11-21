// Finding concepts by name (the glossary), by the names a text mentions
// (recall), and by full text (search).
import path from "node:path";

/** Case, punctuation and spacing do not distinguish names. `+` and `#` do. */
export function normalizeKey(text) {
  return String(text)
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}+#]+/gu, " ")
    .trim();
}

function distance(a, b, max) {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let previous = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    let best = i;
    for (let j = 1; j <= b.length; j++) {
      row[j] = Math.min(
        previous[j] + 1,
        row[j - 1] + 1,
        previous[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1),
      );
      best = Math.min(best, row[j]);
    }
    if (best > max) return max + 1;
    previous = row;
  }
  return previous[b.length];
}

/** Normalized names of a concept: title, aliases, and the file name. */
export function keysOf(file) {
  const names = [file.title, ...file.aliases, path.posix.basename(file.id)];
  return [...new Set(names.map(normalizeKey).filter(Boolean))];
}

/**
 * Concepts named `term` exactly; failing that, near names: a shared prefix, a
 * substring, or a small typo.
 * @returns {{ exact: import("./bundle.js").WikiFile[], candidates: import("./bundle.js").WikiFile[] }}
 */
export function lookup(files, term, limit = 5) {
  const key = normalizeKey(term);
  if (!key) return { exact: [], candidates: [] };
  const exact = [];
  const near = [];
  const typos = Math.max(1, Math.floor(key.length / 5));
  for (const file of files) {
    let best = 0;
    for (const name of keysOf(file)) {
      if (name === key) best = 4;
      else if (name.startsWith(key) || key.startsWith(name)) best = Math.max(best, 3);
      else if (name.includes(key)) best = Math.max(best, 2);
      else if (distance(name, key, typos) <= typos) best = Math.max(best, 1);
    }
    if (best === 4) exact.push(file);
    else if (best > 0) near.push({ file, best });
  }
  if (exact.length > 0) return { exact, candidates: [] };
  near.sort((a, b) => b.best - a.best || a.file.title.localeCompare(b.file.title));
  return { exact, candidates: near.slice(0, limit).map((entry) => entry.file) };
}

function verbatim(text, name) {
  if (!/[\p{Lu}\p{N}]/u.test(name)) return false;
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?<![\\p{L}\\p{N}])${escaped}(?![\\p{L}\\p{N}])`, "u").test(text);
}

/**
 * Current concepts whose title or alias appears in `text` as whole words, the
 * longest name winning where names overlap. A name shorter than three
 * characters ("CI") counts only when the text has it verbatim, capitals
 * included. Only `status: stable` concepts are recalled (§5.4).
 */
export function recall(files, text, limit = 8) {
  const table = new Map();
  let longest = 1;
  for (const file of files) {
    if (!file.status.current) continue;
    for (const name of [file.title, ...file.aliases]) {
      const key = normalizeKey(name);
      if (!key) continue;
      if (!table.has(key)) table.set(key, []);
      table.get(key).push({ file, name });
      longest = Math.max(longest, key.split(" ").length);
    }
  }
  const words = normalizeKey(text).split(" ");
  const hits = new Map();
  for (let at = 0; at < words.length;) {
    let taken = 1;
    for (let count = Math.min(longest, words.length - at); count >= 1; count--) {
      const key = words.slice(at, at + count).join(" ");
      const named = (table.get(key) ?? []).filter(
        (entry) => key.length >= 3 || verbatim(text, entry.name),
      );
      if (named.length === 0) continue;
      for (const entry of named) hits.set(entry.file.rel, entry.file);
      taken = count;
      break;
    }
    at += taken;
  }
  return [...hits.values()].slice(0, limit);
}

/** The block of text a recall adds to a prompt. */
export function formatRecall(infos) {
  const lines = infos.map((info) => {
    const marks = [info.type, info.tier];
    if (info.stale) marks.push("stale: check before relying on it");
    const text = info.description || "(no description)";
    return `- ${info.title} (${marks.join(", ")}): ${text} [${info.id}]`;
  });
  return `Wiki entries for names in this prompt:\n${lines.join("\n")}`;
}

function count(haystack, needle) {
  let total = 0;
  for (let at = haystack.indexOf(needle); at !== -1; at = haystack.indexOf(needle, at + 1)) {
    total++;
  }
  return total;
}

/**
 * Full-text search. Every query word is looked for in the title, aliases,
 * description, tags and body; concepts matching more words rank first, then
 * those matching in weightier fields.
 * @param {{ type?: string, tag?: string, limit?: number }} [options]
 * @returns {{ file: import("./bundle.js").WikiFile, score: number, snippet: string }[]}
 */
export function search(files, query, { type, tag, limit = 10 } = {}) {
  const words = [...new Set(normalizeKey(query).split(" ").filter(Boolean))];
  const results = [];
  for (const file of files) {
    if (type && file.type.toLowerCase() !== type.toLowerCase()) continue;
    if (tag && !file.tags.some((item) => item.toLowerCase() === tag.toLowerCase())) continue;
    const title = normalizeKey(file.title);
    const aliases = file.aliases.map(normalizeKey).join("  ");
    const description = normalizeKey(file.description);
    const tags = file.tags.map(normalizeKey).join("  ");
    const body = normalizeKey(file.body);
    let matched = 0;
    let score = 0;
    for (const word of words) {
      const weight =
        (title.includes(word) ? 8 : 0) +
        (aliases.includes(word) ? 6 : 0) +
        (description.includes(word) ? 4 : 0) +
        (tags.includes(word) ? 3 : 0) +
        Math.min(count(body, word), 5);
      if (weight > 0) matched++;
      score += weight;
    }
    if (matched === 0 && words.length > 0) continue;
    const line = file.body
      .split("\n")
      .find((text) => words.some((word) => normalizeKey(text).includes(word)));
    const snippet = (line ?? file.description).trim().slice(0, 200);
    results.push({ file, matched, score, snippet });
  }
  results.sort(
    (a, b) =>
      b.matched - a.matched || b.score - a.score || a.file.title.localeCompare(b.file.title),
  );
  return results.slice(0, limit).map(({ file, score, snippet }) => ({ file, score, snippet }));
}
