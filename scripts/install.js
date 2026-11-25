#!/usr/bin/env node
// Idempotent installer for memory-wiki (safe for `mise bootstrap --only repos`):
// installs JS dependencies if missing, links the `wiki` CLI and shared Agent
// Skill, bridges skill dirs when present, links omp/tern plugins when those
// tools exist, and reports wiki/area status without ever creating sparsebundles.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const usage = "Usage: node scripts/install.js [--dry-run]";
let dryRun = false;
for (const arg of process.argv.slice(2)) {
  if (arg === "--dry-run") dryRun = true;
  else {
    process.stderr.write(`Unknown option: ${arg}\n${usage}\n`);
    process.exit(2);
  }
}

const repo = fs.realpathSync(path.resolve(import.meta.dirname, ".."));
const home = os.homedir();
const prefix = dryRun ? "would " : "";
let conflicts = 0;

const report = (status, target, detail = "") => {
  process.stdout.write(`${status.padEnd(16)} ${target}${detail ? `  (${detail})` : ""}\n`);
};

function lexists(target) {
  try {
    fs.lstatSync(target);
    return true;
  } catch {
    return false;
  }
}

function realpathOrNull(target) {
  try {
    return fs.realpathSync(target);
  } catch {
    return null;
  }
}

function which(name) {
  const result = spawnSync("sh", ["-c", 'command -v "$1"', "--", name], { encoding: "utf8" });
  return result.status === 0 ? result.stdout.trim() : null;
}

function ensureLink(source, target) {
  const realSource = fs.realpathSync(source);
  if (!lexists(target)) {
    if (!dryRun) {
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.symlinkSync(source, target);
    }
    report(`${prefix}link`, target);
    return;
  }
  const resolved = realpathOrNull(target);
  const isLink = fs.lstatSync(target).isSymbolicLink();
  if (resolved === realSource) {
    report("ok", target);
    return;
  }
  conflicts++;
  report("review", target, isLink ? `points to ${fs.readlinkSync(target)}` : "not a symlink");
}

// 1. Install Node dependencies (only `yaml`) when missing or older than aube-lock.yaml.
const nodeModules = path.join(repo, "node_modules");
const yamlPkg = path.join(nodeModules, "yaml", "package.json");
const lockFile = path.join(repo, "aube-lock.yaml");
let needDeps = !fs.existsSync(yamlPkg);
if (!needDeps && fs.existsSync(lockFile)) {
  needDeps = fs.statSync(lockFile).mtimeMs > fs.statSync(nodeModules).mtimeMs;
}
if (needDeps) {
  const pm = which("aube") ?? which("npm");
  if (!pm) {
    conflicts++;
    report("failed", path.join(repo, "node_modules"), "neither aube nor npm found on PATH");
  } else if (dryRun) {
    report("would install", path.join(repo, "node_modules"), path.basename(pm));
  } else {
    const args = path.basename(pm) === "aube" && fs.existsSync(lockFile)
      ? ["install", "--frozen-lockfile"]
      : ["install"];
    const result = spawnSync(pm, args, { cwd: repo, encoding: "utf8" });
    if (result.status !== 0) {
      conflicts++;
      report("failed", path.join(repo, "node_modules"), (result.stderr || result.stdout).trim().split("\n")[0]);
    } else {
      report("install", path.join(repo, "node_modules"), path.basename(pm));
    }
  }
} else {
  report("ok", path.join(repo, "node_modules"));
}

// 2. Link the `wiki` CLI and shared Agent Skill.
ensureLink(path.join(repo, "bin", "wiki.js"), path.join(home, ".local", "bin", "wiki"));
const sharedSkill = path.join(home, ".agents", "skills", "memory-wiki");
ensureLink(path.join(repo, "skills", "memory-wiki"), sharedSkill);

// 3. Bridge into Antigravity and Claude Code skill directories when present.
for (const bridgeDir of [
  path.join(home, ".gemini", "config", "skills"),
  path.join(home, ".claude", "skills"),
]) {
  if (fs.existsSync(bridgeDir) && fs.statSync(bridgeDir).isDirectory()) {
    ensureLink(sharedSkill, path.join(bridgeDir, "memory-wiki"));
  }
}

// 4. Link omp plugin when omp is installed.
const ompBin = which("omp");
if (ompBin) {
  const ompLink = path.join(home, ".omp", "plugins", "node_modules", "memory-wiki");
  if (realpathOrNull(ompLink) === repo) {
    report("ok", "omp plugin: memory-wiki");
  } else if (dryRun) {
    report("would link", "omp plugin: memory-wiki", repo);
  } else {
    const res = spawnSync(ompBin, ["plugin", "link", repo], { encoding: "utf8" });
    if (res.status === 0) report("link", "omp plugin: memory-wiki");
    else {
      conflicts++;
      report("failed", "omp plugin: memory-wiki", (res.stderr || res.stdout).trim().split("\n")[0]);
    }
  }
} else {
  report("skip", "omp plugin: memory-wiki", "omp not installed");
}

// 5. Link Tern plugin on macOS when tern is installed.
const ternBin = process.platform === "darwin" ? which("tern") : null;
if (ternBin) {
  const ternSource = path.join(repo, "tern");
  const list = spawnSync(ternBin, ["plugin", "list"], { encoding: "utf8" });
  const already = list.status === 0 && /\bmemory-wiki\b/.test(list.stdout);
  if (already) {
    report("ok", "tern plugin: memory-wiki");
  } else if (dryRun) {
    report("would link", "tern plugin: memory-wiki", ternSource);
  } else {
    const res = spawnSync(ternBin, ["plugin", "link", ternSource], { encoding: "utf8" });
    if (res.status === 0) report("link", "tern plugin: memory-wiki");
    else {
      conflicts++;
      report("failed", "tern plugin: memory-wiki", (res.stderr || res.stdout).trim().split("\n")[0]);
    }
  }
} else if (process.platform === "darwin") {
  report("skip", "tern plugin: memory-wiki", "tern not installed");
}

// 6. Report wiki root and private area status without ever creating sparsebundles.
const root = process.env.MEMORY_WIKI_HOME?.trim()
  ? path.resolve(process.env.MEMORY_WIKI_HOME.trim())
  : path.join(process.env.XDG_DATA_HOME || path.join(home, ".local", "share"), "memory-wiki");
if (!fs.existsSync(root)) {
  report("missing", root, "clone solforged/wiki or run: wiki init");
} else {
  report("ok", root);
  if (fs.existsSync(yamlPkg)) {
    const { areaStatus, loadConfig } = await import("../src/index.js");
    try {
      const config = loadConfig(root);
      for (const area of areaStatus(root, config)) {
        if (!area.exists) {
          report(
            "area missing",
            area.name,
            `run: wiki area restore ${area.name} (or wiki area create ${area.name})`,
          );
        } else {
          report("area ok", area.name, area.mounted ? "unlocked" : "locked");
        }
      }
    } catch (error) {
      conflicts++;
      report("review", root, error.message);
    }
  }
}

if (conflicts) {
  process.stdout.write(`\n${conflicts} item(s) need attention; review above, then rerun.\n`);
  process.exitCode = 1;
}
