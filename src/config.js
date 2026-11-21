// Where the wiki lives and what its settings file says.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { parse } from "yaml";

/** Settings file in the bundle root. Dot-files are outside the OKF bundle (§3.2). */
export const CONFIG_FILE = ".llm-wiki.yml";
export const VAULT_DIR = ".vaults";

/** The concept types this producer writes, and the directory each lives in. */
export const TYPE_DIRS = {
  Concept: "concepts",
  Entity: "entities",
  Project: "projects",
  Investigation: "investigations",
};

const AREA_NAME = /^[a-z][a-z0-9-]*$/;
const RESERVED_AREAS = new Set(["default", "all", ...Object.values(TYPE_DIRS)]);

/** `$LLM_WIKI_HOME`, else `llm-wiki` under the XDG data directory. */
export function wikiRoot(env = process.env) {
  if (env.LLM_WIKI_HOME) return path.resolve(env.LLM_WIKI_HOME);
  return path.join(env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share"), "llm-wiki");
}

/** Model providers that run on this machine, by their omp names. */
const LOCAL_PROVIDERS = ["llama.cpp", "lm-studio", "ollama", "local"];

/**
 * @typedef {{ image: string, size: string, passphrase: string[] }} Area
 * @typedef {{ human: string, areas: Record<string, Area>, localProviders: string[] }} Config
 */

/**
 * Reads `.llm-wiki.yml`. A root without one has no private areas.
 * @returns {Config}
 */
export function loadConfig(root) {
  let raw;
  try {
    raw = parse(fs.readFileSync(path.join(root, CONFIG_FILE), "utf8"));
  } catch (error) {
    if (error.code !== "ENOENT") throw new Error(`${CONFIG_FILE}: ${error.message}`);
  }
  raw ??= {};
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error(`${CONFIG_FILE}: expected a mapping`);
  }
  const human = raw.human ?? `human:${os.userInfo().username}`;
  if (typeof human !== "string" || !/^human:\S+$/.test(human)) {
    throw new Error(`${CONFIG_FILE}: human must be "human:<id>"`);
  }
  const localProviders = raw.local_providers ?? LOCAL_PROVIDERS;
  if (!Array.isArray(localProviders) || localProviders.some((name) => typeof name !== "string")) {
    throw new Error(`${CONFIG_FILE}: local_providers must be a list of provider names`);
  }
  /** @type {Record<string, Area>} */
  const areas = {};
  for (const [name, value] of Object.entries(raw.areas ?? {})) {
    if (!AREA_NAME.test(name) || RESERVED_AREAS.has(name)) {
      throw new Error(`${CONFIG_FILE}: "${name}" cannot name an area`);
    }
    const passphrase = value?.passphrase;
    if (
      !Array.isArray(passphrase) ||
      passphrase.length === 0 ||
      passphrase.some((arg) => typeof arg !== "string")
    ) {
      throw new Error(
        `${CONFIG_FILE}: areas.${name}.passphrase must be a command, as a list of strings`,
      );
    }
    areas[name] = {
      image: path.resolve(root, value.image ?? `${VAULT_DIR}/${name}.sparsebundle`),
      size: String(value.size ?? "2g"),
      passphrase,
    };
  }
  return { human, areas, localProviders };
}
