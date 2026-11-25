// Reads an OKF bundle from disk: which files are concepts (§3.2), what each
// concept says about itself, and optional per-area now.md / log.md context.
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { defaultOnly, NOW_LIMIT } from "./config.js";
import { parse } from "./frontmatter.js";
import { asList, lifecycle, present, staleness, trustTier } from "./okf.js";

/** Aliases a concept also answers to. An extension key (§4.1). */
export const ALIASES_KEY = "x-memory-wiki-aliases";
/** Previous key name, still read so existing and encrypted concepts keep working. */
export const LEGACY_ALIASES_KEY = "x-llm-wiki-aliases";
export const NOW_FILE = "now.md";

const RESERVED = { "index.md": "index", "log.md": "log" };

/**
 * @typedef {object} WikiFile
 * @property {"concept" | "page" | "index" | "log" | "asset"} kind
 *   `page` is Markdown that is not a concept: no frontmatter or no `type`.
 * @property {string} rel   path from the bundle root, with `/`
 * @property {string} abs
 * @property {string} area  "default" or a private area's name
 * @property {string} id    concept ID: `rel` without `.md`
 * @property {Record<string, any>} fm  frontmatter, `{}` when absent or invalid
 * @property {string} body
 * @property {string[]} errors  why the frontmatter block does not parse
 * @property {boolean} hasBlock
 * @property {string} type
 * @property {string} title
 * @property {string} description
 * @property {string[]} aliases
 * @property {string[]} tags
 * @property {string} supersededBy
 * @property {{ status: string, known: boolean, current: boolean }} status
 * @property {"unverified" | "machine-confirmed" | "human-reviewed"} tier
 */

/** Parsed files by absolute path, reused while size and modification time hold. */
const cache = new Map();

/** Drops the cached parse of a file, or of every file under a directory. */
export function forget(abs) {
  for (const key of cache.keys()) {
    if (key === abs || key.startsWith(abs + path.sep)) cache.delete(key);
  }
}

export function areaOf(rel, config) {
  const first = rel.split("/")[0];
  return Object.hasOwn(config.areas, first) ? first : "default";
}

/** An unlocked area is a volume mounted at `<root>/<name>`. */
export function isMounted(root, name) {
  try {
    return fs.statSync(path.join(root, name)).dev !== fs.statSync(root).dev;
  } catch {
    return false;
  }
}

/**
 * The areas a read covers: "default", plus each private area asked for by
 * name or by "all" that is unlocked now. A locked area reads as empty.
 * When `MEMORY_WIKI_DEFAULT_ONLY=1`, any request beyond `"default"` is refused.
 */
export function resolveAreas(root, config, wanted = ["default"], env = process.env) {
  if (defaultOnly(env) && wanted.some((name) => name !== "default")) {
    throw new Error("private areas are disabled in this environment (MEMORY_WIKI_DEFAULT_ONLY)");
  }
  const names = wanted.includes("all") ? ["default", ...Object.keys(config.areas)] : wanted;
  for (const name of names) {
    if (name !== "default" && !Object.hasOwn(config.areas, name)) {
      throw new Error(`unknown area "${name}"`);
    }
  }
  return [...new Set(names)].filter((name) => name === "default" || isMounted(root, name));
}

function* walk(root, real, dir, areas, config, problems) {
  let entries;
  try {
    entries = fs.readdirSync(path.join(root, dir), { withFileTypes: true });
  } catch {
    return;
  }
  entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  for (const entry of entries) {
    if (entry.name.startsWith(".")) continue; // §3.2 rule 4
    const rel = dir ? `${dir}/${entry.name}` : entry.name;
    if (!dir) {
      const area = Object.hasOwn(config.areas, entry.name) ? entry.name : "default";
      if (!areas.includes(area)) continue;
    }
    const abs = path.join(root, rel);
    if (entry.isSymbolicLink()) {
      // §3.2 rule 5: nothing outside the bundle root. Linked directories are
      // not followed at all, which also rules out cycles.
      let target;
      try {
        target = fs.realpathSync(abs);
      } catch {
        continue;
      }
      if (target !== real && !target.startsWith(real + path.sep)) {
        problems.push({ rel, problem: "symbolic link leaves the bundle root" });
      } else if (fs.statSync(target).isFile()) {
        yield rel;
      }
    } else if (entry.isDirectory()) {
      yield* walk(root, real, rel, areas, config, problems);
    } else if (entry.isFile()) {
      yield rel;
    }
  }
}

function titleFromName(rel) {
  return path.posix.basename(rel, ".md").replace(/[-_]+/g, " ");
}

function strings(value) {
  return asList(value)
    .filter((item) => typeof item === "string" || typeof item === "number")
    .map(String);
}

/** @returns {WikiFile} */
function build(root, rel, config) {
  const abs = path.join(root, rel);
  const file = {
    kind: "asset",
    rel,
    abs,
    area: areaOf(rel, config),
    id: rel,
    fm: {},
    body: "",
    errors: [],
    hasBlock: false,
    type: "",
    title: "",
    description: "",
    aliases: [],
    tags: [],
    supersededBy: "",
    status: lifecycle({}),
    tier: "unverified",
  };
  if (!rel.endsWith(".md")) return file;
  const parsed = parse(fs.readFileSync(abs, "utf8"));
  file.id = rel.slice(0, -3);
  file.body = parsed.body;
  file.errors = parsed.errors;
  file.hasBlock = parsed.hasBlock;
  file.fm = parsed.data ?? {};
  const reserved = RESERVED[path.posix.basename(rel)];
  if (reserved) {
    file.kind = reserved; // §3.1: never a concept, whatever its frontmatter says
    return file;
  }
  if (!parsed.data || !present(parsed.data.type)) {
    file.kind = "page";
    return file;
  }
  const fm = parsed.data;
  file.kind = "concept";
  file.type = String(fm.type);
  file.title = present(fm.title) ? String(fm.title) : titleFromName(rel);
  file.description = present(fm.description) ? String(fm.description) : "";
  file.aliases = [...new Set([...strings(fm[ALIASES_KEY]), ...strings(fm[LEGACY_ALIASES_KEY])])];
  file.tags = strings(fm.tags);
  file.supersededBy = present(fm.superseded_by) ? String(fm.superseded_by) : "";
  file.status = lifecycle(fm);
  file.tier = trustTier(fm);
  return file;
}

/**
 * Every file of the selected areas, in path order.
 * @param {{ areas?: string[], env?: NodeJS.ProcessEnv }} [options] default: the default space only
 * @returns {{ files: WikiFile[], problems: { rel: string, problem: string }[] }}
 */
export function scan(root, config, { areas = ["default"], env = process.env } = {}) {
  const selected = resolveAreas(root, config, areas, env);
  const problems = [];
  const files = [];
  let real;
  try {
    real = fs.realpathSync(root);
  } catch {
    return { files, problems };
  }
  for (const rel of walk(root, real, "", selected, config, problems)) {
    const abs = path.join(root, rel);
    const stat = fs.statSync(abs);
    const hit = cache.get(abs);
    if (hit && hit.mtimeMs === stat.mtimeMs && hit.size === stat.size) {
      files.push(hit.file);
      continue;
    }
    const file = build(root, rel, config);
    cache.set(abs, { mtimeMs: stat.mtimeMs, size: stat.size, file });
    files.push(file);
  }
  return { files, problems };
}

/** The concepts of the selected areas. */
export function concepts(root, config, options) {
  return scan(root, config, options).files.filter((file) => file.kind === "concept");
}

/** A concept by ID, by path from the root, or by absolute path. */
export function findConcept(files, root, ref) {
  let rel = ref;
  if (path.isAbsolute(ref) && (ref === root || ref.startsWith(root + path.sep))) {
    rel = path.relative(root, ref).split(path.sep).join("/");
  }
  rel = rel.replace(/^\/+/, "").replace(/\.md$/, "");
  return files.find((file) => file.kind === "concept" && file.id === rel);
}

/** Hash of a concept's recall-visible fields so edits refresh recall even without touching generated.at. */
export function recallRevision(file) {
  return createHash("sha256")
    .update(JSON.stringify([file.title, file.aliases, file.description]))
    .digest("hex")
    .slice(0, 16);
}

/** Full SHA-256 hex digest of raw text, used for optimistic write concurrency (`--if-match`). */
export function fileHash(text) {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

/** Truncates UTF-8 text to at most `limit` bytes without splitting a code point. */
export function truncateUtf8(text, limit) {
  if (Buffer.byteLength(text, "utf8") <= limit) return { text, truncated: false };
  let bytes = 0;
  let end = 0;
  for (const ch of text) {
    const size = Buffer.byteLength(ch, "utf8");
    if (bytes + size > limit) break;
    bytes += size;
    end += ch.length;
  }
  return { text: text.slice(0, end).trimEnd(), truncated: true };
}

/**
 * Optional always-on `now.md` page per selected area (current stack/focus, capped at `nowLimit` bytes).
 * @param {{ areas?: string[], now?: number, env?: NodeJS.ProcessEnv }} [options]
 */
export function readNow(root, config, { areas = ["default"], now = Date.now(), env = process.env } = {}) {
  const limit = config.nowLimit ?? NOW_LIMIT;
  const pages = [];
  for (const area of resolveAreas(root, config, areas, env)) {
    const ref = area === "default" ? NOW_FILE : `${area}/${NOW_FILE}`;
    const abs = path.join(root, ref);
    let raw;
    try {
      raw = fs.readFileSync(abs, "utf8");
    } catch (error) {
      if (error.code === "ENOENT") continue;
      throw error;
    }
    const parsed = parse(raw);
    const body = parsed.body.trim();
    if (!body) continue;
    const fm = parsed.data ?? {};
    const bytes = Buffer.byteLength(body, "utf8");
    const { text, truncated } = truncateUtf8(body, limit);
    pages.push({
      area,
      ref,
      path: abs,
      text,
      bytes,
      limit,
      truncated,
      stale: staleness(fm, now),
      staleAfter: present(fm.stale_after) ? String(fm.stale_after) : undefined,
      revision: fileHash(raw),
    });
  }
  return pages;
}

/**
 * Recent entries from each selected area's `log.md`, newest first.
 * @param {{ areas?: string[], limit?: number, env?: NodeJS.ProcessEnv }} [options]
 */
export function readRecent(
  root,
  config,
  { areas = ["default"], limit = 15, env = process.env } = {},
) {
  const items = [];
  for (const area of resolveAreas(root, config, areas, env)) {
    const abs = path.join(root, area === "default" ? "log.md" : `${area}/log.md`);
    let raw;
    try {
      raw = fs.readFileSync(abs, "utf8");
    } catch (error) {
      if (error.code === "ENOENT") continue;
      throw error;
    }
    let date = "";
    let order = 0;
    for (const line of raw.split("\n")) {
      const heading = /^## +(\d{4}-\d{2}-\d{2})\s*$/.exec(line);
      if (heading) {
        date = heading[1];
        continue;
      }
      const bullet = /^\s*[-*]\s+(.+?)\s*$/.exec(line);
      if (bullet && date) items.push({ area, date, entry: bullet[1], order: order++ });
    }
  }
  items.sort((a, b) => b.date.localeCompare(a.date) || a.order - b.order);
  return items.slice(0, limit).map(({ area, date, entry }) => ({ area, date, entry }));
}

/** What callers show or serialize for a concept. */
export function describe(file, now = Date.now()) {
  const info = {
    id: file.id,
    path: file.abs,
    area: file.area,
    type: file.type,
    title: file.title,
    description: file.description,
    aliases: file.aliases,
    tags: file.tags,
    status: file.status.status,
    tier: file.tier,
    stale: staleness(file.fm, now),
    revision: recallRevision(file),
  };
  if (file.supersededBy) info.superseded_by = file.supersededBy;
  return info;
}

/** One concept as two or three lines of text. */
export function formatSummary(info) {
  const marks = [info.type, info.tier];
  if (info.status !== "stable") marks.push(info.status);
  if (info.stale) marks.push(info.stale === true ? "stale" : "stale_after unreadable");
  if (info.area !== "default") marks.push(`area ${info.area}`);
  const lines = [`${info.title} (${marks.join(", ")}) ${info.id}`];
  if (info.description) lines.push(`  ${info.description}`);
  if (info.aliases.length > 0) lines.push(`  aliases: ${info.aliases.join(", ")}`);
  if (info.superseded_by) lines.push(`  superseded_by: ${info.superseded_by}`);
  return lines.join("\n");
}
