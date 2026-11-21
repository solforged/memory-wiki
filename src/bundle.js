// Reads an OKF bundle from disk: which files are concepts (§3.2), and what
// each concept says about itself.
import fs from "node:fs";
import path from "node:path";
import { parse } from "./frontmatter.js";
import { asList, lifecycle, present, staleness, trustTier } from "./okf.js";

/** Aliases a concept also answers to. An extension key (§4.1). */
export const ALIASES_KEY = "x-llm-wiki-aliases";

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
 */
export function resolveAreas(root, config, wanted = ["default"]) {
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
  file.aliases = strings(fm[ALIASES_KEY]);
  file.tags = strings(fm.tags);
  file.status = lifecycle(fm);
  file.tier = trustTier(fm);
  return file;
}

/**
 * Every file of the selected areas, in path order.
 * @param {{ areas?: string[] }} [options] default: the default space only
 * @returns {{ files: WikiFile[], problems: { rel: string, problem: string }[] }}
 */
export function scan(root, config, { areas = ["default"] } = {}) {
  const selected = resolveAreas(root, config, areas);
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

/** What callers show or serialize for a concept. */
export function describe(file, now = Date.now()) {
  return {
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
  };
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
  return lines.join("\n");
}
