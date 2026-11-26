// Where the wiki lives and what its settings file says.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { parse } from "yaml";

/** Settings file in the bundle root. Dot-files are outside the OKF bundle (§3.2). */
export const CONFIG_FILE = ".memory-wiki.yml";
export const LEGACY_CONFIG_FILE = ".llm-wiki.yml";
/** Gitignored per-host overlay for machine-local settings such as NAS sync URLs. */
export const LOCAL_CONFIG_FILE = ".memory-wiki.local.yml";
export const VAULT_DIR = ".vaults";
export const NOW_LIMIT = 2000;

/** The concept types this producer writes, and the directory each lives in. */
export const TYPE_DIRS = {
  Concept: "concepts",
  Entity: "entities",
  Project: "projects",
  Investigation: "investigations",
};

const AREA_NAME = /^[a-z][a-z0-9-]*$/;
const HOST_NAME = /^[a-z0-9][a-z0-9._-]*$/i;
const AGE_RECIPIENT = /^age1[0-9a-z]+$/;
const RESERVED_AREAS = new Set(["default", "all", ...Object.values(TYPE_DIRS)]);
const TIME_HHMM = /^([01]\d|2[0-3]):([0-5]\d)$/;
const WINDOW_RANGE = /^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$/;

/** `$MEMORY_WIKI_HOME`, else `memory-wiki` under the XDG data directory. */
export function wikiRoot(env = process.env) {
  if (env.MEMORY_WIKI_HOME?.trim()) return path.resolve(env.MEMORY_WIKI_HOME.trim());
  return path.join(env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share"), "memory-wiki");
}

/** True when the environment forbids private areas regardless of CLI flags. */
export function defaultOnly(env = process.env) {
  const flag = env.MEMORY_WIKI_DEFAULT_ONLY?.trim().toLowerCase();
  return flag === "1" || flag === "true";
}

/** Short normalized host label used for `<host>.bundle.age` sync files. */
export function defaultHost(env = process.env) {
  const raw = (env.MEMORY_WIKI_HOST?.trim() || os.hostname()).replace(/\.local$/i, "").split(".")[0];
  const clean = raw.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
  return clean || "host";
}

/** Model providers that run on this machine, by their harness names. */
const LOCAL_PROVIDERS = ["llama.cpp", "lm-studio", "ollama", "local"];

/** Defaults for unattended off-hours wiki dreaming. */
export const DEFAULT_DREAM = {
  model: "openai/gpt-6.1-terra",
  thinking: "medium",
  window: "02:00-06:00",
  slots: ["03:15", "05:00"],
  excludePaths: [],
  repoRoots: ["~/src"],
  maxWrites: 8,
  maxInputChars: 48_000,
  minTurns: 3,
};

/**
 * @typedef {{ remote: string | null, username: string | string[] | null, password: string[] | null, host: string | null }} SyncConfig
 * @typedef {{ model: string, thinking: string | null, window: string, slots: string[], excludePaths: string[], repoRoots: string[], maxWrites: number, maxInputChars: number, minTurns: number }} DreamConfig
 * @typedef {{ image: string, size: string, passphrase: string[], ageRecipients: string[], ageIdentity: string[] | null, sync: SyncConfig }} Area
 * @typedef {{ human: string, areas: Record<string, Area>, localProviders: string[], nowLimit: number, sync: SyncConfig, dream: DreamConfig }} Config
 */

function isMapping(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function readYamlMapping(root, filename) {
  let text;
  try {
    text = fs.readFileSync(path.join(root, filename), "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw new Error(`${filename}: ${error.message}`);
  }
  let parsed;
  try {
    parsed = parse(text);
  } catch (error) {
    throw new Error(`${filename}: ${error.message}`);
  }
  if (parsed === null || parsed === undefined) return {};
  if (!isMapping(parsed)) throw new Error(`${filename}: expected a mapping`);
  return parsed;
}

function mergeRaw(base, local) {
  if (!local) return base;
  const merged = { ...base, ...local };
  if (base.sync !== undefined || local.sync !== undefined) {
    if (base.sync !== undefined && !isMapping(base.sync)) {
      throw new Error(`${CONFIG_FILE}: sync must be a mapping`);
    }
    if (local.sync !== undefined && !isMapping(local.sync)) {
      throw new Error(`${LOCAL_CONFIG_FILE}: sync must be a mapping`);
    }
    merged.sync = { ...base.sync, ...local.sync };
  }
  if (base.dream !== undefined || local.dream !== undefined) {
    if (base.dream !== undefined && !isMapping(base.dream)) {
      throw new Error(`${CONFIG_FILE}: dream must be a mapping`);
    }
    if (local.dream !== undefined && !isMapping(local.dream)) {
      throw new Error(`${LOCAL_CONFIG_FILE}: dream must be a mapping`);
    }
    merged.dream = { ...base.dream, ...local.dream };
  }
  if (base.areas !== undefined || local.areas !== undefined) {
    if (base.areas !== undefined && !isMapping(base.areas)) {
      throw new Error(`${CONFIG_FILE}: areas must be a mapping`);
    }
    if (local.areas !== undefined && !isMapping(local.areas)) {
      throw new Error(`${LOCAL_CONFIG_FILE}: areas must be a mapping`);
    }
    const areas = {};
    const names = new Set([...Object.keys(base.areas ?? {}), ...Object.keys(local.areas ?? {})]);
    for (const name of names) {
      const a = base.areas?.[name];
      const b = local.areas?.[name];
      if (a !== undefined && !isMapping(a)) {
        throw new Error(`${CONFIG_FILE}: areas.${name} must be a mapping`);
      }
      if (b !== undefined && !isMapping(b)) {
        throw new Error(`${LOCAL_CONFIG_FILE}: areas.${name} must be a mapping`);
      }
      const entry = { ...a, ...b };
      if (a?.sync !== undefined || b?.sync !== undefined) {
        if (a?.sync !== undefined && !isMapping(a.sync)) {
          throw new Error(`${CONFIG_FILE}: areas.${name}.sync must be a mapping`);
        }
        if (b?.sync !== undefined && !isMapping(b.sync)) {
          throw new Error(`${LOCAL_CONFIG_FILE}: areas.${name}.sync must be a mapping`);
        }
        entry.sync = { ...a?.sync, ...b?.sync };
      }
      areas[name] = entry;
    }
    merged.areas = areas;
  }
  return merged;
}

function commandList(value, what, required) {
  if (value === undefined || value === null) {
    if (required) throw new Error(`${what} must be a command, as a list of strings`);
    return null;
  }
  if (
    !Array.isArray(value) ||
    value.length === 0 ||
    value.some((arg) => typeof arg !== "string" || arg.length === 0)
  ) {
    throw new Error(`${what} must be a command, as a list of strings (never store secrets inline)`);
  }
  return [...value];
}

function stringList(value, what, fallback) {
  if (value === undefined || value === null) return [...fallback];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !item.trim())) {
    throw new Error(`${what} must be a list of non-empty strings`);
  }
  return [...new Set(value.map((item) => item.trim()))];
}

function parseRecipients(value, source, name) {
  const raw = value.age_recipients ?? value.age_recipient;
  if (raw === undefined || raw === null) return [];
  const list = typeof raw === "string" ? [raw] : raw;
  if (!Array.isArray(list) || list.length === 0) {
    throw new Error(`${source}: areas.${name}.age_recipient must be an age1... public key or list of keys`);
  }
  return list.map((item) => {
    if (typeof item !== "string" || !AGE_RECIPIENT.test(item.trim())) {
      throw new Error(`${source}: areas.${name}.age_recipient "${item}" is not a valid age1... public key`);
    }
    return item.trim();
  });
}

function parseSync(raw, label, inherited = null) {
  const base = inherited ?? { remote: null, username: null, password: null, host: null };
  if (raw === undefined || raw === null) return { ...base };
  if (!isMapping(raw)) throw new Error(`${label} must be a mapping`);
  let remote = base.remote;
  if (raw.remote !== undefined && raw.remote !== null) {
    if (typeof raw.remote !== "string" || !raw.remote.trim()) {
      throw new Error(`${label}.remote must be a non-empty URL or directory path`);
    }
    remote = raw.remote.trim();
  }
  let username = base.username;
  if (raw.username !== undefined && raw.username !== null) {
    if (typeof raw.username === "string") {
      if (!raw.username.trim()) throw new Error(`${label}.username must not be empty`);
      username = raw.username.trim();
    } else {
      username = commandList(raw.username, `${label}.username`, true);
    }
  }
  const password =
    raw.password !== undefined ? commandList(raw.password, `${label}.password`, false) : base.password;
  let host = base.host;
  if (raw.host !== undefined && raw.host !== null) {
    if (typeof raw.host !== "string" || !HOST_NAME.test(raw.host.trim())) {
      throw new Error(`${label}.host must match letters, digits, ".", "_" or "-"`);
    }
    host = raw.host.trim().toLowerCase();
  }
  return { remote, username, password, host };
}

function parseDream(raw, label) {
  const out = {
    ...DEFAULT_DREAM,
    slots: [...DEFAULT_DREAM.slots],
    excludePaths: [...DEFAULT_DREAM.excludePaths],
    repoRoots: [...DEFAULT_DREAM.repoRoots],
  };
  if (raw === undefined || raw === null) return out;
  if (!isMapping(raw)) throw new Error(`${label} must be a mapping`);
  if (raw.model !== undefined && raw.model !== null) {
    if (typeof raw.model !== "string" || !raw.model.trim()) {
      throw new Error(`${label}.model must be a non-empty string`);
    }
    out.model = raw.model.trim();
  }
  if (raw.thinking !== undefined) {
    if (raw.thinking === null || raw.thinking === "") out.thinking = null;
    else if (typeof raw.thinking === "string") out.thinking = raw.thinking.trim();
    else throw new Error(`${label}.thinking must be a string or null`);
  }
  if (raw.window !== undefined && raw.window !== null) {
    if (typeof raw.window !== "string" || !WINDOW_RANGE.test(raw.window.trim())) {
      throw new Error(`${label}.window must be "HH:MM-HH:MM" in 24-hour local time`);
    }
    out.window = raw.window.trim();
  }
  if (raw.slots !== undefined && raw.slots !== null) {
    const slots = stringList(raw.slots, `${label}.slots`, DEFAULT_DREAM.slots);
    if (!slots.length || slots.some((s) => !TIME_HHMM.test(s))) {
      throw new Error(`${label}.slots must be a non-empty list of "HH:MM" times`);
    }
    out.slots = slots;
  }
  if (raw.exclude_paths !== undefined) {
    out.excludePaths = stringList(raw.exclude_paths, `${label}.exclude_paths`, []);
  }
  if (raw.repo_roots !== undefined) {
    out.repoRoots = stringList(raw.repo_roots, `${label}.repo_roots`, DEFAULT_DREAM.repoRoots);
  }
  if (raw.max_writes !== undefined && raw.max_writes !== null) {
    if (!Number.isInteger(raw.max_writes) || raw.max_writes < 1) {
      throw new Error(`${label}.max_writes must be a positive integer`);
    }
    out.maxWrites = raw.max_writes;
  }
  return out;
}

/**
 * Reads `.memory-wiki.yml` (falling back to `.llm-wiki.yml` for older checkouts)
 * and merges an optional gitignored `.memory-wiki.local.yml` overlay.
 * A root without any settings file has no private areas.
 * @returns {Config}
 */
export function loadConfig(root) {
  let base = null;
  let source = CONFIG_FILE;
  for (const candidate of [CONFIG_FILE, LEGACY_CONFIG_FILE]) {
    const found = readYamlMapping(root, candidate);
    if (found !== null) {
      base = found;
      source = candidate;
      break;
    }
  }
  const local = readYamlMapping(root, LOCAL_CONFIG_FILE);
  const raw = mergeRaw(base ?? {}, local);

  const human = raw.human ?? `human:${os.userInfo().username}`;
  if (typeof human !== "string" || !/^human:\S+$/.test(human)) {
    throw new Error(`${source}: human must be "human:<id>"`);
  }
  const localProviders = raw.local_providers ?? LOCAL_PROVIDERS;
  if (!Array.isArray(localProviders) || localProviders.some((name) => typeof name !== "string")) {
    throw new Error(`${source}: local_providers must be a list of provider names`);
  }
  const nowLimit = raw.now_limit ?? NOW_LIMIT;
  if (!Number.isInteger(nowLimit) || nowLimit < 100) {
    throw new Error(`${source}: now_limit must be an integer >= 100`);
  }
  const sync = parseSync(raw.sync, `${source}: sync`);
  const dream = parseDream(raw.dream, `${source}: dream`);

  /** @type {Record<string, Area>} */
  const areas = {};
  for (const [name, value] of Object.entries(raw.areas ?? {})) {
    if (!AREA_NAME.test(name) || RESERVED_AREAS.has(name)) {
      throw new Error(`${source}: "${name}" cannot name an area`);
    }
    if (!isMapping(value)) {
      throw new Error(`${source}: areas.${name} must be a mapping`);
    }
    const passphrase = commandList(value.passphrase, `${source}: areas.${name}.passphrase`, true);
    const ageRecipients = parseRecipients(value, source, name);
    const ageIdentity = commandList(value.age_identity, `${source}: areas.${name}.age_identity`, false);
    areas[name] = {
      image: path.resolve(root, value.image ?? `${VAULT_DIR}/${name}.sparsebundle`),
      size: String(value.size ?? "2g"),
      passphrase,
      ageRecipients,
      ageIdentity,
      sync: parseSync(value.sync, `${source}: areas.${name}.sync`, sync),
    };
  }
  return { human, areas, localProviders, nowLimit, sync, dream };
}
