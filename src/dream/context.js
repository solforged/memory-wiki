// Deterministic wiki context & prompt builder for dreaming (0 tokens):
// compact default-space manifest, local ~/src/* source-drift detection,
// lint/staleness/orphan triage, and pre-loading of relevant concept bodies.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { LEGACY_ALIASES_KEY, concepts, readNow } from "../bundle.js";
import { extractLinks, resolveTarget } from "../links.js";
import { lint } from "../lint.js";
import { recall } from "../lookup.js";
import { asList, present, staleness, timestamp } from "../okf.js";
import { DREAM_MARKER, expandHome } from "./harvest.js";

const MAX_PRELOADS = 14;
const MAX_PRELOAD_CHARS = 24_000;
const MAX_DRIFT = 6;
const MAX_DRIFT_CHARS = 1500;
const MAX_ORPHAN_PRELOADS = 4;
const HYGIENE_INTERVAL_MS = 7 * 86_400_000;

function discoverRepos(repoRoots, home) {
  const repos = new Map();
  for (const raw of repoRoots) {
    const abs = path.resolve(expandHome(raw, home));
    if (!fs.existsSync(abs)) continue;
    try {
      for (const entry of fs.readdirSync(abs, { withFileTypes: true })) {
        if (!entry.isDirectory() || entry.name.startsWith(".")) continue;
        const repoPath = path.join(abs, entry.name);
        if (fs.existsSync(path.join(repoPath, ".git")) && !repos.has(entry.name)) {
          repos.set(entry.name, repoPath);
        }
      }
    } catch {
      // ignore unreadable repo root
    }
  }
  return repos;
}

function resolveSource(resource, repos, home) {
  if (typeof resource !== "string" || !resource.trim() || /^[a-z][a-z0-9+.-]*:/i.test(resource.trim())) {
    return null;
  }
  const trimmed = resource.trim();
  if (trimmed.startsWith("~/") || path.isAbsolute(trimmed)) {
    const abs = path.resolve(expandHome(trimmed, home));
    if (!fs.existsSync(abs) || !fs.statSync(abs).isFile()) return null;
    for (const [repoName, repoPath] of repos) {
      if (abs.startsWith(`${repoPath}${path.sep}`)) {
        return { repoName, repoPath, relPath: path.relative(repoPath, abs).split(path.sep).join("/"), abs };
      }
    }
    return null;
  }
  const slash = trimmed.indexOf("/");
  if (slash <= 0) return null;
  const repoName = trimmed.slice(0, slash);
  const relPath = trimmed.slice(slash + 1);
  const repoPath = repos.get(repoName);
  if (!repoPath || !relPath) return null;
  const abs = path.join(repoPath, relPath);
  return fs.existsSync(abs) && fs.statSync(abs).isFile() ? { repoName, repoPath, relPath, abs } : null;
}

function detectDrift(files, repos, home) {
  const hits = [];
  const driftedIds = new Set();
  for (const file of files) {
    if (hits.length >= MAX_DRIFT) break;
    const generatedAt = file.fm?.generated?.at;
    const generatedMs = present(generatedAt) ? timestamp(generatedAt) : NaN;
    if (Number.isNaN(generatedMs)) continue;
    const sinceIso = new Date(generatedMs + 1000).toISOString();
    for (const src of asList(file.fm?.sources)) {
      if (hits.length >= MAX_DRIFT) break;
      const resource = typeof src === "string" ? src : src?.resource;
      const resolved = resolveSource(resource, repos, home);
      if (!resolved) continue;
      const log = spawnSync(
        "git",
        ["-C", resolved.repoPath, "log", `--since=${sinceIso}`, "--oneline", "-n", "5", "--", resolved.relPath],
        { encoding: "utf8" },
      );
      if (log.status !== 0 || !log.stdout.trim()) continue;
      const commits = log.stdout.trim().split("\n");
      const oldest = commits.at(-1).split(" ")[0];
      const diff = spawnSync(
        "git",
        ["-C", resolved.repoPath, "diff", "--unified=1", `${oldest}^..HEAD`, "--", resolved.relPath],
        { encoding: "utf8" },
      );
      const excerpt = (
        diff.status === 0 && diff.stdout.trim()
          ? diff.stdout.trim()
          : fs.readFileSync(resolved.abs, "utf8").trim()
      ).slice(0, MAX_DRIFT_CHARS);
      hits.push({ id: file.id, resource, commits, excerpt });
      driftedIds.add(file.id);
    }
  }
  return { hits, driftedIds };
}

function recentRepoActivity(repos, now) {
  const since = new Date(now - 14 * 86_400_000).toISOString();
  const lines = [];
  for (const [name, repoPath] of [...repos.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const res = spawnSync("git", ["-C", repoPath, "log", `--since=${since}`, "--oneline", "-n", "5"], {
      encoding: "utf8",
    });
    if (res.status === 0 && res.stdout.trim()) {
      lines.push(`- ${name}:\n  ${res.stdout.trim().split("\n").join("\n  ")}`);
    }
  }
  return lines.join("\n").slice(0, 2500);
}

function inboundCounts(files) {
  const counts = new Map(files.map((f) => [f.rel, 0]));
  for (const file of files) {
    const seen = new Set();
    for (const link of extractLinks(file.body)) {
      const to = resolveTarget(link.target, file.rel);
      if (to.kind === "internal" && to.rel !== file.rel && counts.has(to.rel) && !seen.has(to.rel)) {
        seen.add(to.rel);
        counts.set(to.rel, counts.get(to.rel) + 1);
      }
    }
  }
  return counts;
}

export function buildDreamContext(
  root,
  config,
  harvest,
  state,
  { now = Date.now(), force = false, days = null, home = os.homedir(), env = process.env } = {},
) {
  const safeEnv = { ...env, MEMORY_WIKI_DEFAULT_ONLY: "1" };
  const files = concepts(root, config, { areas: ["default"], env: safeEnv });
  const findings = lint(root, config, { areas: ["default"], now, env: safeEnv });
  const nowPage = readNow(root, config, { areas: ["default"], now, env: safeEnv })[0] ?? null;
  const nowMissing = !nowPage;
  const nowStale = Boolean(nowPage && (nowPage.stale === true || nowPage.stale === "malformed"));
  const nowNeedsRefresh = nowMissing || nowStale;

  const inbound = inboundCounts(files);
  const repos = discoverRepos(config.dream.repoRoots, home);
  const { hits: driftedSources, driftedIds } = detectDrift(files, repos, home);
  const lintIssues = findings.filter((f) => f.level === "error" || f.level === "warning");
  const lintRels = new Set(lintIssues.map((f) => f.rel));
  const staleFiles = files.filter((f) => Boolean(staleness(f.fm, now)));
  const orphanFiles = files.filter((f) => f.status.status !== "deprecated" && (inbound.get(f.rel) ?? 0) === 0);

  const lastHygieneMs = state.lastHygieneAt ? Date.parse(state.lastHygieneAt) : 0;
  const hygieneDue = !lastHygieneMs || now - lastHygieneMs >= HYGIENE_INTERVAL_MS;
  const hasActionableWork =
    force ||
    days !== null ||
    harvest.substantiveTurns >= config.dream.minTurns ||
    driftedSources.length > 0 ||
    staleFiles.length > 0 ||
    lintIssues.length > 0 ||
    nowNeedsRefresh ||
    (orphanFiles.length > 0 && hygieneDue);

  const manifest = files
    .map((f) => {
      const flags = [
        staleness(f.fm, now) && "STALE",
        driftedIds.has(f.id) && "DRIFT",
        (inbound.get(f.rel) ?? 0) === 0 && f.status.status !== "deprecated" && "ORPHAN",
        Object.hasOwn(f.fm, LEGACY_ALIASES_KEY) && "LEGACY_ALIAS",
      ].filter(Boolean);
      const aliases = f.aliases.length > 0 ? ` | aliases: ${f.aliases.join(", ")}` : "";
      const tags = f.tags.length > 0 ? ` | tags: ${f.tags.join(", ")}` : "";
      const flagStr = flags.length > 0 ? ` [${flags.join(",")}]` : "";
      return `- ${f.id} | ${f.type} | ${f.status.status}/${f.tier} | "${f.title}"${aliases}${tags}${flagStr} — ${f.description}`;
    })
    .join("\n");

  const queue = [];
  const queued = new Set();
  const enqueue = (file, reason) => {
    if (!file || queued.has(file.id) || queue.length >= MAX_PRELOADS) return;
    queued.add(file.id);
    queue.push({ file, reason });
  };
  files.filter((f) => driftedIds.has(f.id)).forEach((f) => enqueue(f, "source-drift"));
  staleFiles.forEach((f) => enqueue(f, "stale"));
  files.filter((f) => lintRels.has(f.rel)).forEach((f) => enqueue(f, "lint"));
  if (harvest.digestText) recall(files, harvest.digestText, { limit: 12 }).forEach((f) => enqueue(f, "mentioned"));
  orphanFiles.slice(0, MAX_ORPHAN_PRELOADS).forEach((f) => enqueue(f, "orphan"));

  const preloaded = [];
  let chars = 0;
  for (const { file, reason } of queue) {
    const text = [
      `#### ${file.id} (${file.type}, ${file.status.status}, ${file.tier}; ${reason})`,
      `title: ${file.title}`,
      `description: ${file.description}`,
      file.aliases.length ? `aliases: ${file.aliases.join(", ")}` : "",
      file.tags.length ? `tags: ${file.tags.join(", ")}` : "",
      `\n${file.body.trim() || "(empty body)"}`,
    ]
      .filter(Boolean)
      .join("\n");
    if (chars + text.length > MAX_PRELOAD_CHARS && preloaded.length > 0) break;
    preloaded.push({ id: file.id, reason, text });
    chars += text.length;
  }

  return {
    files,
    manifest,
    preloaded,
    driftedSources,
    lintIssues,
    staleCount: staleFiles.length,
    orphanCount: orphanFiles.length,
    hygieneDue,
    nowPage,
    nowMissing,
    nowStale,
    nowNeedsRefresh,
    repoActivity: nowNeedsRefresh ? recentRepoActivity(repos, now) : "",
    hasActionableWork,
  };
}

export function buildDreamPrompts(context, harvest, config, { now = new Date() } = {}) {
  const maxWrites = config.dream.maxWrites;
  const nowLimit = Math.min(config.nowLimit ?? 2000, 1800);
  const system = `${DREAM_MARKER}
You are the overnight consolidation ("dream") pass for an OKF v0.3 memory-wiki.
Output ONLY a single valid JSON object matching the schema below. No prose outside JSON.

Rules:
1. Durability over volume. Capture only knowledge useful across future sessions:
   - User corrections ("X actually means Y", "we switched from A to B").
   - Personal jargon, hosts, tools, repos, or projects missing from the wiki.
   - Concluded investigations (question, tried, findings, open items) -> type "Investigation".
   - Missing aliases for lexical prompt recall, missing relative Markdown cross-links, or real source/transcript drift.
   NEVER capture one-off debugging chatter, transient PR/task state, secrets/tokens/keys, private-area info, or generic textbook facts.
2. OKF v0.3 conventions:
   - type: Concept | Entity | Project | Investigation.
   - description: ONE concise sentence (what lookup and recall show).
   - Link concepts with relative Markdown links like [Tern](../entities/tern.md), NEVER [[wikilinks]]. Only link to IDs in the manifest or created in this plan.
   - Cite provenance in sources (e.g. "session:pi/<id>" or repo path).
   - Updating body replaces it completely; preserve accurate existing bullets.
3. Stability:
   - Updating a "stable" entry demotes it to "draft". Only touch "stable" entries for genuine factual drift or contradictions, not cosmetic tweaks.
4. Budget:
   - Total creates + updates + deprecations <= ${maxWrites}. Prefer updating existing entries over creating duplicates. Return empty arrays if nothing durable warrants writing.
5. now.md:
   - Set "now" to concise Markdown (<= ${nowLimit} UTF-8 bytes, ## Stack and ## Focus) ONLY when now_needs_refresh is true; otherwise null.

JSON schema:
{
  "summary": "1-2 sentence summary",
  "creates": [{ "type": "Concept|Entity|Project|Investigation", "title": "...", "description": "...", "aliases": [], "tags": [], "sources": [], "stale_after": null, "body": "..." }],
  "updates": [{ "id": "entities/example", "reason": "...", "title": null, "description": null, "aliases": [], "tags": [], "sources": [], "replace_lists": false, "stale_after": null, "body": null }],
  "deprecations": [{ "id": "concepts/old", "superseded_by": "new.md", "reason": "..." }],
  "now": null,
  "notes": []
}`;

  const sections = [
    `Date: ${now.toISOString().slice(0, 10)} | max_writes: ${maxWrites} | now_needs_refresh: ${context.nowNeedsRefresh}`,
    context.nowPage ? `## Current now.md\n${context.nowPage.text}` : "",
    context.repoActivity ? `## Recent local repo commits (14d)\n${context.repoActivity}` : "",
    `## Wiki Manifest (${context.files.length} concepts, ${context.orphanCount} orphans, ${context.staleCount} stale)\n${context.manifest || "(empty)"}`,
    context.lintIssues.length
      ? `## Lint warnings/errors\n${context.lintIssues.map((f) => `- ${f.level} ${f.rel}: ${f.message}`).join("\n")}`
      : "",
    context.driftedSources.length
      ? `## Drifted local sources\n${context.driftedSources.map((d) => `### ${d.id} <- ${d.resource}\n${d.commits.join("\n")}\n${d.excerpt}`).join("\n\n")}`
      : "",
    context.preloaded.length
      ? `## Pre-loaded concepts\n${context.preloaded.map((p) => p.text).join("\n\n---\n\n")}`
      : "",
    `## New transcripts (${harvest.sessions.length} sessions, ${harvest.substantiveTurns} substantive turns${harvest.truncated ? ", truncated" : ""})\n${harvest.digestText || "(none)"}`,
  ].filter(Boolean);

  return { system, user: sections.join("\n\n") };
}
