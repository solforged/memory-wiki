// Where the wiki lives and what its settings file says.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { parse } from "yaml";

/** Settings file in the bundle root. Dot-files are outside the OKF bundle (§3.2). */
export const CONFIG_FILE = ".memory-wiki.yml";
export const LEGACY_CONFIG_FILE = ".llm-wiki.yml";
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
const RESERVED_AREAS = new Set(["default", "all", ...Object.values(TYPE_DIRS)]);

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

/** Model providers that run on this machine, by their harness names. */
const LOCAL_PROVIDERS = ["llama.cpp", "lm-studio", "ollama", "local"];

/**
 * @typedef {{ image: string, size: string, passphrase: string[] }} Area
 * @typedef {{ human: string, areas: Record<string, Area>, localProviders: string[], nowLimit: number }} Config
 */

/**
 * Reads `.memory-wiki.yml` (falling back to `.llm-wiki.yml` for older checkouts).
 * A root without either has no private areas.
 * @returns {Config}
 */
export function loadConfig(root) {
  let raw;
  let source = CONFIG_FILE;
  for (const candidate of [CONFIG_FILE, LEGACY_CONFIG_FILE]) {
    try {
      raw = parse(fs.readFileSync(path.join(root, candidate), "utf8"));
      source = candidate;
      break;
    } catch (error) {
      if (error.code !== "ENOENT") throw new Error(`${candidate}: ${error.message}`);
    }
  }
  raw ??= {};
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error(`${source}: expected a mapping`);
  }
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
  /** @type {Record<string, Area>} */
  const areas = {};
  for (const [name, value] of Object.entries(raw.areas ?? {})) {
    if (!AREA_NAME.test(name) || RESERVED_AREAS.has(name)) {
      throw new Error(`${source}: "${name}" cannot name an area`);
    }
    const passphrase = value?.passphrase;
    if (
      !Array.isArray(passphrase) ||
      passphrase.length === 0 ||
      passphrase.some((arg) => typeof arg !== "string")
    ) {
      throw new Error(
        `${source}: areas.${name}.passphrase must be a command, as a list of strings`,
      );
    }
    areas[name] = {
      image: path.resolve(root, value.image ?? `${VAULT_DIR}/${name}.sparsebundle`),
      size: String(value.size ?? "2g"),
      passphrase,
    };
  }
  return { human, areas, localProviders, nowLimit };
}
