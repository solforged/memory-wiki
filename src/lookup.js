// Finding concepts by name (the glossary), by the names a text mentions
// (recall), and by full text (search).
import path from "node:path";
import { staleness } from "./okf.js";

/** Case, punctuation and spacing do not distinguish names. `+` and `#` do. */
export function normalizeKey(text) {
  return String(text)
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}+#]+/gu, " ")
    .trim();
}

const escapeRe = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

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
  const escaped = escapeRe(name.trim()).replace(/\s+/g, "\\s+");
  return new RegExp(`(?<![\\p{L}\\p{N}_])${escaped}(?![\\p{L}\\p{N}_])`, "u").test(text);
}

/** Whether `nowText` already defines `title` as a heading or bold bullet item. */
function explainedInNow(nowText, title) {
  if (!nowText || !title.trim()) return false;
  return new RegExp(
    `(?:^|\\n)(?:#{1,4}\\s+|\\*\\*|[-*]\\s+\\*\\*)${escapeRe(title.trim())}(?:\\*\\*|[:\\s—–-])`,
    "i",
  ).test(nowText);
}

/**
 * Current concepts whose title or alias appears in `text` as whole words, the
 * longest name winning where names overlap. Names shorter than three characters
 * ("CI") and all-uppercase acronyms ("PRR", "MAP", "SET") match only when the
 * text carries them verbatim, capitals included. Only `status: stable` concepts
 * are recalled (§5.4).
 *
 * @param {number | { limit?: number, nowText?: string }} [options]
 */
export function recall(files, text, options = 8) {
  const limit = typeof options === "number" ? options : (options.limit ?? 8);
  const nowText = typeof options === "number" ? "" : (options.nowText ?? "");
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
      const named = (table.get(key) ?? []).filter((entry) => {
        const needsCase = key.length < 3 || !/\p{Ll}/u.test(entry.name);
        return !needsCase || verbatim(text, entry.name);
      });
      if (named.length === 0) continue;
      for (const entry of named) {
        if (!explainedInNow(nowText, entry.file.title)) hits.set(entry.file.rel, entry.file);
      }
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

function tokens(text) {
  const norm = normalizeKey(text);
  return norm ? norm.split(" ") : [];
}

function tokenCount(words, target) {
  let total = 0;
  for (const word of words) if (word === target) total++;
  return total;
}

function substringMatch(words, target) {
  return target.length >= 3 && words.some((word) => word.includes(target));
}

/**
 * Full-text search. Whole-word matches rank highest; substring matches only
 * apply to query words of at least 3 characters (so "pi" or "op" never match
 * "pipe" or "copy"). Deprecated entries sort after active ones.
 *
 * @param {{ type?: string, tag?: string | string[], tags?: string[], status?: string, stale?: boolean, limit?: number, now?: number }} [options]
 * @returns {{ file: import("./bundle.js").WikiFile, score: number, snippet: string }[]}
 */
export function search(
  files,
  query,
  { type, tag, tags, status, stale, limit = 10, now = Date.now() } = {},
) {
  const words = [...new Set(tokens(query))];
  const wantedTags = [
    ...(Array.isArray(tags) ? tags : []),
    ...(Array.isArray(tag) ? tag : tag ? [tag] : []),
  ].map((item) => item.toLowerCase());
  const results = [];
  for (const file of files) {
    if (type && file.type.toLowerCase() !== type.toLowerCase()) continue;
    if (status && file.status.status.toLowerCase() !== status.toLowerCase()) continue;
    if (stale && !staleness(file.fm, now)) continue;
    if (wantedTags.length > 0) {
      const fileTags = new Set(file.tags.map((item) => item.toLowerCase()));
      if (wantedTags.some((wanted) => !fileTags.has(wanted))) continue;
    }
    const titleWords = tokens(file.title);
    const aliasWords = file.aliases.flatMap(tokens);
    const descWords = tokens(file.description);
    const tagWords = file.tags.flatMap(tokens);
    const bodyWords = tokens(file.body);
    let matched = 0;
    let score = 0;
    for (const word of words) {
      const titleScore = tokenCount(titleWords, word) > 0 ? 8 : substringMatch(titleWords, word) ? 3 : 0;
      const aliasScore = tokenCount(aliasWords, word) > 0 ? 6 : substringMatch(aliasWords, word) ? 2 : 0;
      const descScore = tokenCount(descWords, word) > 0 ? 4 : substringMatch(descWords, word) ? 1 : 0;
      const tagScore = tokenCount(tagWords, word) > 0 ? 3 : substringMatch(tagWords, word) ? 1 : 0;
      const bodyExact = Math.min(tokenCount(bodyWords, word), 5);
      const bodyScore = bodyExact > 0 ? bodyExact : substringMatch(bodyWords, word) ? 1 : 0;
      const weight = titleScore + aliasScore + descScore + tagScore + bodyScore;
      if (weight > 0) matched++;
      score += weight;
    }
    if (matched === 0 && words.length > 0) continue;
    const bodyLines = file.body.split("\n");
    const exactLine = bodyLines.find((text) => {
      const lineWords = tokens(text);
      return words.some((word) => tokenCount(lineWords, word) > 0);
    });
    const fuzzyLine =
      exactLine ??
      bodyLines.find((text) => {
        const lineWords = tokens(text);
        return words.some((word) => substringMatch(lineWords, word));
      });
    const snippet = (fuzzyLine ?? file.description).trim().slice(0, 200);
    results.push({ file, matched, score, snippet });
  }
  results.sort(
    (a, b) =>
      b.matched - a.matched ||
      (a.file.status.status === "deprecated" ? 1 : 0) -
        (b.file.status.status === "deprecated" ? 1 : 0) ||
      b.score - a.score ||
      a.file.title.localeCompare(b.file.title),
  );
  return results.slice(0, limit).map(({ file, score, snippet }) => ({ file, score, snippet }));
}
