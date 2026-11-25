import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { CONFIG_FILE, loadConfig, lockArea } from "../src/index.js";

// Commits and CLI invocations made by tests must not depend on the host Git or harness env.
process.env.GIT_CONFIG_GLOBAL = "/dev/null";
process.env.GIT_CONFIG_NOSYSTEM = "1";
for (const role of ["AUTHOR", "COMMITTER"]) {
  process.env[`GIT_${role}_NAME`] = "Test";
  process.env[`GIT_${role}_EMAIL`] = "test@example.invalid";
}
for (const key of [
  "MEMORY_WIKI_HOME",
  "MEMORY_WIKI_ACTOR",
  "MEMORY_WIKI_DEFAULT_ONLY",
  "ANTIGRAVITY_AGENT",
  "CLAUDECODE",
  "PI_CODING_AGENT_DIR",
  "OMP_PROFILE",
]) {
  delete process.env[key];
}

export const NOW = new Date("2026-11-21T12:00:00Z");

/**
 * A wiki in a temporary directory. `files` maps paths to text; `areas` goes
 * into the settings file. When the test ends its areas are locked, then it
 * is removed: removing it first would delete what a mounted image holds.
 */
export function tempWiki(t, files = {}, { git = false, areas } = {}) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "memory-wiki-test-")));
  t.after(() => {
    try {
      const config = loadConfig(root);
      for (const name of Object.keys(config.areas)) lockArea(root, config, name, { force: true });
    } catch {
      // ignore invalid config written by negative tests
    }
    fs.rmSync(root, { recursive: true, force: true });
  });
  const settings = { human: "human:tester", ...(areas ? { areas } : {}) };
  fs.writeFileSync(path.join(root, CONFIG_FILE), JSON.stringify(settings));
  for (const [rel, text] of Object.entries(files)) write(root, rel, text);
  if (git) run("git", ["-C", root, "init", "--quiet"]);
  return { root, config: loadConfig(root) };
}

export function write(root, rel, text) {
  const abs = path.join(root, rel);
  fs.mkdirSync(path.dirname(abs), { recursive: true });
  fs.writeFileSync(abs, text);
}

export const read = (root, rel) => fs.readFileSync(path.join(root, rel), "utf8");

export function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: "utf8", ...options });
  if (result.status !== 0) throw new Error(`${command} ${args.join(" ")}: ${result.stderr}`);
  return result.stdout;
}

/** A concept file from frontmatter lines and a body. */
export const concept = (frontmatter, body = "") => `---\n${frontmatter.trim()}\n---\n${body}`;
