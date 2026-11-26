// Dream pipeline orchestrator: guards, headless pi completion, DreamPlan
// validation, guardrailed atomic writes via src/write.js, status, and undo.
import { spawn, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { areaOf, concepts, findConcept, scan } from "../bundle.js";
import { TYPE_DIRS } from "../config.js";
import { gitIn } from "../git.js";
import { extractLinks, resolveTarget } from "../links.js";
import { lint } from "../lint.js";
import { timestamp } from "../okf.js";
import { privateReference } from "../policy.js";
import { capture, deprecate, reindex, writeNow } from "../write.js";
import { buildDreamContext, buildDreamPrompts } from "./context.js";
import { DREAM_MARKER, dreamCacheDir, harvestTranscripts, loadDreamState, saveDreamState } from "./harvest.js";
import {
  LAUNCH_AGENT_LABEL,
  checkAcPower,
  inOffHoursWindow,
  installLaunchAgent,
  launchAgentStatus,
  renderLaunchAgentPlist,
  uninstallLaunchAgent,
} from "./schedule.js";

const VALID_TYPES = new Set(Object.keys(TYPE_DIRS));
const COOLDOWN_HOURS = 18;
const MODEL_TIMEOUT_MS = 180_000;
const SECRET_RE =
  /\b(?:(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{16,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|AGE-SECRET-KEY-1[0-9A-Z]{20,}|op:\/\/[^\s"'`<>]+|Bearer\s+[A-Za-z0-9._-]{24,})\b|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/i;

export function dreamActor(model) {
  const tag = String(model || "unknown")
    .split("/")
    .pop()
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `memory-wiki-dream/${tag || "unknown"}`;
}

function acquireLock(cacheDir) {
  fs.mkdirSync(cacheDir, { recursive: true });
  const lockDir = path.join(cacheDir, "dream.lock");
  const pidFile = path.join(lockDir, "pid");
  for (let i = 0; i < 2; i++) {
    try {
      fs.mkdirSync(lockDir);
      fs.writeFileSync(pidFile, String(process.pid));
      return () => fs.rmSync(lockDir, { recursive: true, force: true });
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      let stale = false;
      try {
        const pid = Number(fs.readFileSync(pidFile, "utf8").trim());
        if (pid > 0) {
          try {
            process.kill(pid, 0);
          } catch (err) {
            stale = err.code !== "EPERM";
          }
        }
      } catch {
        stale = true;
      }
      if (!stale) return null;
      fs.rmSync(lockDir, { recursive: true, force: true });
    }
  }
  return null;
}

/** Runs an ephemeral, tool-free headless completion via `pi --print`. */
export function invokePi({ model, thinking }, { system, user }, { env = process.env } = {}) {
  const args = [
    "--print",
    "--no-session",
    "--no-tools",
    "--no-extensions",
    "--no-mcp",
    "--model",
    model,
    "--system-prompt",
    system,
    ...(thinking ? ["--thinking", thinking] : []),
  ];
  return new Promise((resolve, reject) => {
    const child = spawn("pi", args, {
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...env, MEMORY_WIKI_DEFAULT_ONLY: "1" },
    });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
      setTimeout(() => child.kill("SIGKILL"), 3000).unref();
    }, MODEL_TIMEOUT_MS);
    timer.unref();
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    child.stdin.on("error", () => {});
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(new Error(`failed to launch pi: ${err.message}`));
    });
    child.on("close", (code, sig) => {
      clearTimeout(timer);
      if (timedOut) reject(new Error(`pi timed out after ${MODEL_TIMEOUT_MS / 1000}s`));
      else if (code !== 0) reject(new Error(`pi exited with ${code ?? sig}: ${(stderr || stdout).trim().slice(0, 500)}`));
      else resolve(stdout);
    });
    child.stdin.end(user);
  });
}

export function extractJsonObject(raw) {
  const text = String(raw ?? "").trim();
  if (!text) throw new Error("dream model returned empty output");
  const fenced = /```(?:json)?\s*\n?([\s\S]*?)```/i.exec(text);
  const candidate = fenced ? fenced[1].trim() : text;
  try {
    return JSON.parse(candidate);
  } catch {
    const start = candidate.indexOf("{");
    const end = candidate.lastIndexOf("}");
    if (start !== -1 && end > start) return JSON.parse(candidate.slice(start, end + 1));
    throw new Error(`dream model did not return valid JSON: ${text.slice(0, 200)}`);
  }
}

const cleanList = (v) =>
  Array.isArray(v) ? [...new Set(v.filter((x) => typeof x === "string" && x.trim()).map((x) => x.trim()))] : [];
const optStr = (v) => (typeof v === "string" && v.trim() ? v.trim() : undefined);

export function validateDreamPlan(raw, { maxWrites = 8 } = {}) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("dream plan must be a JSON object");
  let budget = maxWrites;
  const creates = [];
  const updates = [];
  const deprecations = [];

  for (const item of Array.isArray(raw.creates) ? raw.creates : []) {
    if (budget <= 0 || !item || typeof item !== "object") continue;
    const type = [...VALID_TYPES].find((t) => t.toLowerCase() === String(item.type ?? "").toLowerCase());
    const title = optStr(item.title);
    const description = optStr(item.description);
    if (!type || !title || !description) continue;
    creates.push({
      type,
      title,
      description,
      aliases: cleanList(item.aliases),
      tags: cleanList(item.tags),
      sources: cleanList(item.sources),
      staleAfter: optStr(item.stale_after ?? item.staleAfter),
      body: optStr(item.body) ?? "",
    });
    budget--;
  }
  for (const item of Array.isArray(raw.updates) ? raw.updates : []) {
    if (budget <= 0 || !item || typeof item !== "object") continue;
    const id = optStr(item.id);
    if (!id) continue;
    const entry = {
      id,
      reason: optStr(item.reason) ?? "",
      title: optStr(item.title),
      description: optStr(item.description),
      aliases: cleanList(item.aliases),
      tags: cleanList(item.tags),
      sources: cleanList(item.sources),
      replaceLists: Boolean(item.replace_lists ?? item.replaceLists),
      staleAfter: optStr(item.stale_after ?? item.staleAfter),
      body: optStr(item.body),
    };
    if (!entry.title && !entry.description && !entry.aliases.length && !entry.tags.length && !entry.sources.length && !entry.staleAfter && !entry.body) {
      continue;
    }
    updates.push({
      ...entry,
      aliases: entry.aliases.length ? entry.aliases : undefined,
      tags: entry.tags.length ? entry.tags : undefined,
      sources: entry.sources.length ? entry.sources : undefined,
    });
    budget--;
  }
  for (const item of Array.isArray(raw.deprecations) ? raw.deprecations : []) {
    if (budget <= 0 || !item || typeof item !== "object") continue;
    const id = optStr(item.id);
    if (!id) continue;
    deprecations.push({
      id,
      supersededBy: optStr(item.superseded_by ?? item.supersededBy),
      reason: optStr(item.reason) ?? "",
    });
    budget--;
  }

  return {
    summary: optStr(raw.summary) ?? "Consolidation pass completed.",
    creates,
    updates,
    deprecations,
    now: optStr(raw.now) ?? null,
    notes: cleanList(raw.notes).slice(0, 10),
  };
}

function checkLeak(text, root, config, home) {
  if (SECRET_RE.test(text)) return "blocked: contains credential-like secret";
  const ref = privateReference(text, { root, areas: Object.keys(config.areas ?? {}), mounted: [], cwd: "", home });
  return ref ? `blocked: references private content (${ref})` : null;
}

function checkLinks(body, fromRel, validFiles, root, config) {
  if (!body) return null;
  for (const link of extractLinks(body)) {
    const to = resolveTarget(link.target, fromRel);
    if (to.kind === "outside") return `link "${link.target}" leaves the wiki root`;
    if (to.kind !== "internal") continue;
    if (areaOf(to.rel, config) !== "default") return `link "${link.target}" points into a private area`;
    if (!validFiles.has(to.rel) && !fs.existsSync(path.join(root, to.rel))) {
      return `link "${link.target}" has no target (${to.rel})`;
    }
  }
  return null;
}

function gitHead(root) {
  const res = spawnSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf8" });
  return res.status === 0 ? res.stdout.trim() : null;
}

function applyPlan(root, config, plan, context, { by, now, dryRun, home, env }) {
  const existing = scan(root, config, { areas: ["default"], env });
  const validFiles = new Set(existing.files.map((f) => f.rel));
  const createPreviews = [];
  const rejected = [];
  const applied = [];

  // Preview creates first via src/write.js to get canonical IDs and clash checks without duplicating slug logic.
  for (const item of plan.creates) {
    const blob = [item.title, item.description, item.body, ...item.aliases, ...item.tags, ...item.sources].join("\n");
    const leak = checkLeak(blob, root, config, home);
    if (leak) {
      rejected.push({ kind: "create", target: item.title, reason: leak });
      continue;
    }
    try {
      const preview = capture(
        root,
        config,
        {
          by,
          type: item.type,
          title: item.title,
          description: item.description,
          aliases: item.aliases.length ? item.aliases : undefined,
          tags: item.tags.length ? item.tags : undefined,
          sources: item.sources.length ? item.sources : undefined,
          staleAfter: item.staleAfter,
          body: item.body,
          area: "default",
        },
        { now, areas: ["default"], preview: true, env },
      );
      validFiles.add(`${preview.id}.md`);
      createPreviews.push({ item, preview });
    } catch (error) {
      rejected.push({ kind: "create", target: item.title, reason: error.message });
    }
  }

  const baseSha = dryRun ? null : gitHead(root);

  for (const { item, preview } of createPreviews) {
    const linkErr = checkLinks(item.body, `${preview.id}.md`, validFiles, root, config);
    if (linkErr) {
      validFiles.delete(`${preview.id}.md`);
      rejected.push({ kind: "create", target: item.title, reason: `blocked: ${linkErr}` });
      continue;
    }
    try {
      const res = dryRun
        ? preview
        : capture(
            root,
            config,
            {
              by,
              type: item.type,
              title: item.title,
              description: item.description,
              aliases: item.aliases.length ? item.aliases : undefined,
              tags: item.tags.length ? item.tags : undefined,
              sources: item.sources.length ? item.sources : undefined,
              staleAfter: item.staleAfter,
              body: item.body,
              area: "default",
            },
            { now, areas: ["default"], env },
          );
      applied.push({ kind: "create", id: res.id, title: item.title, preview: dryRun ? preview : undefined });
    } catch (error) {
      rejected.push({ kind: "create", target: item.title, reason: error.message });
    }
  }

  for (const item of plan.updates) {
    const file = findConcept(concepts(root, config, { areas: ["default"], env }), root, item.id);
    if (!file || file.area !== "default") {
      rejected.push({ kind: "update", target: item.id, reason: "concept not found in default space" });
      continue;
    }
    const blob = [item.title, item.description, item.body, ...(item.aliases ?? []), ...(item.tags ?? []), ...(item.sources ?? [])]
      .filter(Boolean)
      .join("\n");
    const leak = checkLeak(blob, root, config, home);
    const linkErr = leak ? null : checkLinks(item.body, file.rel, validFiles, root, config);
    if (leak || linkErr) {
      rejected.push({ kind: "update", target: file.id, reason: leak ?? `blocked: ${linkErr}` });
      continue;
    }
    if (item.staleAfter && Number.isNaN(timestamp(item.staleAfter))) {
      rejected.push({ kind: "update", target: file.id, reason: `invalid stale_after "${item.staleAfter}"` });
      continue;
    }
    try {
      const res = capture(
        root,
        config,
        { by, ...item, id: file.id },
        { now, areas: ["default"], preview: dryRun, env },
      );
      applied.push({
        kind: "update",
        id: res.id,
        wasStable: file.status.current,
        reason: item.reason,
        preview: dryRun ? res : undefined,
      });
    } catch (error) {
      rejected.push({ kind: "update", target: file.id, reason: error.message });
    }
  }

  for (const item of plan.deprecations) {
    const file = findConcept(concepts(root, config, { areas: ["default"], env }), root, item.id);
    if (!file || file.area !== "default") {
      rejected.push({ kind: "deprecate", target: item.id, reason: "concept not found in default space" });
      continue;
    }
    if (item.supersededBy) {
      const to = resolveTarget(item.supersededBy, file.rel);
      if (to.kind === "outside" || (to.kind === "internal" && areaOf(to.rel, config) !== "default")) {
        rejected.push({ kind: "deprecate", target: file.id, reason: `invalid superseded_by "${item.supersededBy}"` });
        continue;
      }
    }
    try {
      const res = deprecate(root, config, file.id, {
        by,
        supersededBy: item.supersededBy,
        now,
        areas: ["default"],
        preview: dryRun,
        env,
      });
      applied.push({
        kind: "deprecate",
        id: res.id,
        supersededBy: item.supersededBy,
        reason: item.reason,
        preview: dryRun ? res : undefined,
      });
    } catch (error) {
      rejected.push({ kind: "deprecate", target: file.id, reason: error.message });
    }
  }

  if (plan.now && context.nowNeedsRefresh) {
    const leak = checkLeak(plan.now, root, config, home);
    const bytes = Buffer.byteLength(plan.now.trim(), "utf8");
    const limit = config.nowLimit ?? 2000;
    if (leak || bytes > limit) {
      rejected.push({ kind: "now", target: "now.md", reason: leak ?? `now.md is ${bytes}/${limit} bytes` });
    } else {
      try {
        const res = writeNow(root, config, { text: plan.now, area: "default", areas: ["default"], now, preview: dryRun, env });
        applied.push({ kind: "now", id: "now.md", bytes, preview: dryRun ? res : undefined });
      } catch (error) {
        rejected.push({ kind: "now", target: "now.md", reason: error.message });
      }
    }
  }

  const headSha = dryRun ? null : gitHead(root);
  const revList =
    !dryRun && baseSha && headSha && baseSha !== headSha
      ? spawnSync("git", ["-C", root, "rev-list", "--reverse", `${baseSha}..${headSha}`], { encoding: "utf8" })
      : null;
  const commits = revList?.status === 0 && revList.stdout.trim() ? revList.stdout.trim().split("\n") : [];
  const lintErrors = dryRun
    ? []
    : lint(root, config, { areas: ["default"], now: now.getTime(), env }).filter((f) => f.level === "error");

  return { applied, rejected, commits, lintErrors };
}

export function formatDreamDigest(run) {
  const lines = [
    `# memory-wiki dream — ${run.at}`,
    "",
    `- **Status**: ${run.status}${run.reason ? ` (${run.reason})` : ""}`,
    run.model ? `- **Model**: ${run.model}` : "",
    run.stats
      ? `- **Harvest**: ${run.stats.sessions} sessions (${run.stats.substantiveTurns} substantive turns; ${run.stats.skippedTainted} tainted skipped, ${run.stats.skippedExcluded} excluded)`
      : "",
    run.summary ? `- **Summary**: ${run.summary}` : "",
  ].filter(Boolean);

  if (run.applied?.length) {
    lines.push("", "## Applied", "");
    for (const a of run.applied) {
      const extra = a.wasStable ? " (demoted stable -> draft)" : a.supersededBy ? ` (superseded by ${a.supersededBy})` : "";
      lines.push(`- **${a.kind}** \`${a.id}\`${extra}${a.reason ? ` — ${a.reason}` : ""}`);
    }
  }
  if (run.rejected?.length) {
    lines.push("", "## Rejected by guardrails", "");
    for (const r of run.rejected) lines.push(`- **${r.kind}** \`${r.target}\`: ${r.reason}`);
  }
  if (run.notes?.length) {
    lines.push("", "## Notes", "");
    for (const n of run.notes) lines.push(`- ${n}`);
  }
  if (run.commits?.length) {
    lines.push("", `Commits: \`${run.commits.map((s) => s.slice(0, 8)).join("`, `")}\` (\`wiki dream undo\` reverts).`);
  }
  return `${lines.join("\n")}\n`;
}

export async function runDream(
  root,
  config,
  {
    now = new Date(),
    bypassGuards = false,
    force = false,
    dryRun = false,
    days = null,
    model = null,
    thinking = undefined,
    cacheDir,
    home = os.homedir(),
    env = process.env,
    sourceDirs,
    checkPower = checkAcPower,
    invokeModel = invokePi,
  } = {},
) {
  const safeEnv = { ...env, MEMORY_WIKI_DEFAULT_ONLY: "1" };
  const dir = cacheDir ?? dreamCacheDir(safeEnv);
  const effectiveModel = model ?? config.dream.model;
  const effectiveThinking = thinking !== undefined ? thinking : config.dream.thinking;
  const at = now.toISOString();

  if (!bypassGuards && !dryRun) {
    if (!inOffHoursWindow(config.dream.window, now)) {
      return { status: "skipped", reason: `outside off-hours window ${config.dream.window}`, at };
    }
    const power = checkPower();
    if (!power.onAc) return { status: "skipped", reason: `on ${power.detail} (AC power required)`, at };
  }

  const release = acquireLock(dir);
  if (!release) return { status: "skipped", reason: "another dream run is already in progress", at };

  try {
    const state = loadDreamState(dir);
    if (!bypassGuards && !dryRun && !force && days === null && state.lastSucceededAt) {
      const hours = (now.getTime() - Date.parse(state.lastSucceededAt)) / 3_600_000;
      if (Number.isFinite(hours) && hours < COOLDOWN_HOURS) {
        return { status: "skipped", reason: `cooldown (${hours.toFixed(1)}h < ${COOLDOWN_HOURS}h)`, at };
      }
    }

    const harvest = harvestTranscripts(root, config, state, { now: now.getTime(), days, home, env: safeEnv, sourceDirs });
    const context = buildDreamContext(root, config, harvest, state, { now: now.getTime(), force, days, home, env: safeEnv });
    const stats = {
      sessions: harvest.sessions.length,
      substantiveTurns: harvest.substantiveTurns,
      skippedTainted: harvest.skippedTainted,
      skippedExcluded: harvest.skippedExcluded,
      driftedSources: context.driftedSources.length,
      staleConcepts: context.staleCount,
      orphanConcepts: context.orphanCount,
      nowNeedsRefresh: context.nowNeedsRefresh,
    };

    if (!context.hasActionableWork) {
      const noop = { status: "noop", reason: "no new transcripts, drift, or due hygiene", at, stats };
      if (!dryRun) {
        state.lastRunAt = at;
        saveDreamState(dir, state);
      }
      return noop;
    }

    const prompts = buildDreamPrompts(context, harvest, config, { now });
    const raw = await invokeModel({ model: effectiveModel, thinking: effectiveThinking }, prompts, { env: safeEnv });
    const plan = validateDreamPlan(extractJsonObject(raw), { maxWrites: config.dream.maxWrites });
    const outcome = applyPlan(root, config, plan, context, {
      by: dreamActor(effectiveModel),
      now,
      dryRun,
      home,
      env: safeEnv,
    });

    const id = `${at.slice(0, 10)}-${randomBytes(3).toString("hex")}`;
    const result = {
      id,
      status: dryRun ? "dry-run" : "completed",
      at,
      model: effectiveModel,
      summary: plan.summary,
      notes: plan.notes,
      stats,
      applied: outcome.applied,
      rejected: outcome.rejected,
      commits: outcome.commits,
      lintErrors: outcome.lintErrors,
      digest: "",
    };
    result.digest = formatDreamDigest(result);

    if (!dryRun) {
      state.lastRunAt = at;
      state.lastSucceededAt = at;
      if (context.hygieneDue) state.lastHygieneAt = at;
      state.runs.unshift({
        id,
        at,
        status: "completed",
        model: effectiveModel,
        summary: plan.summary,
        stats,
        applied: outcome.applied.map(({ kind, id: entryId, wasStable, supersededBy, reason }) => ({
          kind,
          id: entryId,
          wasStable,
          supersededBy,
          reason,
        })),
        rejected: outcome.rejected,
        notes: plan.notes,
        commits: outcome.commits,
        reverted: false,
      });
      saveDreamState(dir, state);
    }
    return result;
  } finally {
    release();
  }
}

export function dreamStatus(root, config, { cacheDir, home = os.homedir(), env = process.env } = {}) {
  const dir = cacheDir ?? dreamCacheDir(env);
  const state = loadDreamState(dir);
  const last = state.runs[0] ?? null;
  return {
    root,
    cacheDir: dir,
    config: config.dream,
    schedule: launchAgentStatus({ home }),
    lastRunAt: state.lastRunAt,
    lastSucceededAt: state.lastSucceededAt,
    recentRuns: state.runs.slice(0, 5),
    lastDigest: last ? formatDreamDigest(last) : null,
  };
}

export function undoDream(root, config, { cacheDir, env = process.env } = {}) {
  const dir = cacheDir ?? dreamCacheDir(env);
  const state = loadDreamState(dir);
  const target = state.runs.find((r) => !r.reverted && r.commits?.length > 0);
  if (!target) throw new Error("no dream run with commits to undo");
  const dirty = spawnSync("git", ["-C", root, "status", "--porcelain", "--untracked-files=no"], { encoding: "utf8" });
  if (dirty.status !== 0) throw new Error(`git status failed in ${root}`);
  if (dirty.stdout.trim()) throw new Error("wiki repo has uncommitted changes; commit or stash before wiki dream undo");

  const reverted = [];
  for (const sha of [...target.commits].reverse()) {
    const res = spawnSync("git", [...gitIn(root), "revert", "--no-edit", sha], { encoding: "utf8" });
    if (res.status !== 0) {
      spawnSync("git", ["-C", root, "revert", "--abort"], { encoding: "utf8" });
      throw new Error(`could not revert ${sha.slice(0, 8)}: ${(res.stderr || res.stdout).trim()}`);
    }
    reverted.push(sha);
  }
  reindex(root, config, { areas: ["default"], env: { ...env, MEMORY_WIKI_DEFAULT_ONLY: "1" } });
  target.reverted = true;
  target.revertedAt = new Date().toISOString();
  saveDreamState(dir, state);
  return { runId: target.id, reverted };
}

export {
  DREAM_MARKER,
  LAUNCH_AGENT_LABEL,
  buildDreamContext,
  buildDreamPrompts,
  checkAcPower,
  dreamCacheDir,
  harvestTranscripts,
  inOffHoursWindow,
  installLaunchAgent,
  launchAgentStatus,
  loadDreamState,
  renderLaunchAgentPlist,
  saveDreamState,
  uninstallLaunchAgent,
};
