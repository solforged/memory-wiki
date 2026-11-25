// Changes to the wiki: capture a draft, deprecate or verify it, update now.md,
// keep index.md and log.md current, and record each change as one git commit
// serialized by a per-area lockfile.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import {
  ALIASES_KEY,
  LEGACY_ALIASES_KEY,
  NOW_FILE,
  concepts,
  describe,
  fileHash,
  findConcept,
  forget,
  readNow,
  resolveAreas,
} from "./bundle.js";
import { NOW_LIMIT, TYPE_DIRS, defaultOnly } from "./config.js";
import { append, compose, edit, flowList, quoted, split, stamp } from "./frontmatter.js";
import { gitIn } from "./git.js";
import { normalizeKey } from "./lookup.js";
import { asList, isActor, localDate, timestamp } from "./okf.js";

export const ROOT_INDEX = `---
okf_version: "0.3"
title: memory-wiki
description: Personal knowledge base for agents and their human. Look names, projects and jargon up here before guessing.
---
`;

export const areaDir = (root, area) => (area === "default" ? root : path.join(root, area));

const utc = (now) => now.toISOString().replace(/\.\d{3}Z$/, "Z");
const WAIT_VIEW = new Int32Array(new SharedArrayBuffer(4));
const sleepSync = (ms) => Atomics.wait(WAIT_VIEW, 0, 0, ms);

function isAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

/**
 * Serializes writes inside one area directory so concurrent subagents or panes
 * never collide on `.git/index.lock` or roll back each other's log/index edits.
 */
function withWriteLock(dir, fn, timeoutMs = 5000) {
  const lockDir = path.join(dir, ".wiki-write.lock");
  const pidFile = path.join(lockDir, "pid");
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      fs.mkdirSync(lockDir);
      fs.writeFileSync(pidFile, String(process.pid));
      break;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      let stale = false;
      try {
        const owner = Number(fs.readFileSync(pidFile, "utf8").trim());
        stale = owner > 0 && !isAlive(owner);
      } catch {
        try {
          stale = Date.now() - fs.statSync(lockDir).mtimeMs > 15_000;
        } catch {
          continue;
        }
      }
      if (stale) {
        fs.rmSync(lockDir, { recursive: true, force: true });
        continue;
      }
      if (Date.now() > deadline) {
        throw new Error(`timed out waiting for write lock in ${dir}`);
      }
      sleepSync(25);
    }
  }
  try {
    return fn();
  } finally {
    fs.rmSync(lockDir, { recursive: true, force: true });
  }
}

function read(abs) {
  try {
    return fs.readFileSync(abs, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

/** Files written by one change, with what they held before, so a refused commit undoes all of them. */
class Changes {
  #before = new Map();

  write(abs, text) {
    const previous = read(abs);
    if (previous === text) return;
    if (!this.#before.has(abs)) this.#before.set(abs, previous);
    fs.mkdirSync(path.dirname(abs), { recursive: true });
    fs.writeFileSync(abs, text);
    forget(abs);
  }

  get paths() {
    return [...this.#before.keys()];
  }

  rollback() {
    for (const [abs, previous] of this.#before) {
      if (previous === null) fs.rmSync(abs, { force: true });
      else fs.writeFileSync(abs, previous);
      forget(abs);
    }
  }
}

function isRepo(dir) {
  const top = spawnSync("git", ["-C", dir, "rev-parse", "--show-toplevel"], { encoding: "utf8" });
  return top.status === 0 && fs.realpathSync(top.stdout.trim()) === fs.realpathSync(dir);
}

/** One commit of exactly the changed files. A refusal (a hook, say) restores them. */
function commit(dir, changes, message) {
  const paths = changes.paths;
  if (paths.length === 0 || !isRepo(dir)) return false;
  const git = (...args) => spawnSync("git", [...gitIn(dir), ...args], { encoding: "utf8" });
  let result = git("add", "--", ...paths);
  if (result.status === 0) result = git("commit", "--quiet", "-m", message, "--", ...paths);
  if (result.status !== 0) {
    git("reset", "--quiet", "--", ...paths);
    changes.rollback();
    const why = (result.stderr || result.stdout || "").trim();
    throw new Error(`git refused the change, so nothing was kept:\n${why}`);
  }
  return true;
}

function logText(existing, date, entry) {
  const heading = `## ${date}`;
  if (existing === null) return `# Update Log\n\n${heading}\n\n* ${entry}\n`;
  const lines = existing.split("\n");
  const first = lines.findIndex((line) => line.startsWith("## "));
  if (first !== -1 && lines[first].trim() === heading) {
    let at = first + 1;
    while (at < lines.length && lines[at].trim() === "") at++;
    lines.splice(at, 0, `* ${entry}`);
  } else if (first !== -1) {
    lines.splice(first, 0, heading, "", `* ${entry}`, "");
  } else {
    while (lines.length > 0 && lines.at(-1).trim() === "") lines.pop();
    lines.push("", heading, "", `* ${entry}`, "");
  }
  return lines.join("\n");
}

const linkText = (text) => text.replace(/([[\]\\])/g, "\\$1");
const oneLine = (text) => text.replace(/\s+/g, " ").trim();

/**
 * index.md of every directory of an area that holds concepts (§8), by path.
 * The bundle-root index keeps its frontmatter (§8.1) and lists the default
 * space only: a private area has its own index inside it.
 * @returns {Map<string, string>}
 */
export function renderIndexes(root, config, area, env = process.env) {
  const base = area === "default" ? "" : area;
  const parentOf = (dir) => (dir.includes("/") ? path.posix.dirname(dir) : "");
  const dirs = new Map();
  const node = (dir) => {
    if (!dirs.has(dir)) dirs.set(dir, { files: [], subdirs: new Map() });
    return dirs.get(dir);
  };
  node(base);
  for (const file of concepts(root, config, { areas: [area], env })) {
    let dir = parentOf(file.rel);
    node(dir).files.push(file);
    while (dir !== base) {
      const parent = node(parentOf(dir));
      const name = path.posix.basename(dir);
      parent.subdirs.set(name, (parent.subdirs.get(name) ?? 0) + 1);
      dir = parentOf(dir);
    }
  }
  const indexes = new Map();
  for (const [dir, entry] of dirs) {
    const sections = [];
    if (entry.subdirs.size > 0) {
      const lines = [...entry.subdirs]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([name, total]) => {
          const noun = total === 1 ? "document" : "documents";
          return `* [${linkText(name)}](${encodeURI(name)}/) - ${total} ${noun}`;
        });
      sections.push(`# Directories\n\n${lines.join("\n")}`);
    }
    const types = [...new Set(entry.files.map((file) => file.type))].sort();
    for (const type of types) {
      const lines = entry.files
        .filter((file) => file.type === type)
        .sort((a, b) => a.title.localeCompare(b.title))
        .map((file) => {
          const target = encodeURI(path.posix.basename(file.rel));
          const about = file.description ? ` - ${oneLine(file.description)}` : "";
          return `* [${linkText(file.title)}](${target})${about}`;
        });
      sections.push(`# ${type}\n\n${lines.join("\n")}`);
    }
    const abs = path.join(root, dir, "index.md");
    const body = sections.join("\n\n");
    if (dir === "") {
      const parts = split(read(abs) ?? ROOT_INDEX);
      const head = parts.yaml === null ? ROOT_INDEX : `---\n${parts.yaml}---\n`;
      indexes.set(abs, body ? `${head}\n${body}\n` : head);
    } else if (body) {
      indexes.set(abs, `${body}\n`);
    }
  }
  return indexes;
}

function finish(root, config, area, changes, now, entry, message, env = process.env) {
  const dir = areaDir(root, area);
  const log = path.join(dir, "log.md");
  changes.write(log, logText(read(log), localDate(now), entry));
  for (const [abs, text] of renderIndexes(root, config, area, env)) changes.write(abs, text);
  return commit(dir, changes, message);
}

function slugOf(title) {
  return title
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

function texts(value, what) {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !item.trim())) {
    throw new Error(`${what} must be a list of non-empty strings`);
  }
  return [...new Set(value.map((item) => item.trim()))];
}

function sourcesOf(value) {
  if (value === undefined) return undefined;
  if (!Array.isArray(value)) throw new Error("sources must be a list");
  return value.map((item) => {
    const source = typeof item === "string" ? { resource: item } : item;
    if (!source || typeof source.resource !== "string" || !source.resource.trim()) {
      throw new Error("each source needs a resource (§5.1)");
    }
    return source;
  });
}

function mergeStrings(existing, incoming, keyFn = (item) => item.toLowerCase()) {
  const seen = new Set();
  const out = [];
  for (const item of [...existing, ...incoming]) {
    const key = keyFn(item);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}

function mergeSources(existing, incoming) {
  const seen = new Set();
  const out = [];
  for (const raw of [...existing, ...incoming]) {
    const source = typeof raw === "string" ? { resource: raw } : raw;
    if (!source || typeof source.resource !== "string" || !source.resource.trim()) continue;
    const key = source.resource.trim();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(source);
  }
  return out;
}

function line(value, what, required) {
  if (value === undefined && !required) return undefined;
  if (typeof value !== "string" || !value.trim()) throw new Error(`${what} is required`);
  return oneLine(value);
}

function checkRevision(existingRaw, ifMatch, what) {
  if (!ifMatch) return;
  const current = fileHash(existingRaw ?? "");
  if (current !== ifMatch) {
    throw new Error(`${what} changed since preview; preview again before writing`);
  }
}

/**
 * @typedef {object} CaptureInput
 * @property {string} by           who writes this, as an OKF actor (§7)
 * @property {string} [id]         update this concept; without it a new one is created
 * @property {string} [type]       Concept, Entity, Project or Investigation (new concepts)
 * @property {string} [title]
 * @property {string} [description] one sentence: what a lookup returns
 * @property {string} [body]       Markdown; replaces the body on update
 * @property {string[]} [aliases]  appended on update unless `replaceLists` is true
 * @property {string[]} [tags]     appended on update unless `replaceLists` is true
 * @property {(string | { resource: string, id?: string, title?: string })[]} [sources] appended on update unless `replaceLists` is true
 * @property {boolean} [replaceLists] replace aliases/tags/sources on update instead of merging
 * @property {string} [resource]
 * @property {string} [staleAfter] RFC 3339 date or date-time
 * @property {string} [slug]       file name, when the title has no ASCII letters
 * @property {string} [area]       "default" or a private area (new concepts)
 */

/**
 * Writes a concept as `status: draft` with `generated` set to this change. A
 * draft stays out of recall until a person verifies it, and a revised concept
 * returns to draft: its earlier verification is no longer current (§5.3).
 * @param {CaptureInput} input
 * @param {{ now?: Date, areas?: string[], preview?: boolean, ifMatch?: string, env?: NodeJS.ProcessEnv }} [options]
 */
export function capture(
  root,
  config,
  input,
  { now = new Date(), areas, preview = false, ifMatch, env = process.env } = {},
) {
  if (!isActor(input.by)) {
    throw new Error('by must be an actor: "<producer>/<version>" or "<scheme>:<id>" (§7)');
  }
  const effectiveAreas =
    areas ?? (input.id ? (defaultOnly(env) ? ["default"] : ["all"]) : [input.area ?? "default"]);
  const open = resolveAreas(root, config, effectiveAreas, env);
  const aliases = texts(input.aliases, "aliases");
  const tags = texts(input.tags, "tags");
  const sources = sourcesOf(input.sources);
  if (input.staleAfter !== undefined && Number.isNaN(timestamp(input.staleAfter))) {
    throw new Error("staleAfter must be an RFC 3339 date or date-time with an offset");
  }
  const at = utc(now);

  const plan = () => {
    if (input.id) {
      const file = findConcept(concepts(root, config, { areas: open, env }), root, input.id);
      if (!file) throw new Error(`no concept "${input.id}" to update`);
      const existingRaw = fs.readFileSync(file.abs, "utf8");
      checkRevision(existingRaw, ifMatch, file.id);
      const area = file.area;
      const rel = file.rel;
      const title = line(input.title, "title", false);
      const description = line(input.description, "description", false);
      const finalAliases =
        aliases === undefined
          ? undefined
          : input.replaceLists
            ? aliases
            : mergeStrings(file.aliases, aliases, normalizeKey);
      const finalTags =
        tags === undefined
          ? undefined
          : input.replaceLists
            ? tags
            : mergeStrings(file.tags, tags);
      const finalSources =
        sources === undefined
          ? undefined
          : input.replaceLists
            ? sources
            : mergeSources(asList(file.fm.sources), sources);
      clash(root, config, area, [title, ...(finalAliases ?? [])], file.rel, env);
      const pairs = [];
      if (title) pairs.push(["title", title]);
      if (description) pairs.push(["description", description]);
      if (input.resource) pairs.push(["resource", input.resource]);
      if (finalTags) pairs.push(["tags", flowList(finalTags)]);
      if (finalAliases) {
        pairs.push([ALIASES_KEY, flowList(finalAliases)]);
        if (Object.hasOwn(file.fm, LEGACY_ALIASES_KEY)) pairs.push([LEGACY_ALIASES_KEY, undefined]);
      }
      pairs.push(["status", "draft"], ["generated", stamp(input.by, at)]);
      if (input.staleAfter) pairs.push(["stale_after", quoted(input.staleAfter)]);
      if (finalSources) pairs.push(["sources", finalSources]);
      let text = edit(existingRaw, pairs);
      if (input.body !== undefined) {
        const body = input.body.trim();
        text = `---\n${split(text).yaml}---\n${body ? `\n${body}\n` : ""}`;
      }
      return {
        area,
        rel,
        abs: file.abs,
        id: rel.slice(0, -3),
        created: false,
        wasStable: file.status.current,
        before: existingRaw,
        after: text,
        revision: fileHash(existingRaw),
        entry: `**Update**: Revised [${linkText(title ?? file.title)}](${link(area, rel)}), now a draft.`,
      };
    }
    const area = input.area ?? "default";
    if (!open.includes(area)) {
      const known = area === "default" || Object.hasOwn(config.areas, area);
      throw new Error(
        known ? `area "${area}" is locked or not open to this caller` : `unknown area "${area}"`,
      );
    }
    const type = Object.keys(TYPE_DIRS).find(
      (name) => name.toLowerCase() === String(input.type ?? "").toLowerCase(),
    );
    if (!type) throw new Error(`type must be one of: ${Object.keys(TYPE_DIRS).join(", ")}`);
    const title = line(input.title, "title", true);
    const description = line(input.description, "description", true);
    const slug = input.slug ?? slugOf(title);
    if (!/^[a-z0-9][a-z0-9-]*$/.test(slug)) {
      throw new Error("the title gives no usable file name; pass slug (a-z, 0-9, -)");
    }
    const rel = path.posix.join(area === "default" ? "" : area, TYPE_DIRS[type], `${slug}.md`);
    const abs = path.join(root, rel);
    if (fs.existsSync(abs)) {
      throw new Error(`${rel.slice(0, -3)} exists; pass its id to update it`);
    }
    checkRevision("", ifMatch, rel.slice(0, -3));
    clash(root, config, area, [title, ...(aliases ?? [])], null, env);
    const pairs = [
      ["type", type],
      ["title", title],
      ["description", description],
    ];
    if (input.resource) pairs.push(["resource", input.resource]);
    if (tags?.length) pairs.push(["tags", flowList(tags)]);
    if (aliases?.length) pairs.push([ALIASES_KEY, flowList(aliases)]);
    pairs.push(["status", "draft"], ["generated", stamp(input.by, at)]);
    if (input.staleAfter) pairs.push(["stale_after", quoted(input.staleAfter)]);
    if (sources?.length) pairs.push(["sources", sources]);
    const text = compose(pairs, input.body);
    return {
      area,
      rel,
      abs,
      id: rel.slice(0, -3),
      created: true,
      wasStable: false,
      before: "",
      after: text,
      revision: fileHash(""),
      entry: `**Creation**: Added [${linkText(title)}](${link(area, rel)}) as a draft ${type}.`,
    };
  };

  if (preview) {
    const p = plan();
    return {
      id: p.id,
      path: p.abs,
      area: p.area,
      created: p.created,
      wasStable: p.wasStable,
      before: p.before,
      after: p.after,
      revision: p.revision,
    };
  }

  const targetArea = input.id
    ? (findConcept(concepts(root, config, { areas: open, env }), root, input.id)?.area ?? "default")
    : (input.area ?? "default");
  if (!open.includes(targetArea)) {
    const known = targetArea === "default" || Object.hasOwn(config.areas, targetArea);
    throw new Error(
      known
        ? `area "${targetArea}" is locked or not open to this caller`
        : `unknown area "${targetArea}"`,
    );
  }
  return withWriteLock(areaDir(root, targetArea), () => {
    const p = plan();
    const changes = new Changes();
    changes.write(p.abs, p.after);
    const committed = finish(
      root,
      config,
      p.area,
      changes,
      now,
      p.entry,
      `wiki: capture ${p.id}`,
      env,
    );
    const file = findConcept(concepts(root, config, { areas: [p.area], env }), root, p.id);
    return { ...describe(file, now.getTime()), created: p.created, committed };
  });
}

/** A link from an area's log.md to one of its files. */
function link(area, rel) {
  return encodeURI(area === "default" ? rel : rel.slice(area.length + 1));
}

/** Two concepts of one area answering to the same name make lookups ambiguous. */
function clash(root, config, area, names, selfRel, env = process.env) {
  const wanted = new Set(names.filter(Boolean).map(normalizeKey));
  for (const file of concepts(root, config, { areas: [area], env })) {
    if (file.rel === selfRel) continue;
    const shared = [file.title, ...file.aliases].find((name) => wanted.has(normalizeKey(name)));
    if (shared)
      throw new Error(`"${shared}" already names ${file.id}; update that concept instead`);
  }
}

/**
 * Retires a concept: sets `status: deprecated` and optional `superseded_by`,
 * keeps the file for history, updates log/index, and commits.
 * @param {{ by: string, supersededBy?: string, now?: Date, areas?: string[], preview?: boolean, ifMatch?: string, env?: NodeJS.ProcessEnv }} options
 */
export function deprecate(
  root,
  config,
  ref,
  { by, supersededBy, now = new Date(), areas, preview = false, ifMatch, env = process.env },
) {
  if (!isActor(by)) {
    throw new Error('by must be an actor: "<producer>/<version>" or "<scheme>:<id>" (§7)');
  }
  const open = resolveAreas(root, config, areas ?? (defaultOnly(env) ? ["default"] : ["all"]), env);
  const plan = () => {
    const file = findConcept(concepts(root, config, { areas: open, env }), root, ref);
    if (!file) throw new Error(`no concept "${ref}"`);
    const existingRaw = fs.readFileSync(file.abs, "utf8");
    checkRevision(existingRaw, ifMatch, file.id);
    const pairs = [
      ["status", "deprecated"],
      ["generated", stamp(by, utc(now))],
    ];
    if (supersededBy?.trim()) pairs.push(["superseded_by", supersededBy.trim()]);
    const after = edit(existingRaw, pairs);
    const note = supersededBy?.trim() ? ` (superseded by ${supersededBy.trim()})` : "";
    return {
      file,
      before: existingRaw,
      after,
      revision: fileHash(existingRaw),
      entry: `**Deprecation**: Retired [${linkText(file.title)}](${link(file.area, file.rel)})${note}.`,
    };
  };

  if (preview) {
    const p = plan();
    return {
      id: p.file.id,
      path: p.file.abs,
      area: p.file.area,
      before: p.before,
      after: p.after,
      revision: p.revision,
    };
  }

  const probe = findConcept(concepts(root, config, { areas: open, env }), root, ref);
  if (!probe) throw new Error(`no concept "${ref}"`);
  return withWriteLock(areaDir(root, probe.area), () => {
    const p = plan();
    const changes = new Changes();
    changes.write(p.file.abs, p.after);
    const committed = finish(
      root,
      config,
      p.file.area,
      changes,
      now,
      p.entry,
      `wiki: deprecate ${p.file.id}`,
      env,
    );
    const updated = findConcept(
      concepts(root, config, { areas: [p.file.area], env }),
      root,
      p.file.id,
    );
    return { ...describe(updated, now.getTime()), committed };
  });
}

/**
 * Replaces an area's `now.md` (capped at `config.nowLimit` UTF-8 bytes) and
 * sets its next refresh date (`stale_after`, defaulting to 14 days out).
 * @param {{ text: string, staleAfter?: string, area?: string, now?: Date, areas?: string[], preview?: boolean, ifMatch?: string, env?: NodeJS.ProcessEnv }} options
 */
export function writeNow(
  root,
  config,
  {
    text,
    staleAfter,
    area = "default",
    now = new Date(),
    areas,
    preview = false,
    ifMatch,
    env = process.env,
  },
) {
  const open = resolveAreas(root, config, areas ?? [area], env);
  if (!open.includes(area)) {
    const known = area === "default" || Object.hasOwn(config.areas, area);
    throw new Error(
      known ? `area "${area}" is locked or not open to this caller` : `unknown area "${area}"`,
    );
  }
  const body = String(text ?? "").trim();
  if (!body) throw new Error("now.md text is required");
  const limit = config.nowLimit ?? NOW_LIMIT;
  const bytes = Buffer.byteLength(body, "utf8");
  if (bytes > limit) {
    throw new Error(`now.md is ${bytes} bytes, over its ${limit}-byte limit`);
  }
  const refresh =
    staleAfter?.trim() || localDate(new Date(now.getTime() + 14 * 24 * 60 * 60_000));
  if (Number.isNaN(timestamp(refresh))) {
    throw new Error("staleAfter must be an RFC 3339 date or date-time with an offset");
  }
  const ref = area === "default" ? NOW_FILE : `${area}/${NOW_FILE}`;
  const abs = path.join(root, ref);
  const after = `---\nstale_after: "${refresh}"\n---\n\n${body}\n`;

  const plan = () => {
    const existingRaw = read(abs) ?? "";
    checkRevision(existingRaw, ifMatch, ref);
    return { ref, abs, before: existingRaw, after, revision: fileHash(existingRaw) };
  };

  if (preview) {
    const p = plan();
    return { ref: p.ref, path: p.abs, area, before: p.before, after: p.after, revision: p.revision };
  }

  return withWriteLock(areaDir(root, area), () => {
    const p = plan();
    const changes = new Changes();
    changes.write(p.abs, p.after);
    const committed = finish(
      root,
      config,
      area,
      changes,
      now,
      `**Context**: Updated [${NOW_FILE}](${NOW_FILE}).`,
      `wiki: update ${ref}`,
      env,
    );
    const page = readNow(root, config, { areas: [area], now: now.getTime(), env })[0];
    return { ...page, committed };
  });
}

/**
 * Records that a person confirmed a concept: a `verified` event by a `human:`
 * actor (§5.2) and `status: stable`.
 * @param {{ now?: Date, by?: string, areas?: string[], env?: NodeJS.ProcessEnv }} [options]
 */
export function verify(
  root,
  config,
  ref,
  { now = new Date(), by = config.human, areas = ["all"], env = process.env } = {},
) {
  if (!by.startsWith("human:")) throw new Error("only a human: actor verifies (§5.3)");
  const open = resolveAreas(root, config, areas, env);
  const probe = findConcept(concepts(root, config, { areas: open, env }), root, ref);
  if (!probe) throw new Error(`no concept "${ref}"`);
  return withWriteLock(areaDir(root, probe.area), () => {
    const file = findConcept(concepts(root, config, { areas: [probe.area], env }), root, ref);
    if (!file) throw new Error(`no concept "${ref}"`);
    if (file.status.status === "deprecated") throw new Error(`${file.id} is deprecated`);
    let text = fs.readFileSync(file.abs, "utf8");
    if (file.status.status !== "stable") text = edit(text, [["status", "stable"]]);
    text = append(text, "verified", stamp(by, utc(now)));
    const changes = new Changes();
    changes.write(file.abs, text);
    const entry = `**Verification**: [${linkText(file.title)}](${link(file.area, file.rel)}) confirmed by ${by}.`;
    const committed = finish(
      root,
      config,
      file.area,
      changes,
      now,
      entry,
      `wiki: verify ${file.id}`,
      env,
    );
    const updated = findConcept(
      concepts(root, config, { areas: [file.area], env }),
      root,
      file.id,
    );
    return { ...describe(updated, now.getTime()), committed };
  });
}

/** Regenerates index.md files after edits made by hand. Returns the files rewritten. */
export function reindex(root, config, { areas = ["default"], env = process.env } = {}) {
  const written = [];
  for (const area of resolveAreas(root, config, areas, env)) {
    withWriteLock(areaDir(root, area), () => {
      const changes = new Changes();
      for (const [abs, text] of renderIndexes(root, config, area, env)) changes.write(abs, text);
      commit(areaDir(root, area), changes, "wiki: index");
      written.push(...changes.paths);
    });
  }
  return written;
}
