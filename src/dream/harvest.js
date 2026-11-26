// Multi-harness transcript harvester (Pi, omp, Antigravity) and state store.
// Strips tool outputs, thinking, images, and recalled wiki blocks; drops tainted
// or excluded sessions; and packs a bounded per-project digest.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { privateReference } from "../policy.js";

export const DREAM_MARKER = "<!-- memory-wiki-dream -->";
const MAX_FILE_BYTES = 64 * 1024 * 1024;
const MAX_USER_CHARS = 1200;
const MAX_ASSISTANT_CHARS = 1500;
const MAX_CODE_BLOCK_CHARS = 500;
const DEFAULT_LOOKBACK_DAYS = 7;
const MAX_RUN_HISTORY = 20;
const TAINT_TYPES = new Set(["memory-private", "memory-wiki.private", "llm-wiki.private"]);
const TRIVIAL_USER =
  /^(ok|okay|k|yes|y|no|n|yep|nope|thanks|thank you|thx|ty|cool|nice|sure|got it|sounds good|lgtm|continue|go ahead|do it)[.!?]*$/i;

export const dreamCacheDir = (env = process.env) =>
  path.join(env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "memory-wiki", "dream");

export function expandHome(value, home = os.homedir()) {
  if (!value) return value;
  return value === "~" ? home : value.startsWith("~/") ? path.join(home, value.slice(2)) : value;
}

export function loadDreamState(cacheDir) {
  try {
    const parsed = JSON.parse(fs.readFileSync(path.join(cacheDir, "state.json"), "utf8"));
    return {
      version: 1,
      lastRunAt: typeof parsed.lastRunAt === "string" ? parsed.lastRunAt : null,
      lastSucceededAt: typeof parsed.lastSucceededAt === "string" ? parsed.lastSucceededAt : null,
      lastHygieneAt: typeof parsed.lastHygieneAt === "string" ? parsed.lastHygieneAt : null,
      runs: Array.isArray(parsed.runs) ? parsed.runs.slice(0, MAX_RUN_HISTORY) : [],
    };
  } catch {
    return { version: 1, lastRunAt: null, lastSucceededAt: null, lastHygieneAt: null, runs: [] };
  }
}

export function saveDreamState(cacheDir, state) {
  fs.mkdirSync(cacheDir, { recursive: true });
  const file = path.join(cacheDir, "state.json");
  const tmp = `${file}.tmp-${process.pid}`;
  const data = {
    version: 1,
    lastRunAt: state.lastRunAt ?? null,
    lastSucceededAt: state.lastSucceededAt ?? null,
    lastHygieneAt: state.lastHygieneAt ?? null,
    runs: (state.runs ?? []).slice(0, MAX_RUN_HISTORY),
  };
  fs.writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(tmp, file);
}

function listJsonlFiles(rootDir, isAgy) {
  const found = [];
  if (!rootDir || !fs.existsSync(rootDir)) return found;
  let entries;
  try {
    entries = fs.readdirSync(rootDir, { withFileTypes: true });
  } catch {
    return found;
  }
  for (const entry of entries) {
    if (entry.name.startsWith(".")) continue;
    const full = path.join(rootDir, entry.name);
    if (isAgy) {
      if (!entry.isDirectory()) continue;
      const file = path.join(full, ".system_generated", "logs", "transcript.jsonl");
      if (fs.existsSync(file)) found.push(file);
      continue;
    }
    if (entry.isFile() && entry.name.endsWith(".jsonl")) {
      found.push(full);
      continue;
    }
    if (!entry.isDirectory()) continue;
    try {
      for (const child of fs.readdirSync(full, { withFileTypes: true })) {
        if (!child.name.startsWith(".") && child.isFile() && child.name.endsWith(".jsonl")) {
          found.push(path.join(full, child.name));
        }
      }
    } catch {
      // ignore unreadable directories
    }
  }
  return found;
}

function discoverFiles({ home, env, sourceDirs }) {
  const dirsBySource = sourceDirs ?? {
    pi: [
      ...(env.PI_CODING_AGENT_DIR ? [path.join(env.PI_CODING_AGENT_DIR, "sessions")] : []),
      path.join(env.XDG_DATA_HOME || path.join(home, ".local", "share"), "pi", "agent", "sessions"),
      path.join(home, ".pi", "agent", "sessions"),
    ],
    omp: [path.join(home, ".omp", "agent", "sessions")],
    agy: [path.join(home, ".gemini", "antigravity-cli", "brain")],
  };
  const seen = new Set();
  const files = [];
  for (const [source, dirs] of Object.entries(dirsBySource)) {
    for (const dir of dirs) {
      for (const candidate of listJsonlFiles(dir, source === "agy")) {
        let real;
        try {
          real = fs.realpathSync(candidate);
        } catch {
          continue;
        }
        if (seen.has(real)) continue;
        seen.add(real);
        files.push({ source, path: real });
      }
    }
  }
  return files;
}

function textBlocks(content) {
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  return content
    .map((b) => (typeof b === "string" ? b : b?.type === "text" && typeof b.text === "string" ? b.text : ""))
    .filter(Boolean)
    .join("\n")
    .trim();
}

function agyUserText(raw) {
  if (typeof raw !== "string") return "";
  const blocks = [...raw.matchAll(/<USER_REQUEST>([\s\S]*?)<\/USER_REQUEST>/g)].map((m) => m[1].trim()).filter(Boolean);
  if (blocks.length > 0) return blocks.join("\n\n");
  return raw
    .replace(/<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>[\s\S]*?<\/(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>/g, "")
    .trim();
}

function condense(text, limit) {
  const clean = String(text ?? "")
    .replace(/\r\n/g, "\n")
    .replace(/```[\s\S]*?```/g, (b) =>
      b.length > MAX_CODE_BLOCK_CHARS ? `${b.slice(0, 220)}\n…[code omitted]\n\`\`\`` : b,
    )
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  return clean.length > limit ? `${clean.slice(0, limit - 1).trimEnd()}…` : clean;
}

function activeBranch(entries) {
  const byId = new Map();
  let leaf = null;
  for (const e of entries) {
    if (e.id) byId.set(e.id, e);
    if (e.isMessage) leaf = e;
  }
  if (!leaf?.id) return null;
  const keep = new Set();
  for (let cur = leaf; cur?.id && !keep.has(cur.id); cur = cur.parentId ? byId.get(cur.parentId) : null) {
    keep.add(cur.id);
  }
  return keep;
}

function isExcluded(cwd, excludePaths, root, areas, home) {
  if (!cwd) return false;
  const norm = path.resolve(expandHome(cwd, home));
  const resolvedRoot = path.resolve(root);
  for (const name of [".vaults", ...areas]) {
    const blocked = path.join(resolvedRoot, name);
    if (norm === blocked || norm.startsWith(`${blocked}${path.sep}`)) return true;
  }
  return excludePaths.some((raw) => {
    const expanded = expandHome(raw, home);
    if (expanded.startsWith("/")) {
      const target = path.resolve(expanded);
      return norm === target || norm.startsWith(`${target}${path.sep}`);
    }
    return norm.toLowerCase().includes(expanded.toLowerCase());
  });
}

function parseTreeSession(filePath, source, sinceMs) {
  let sessionId = path.basename(filePath, ".jsonl");
  let cwd = "";
  let title = "";
  let tainted = false;
  let maxTs = 0;
  const graph = [];
  const messages = [];

  for (const line of fs.readFileSync(filePath, "utf8").split("\n")) {
    if (!line.trim()) continue;
    let obj;
    try {
      obj = JSON.parse(line);
    } catch {
      continue;
    }
    if (obj?.type === "session") {
      if (obj.id) sessionId = String(obj.id);
      if (typeof obj.cwd === "string") cwd = obj.cwd;
      continue;
    }
    if (obj?.type === "session_info" && obj.name?.trim()) title = obj.name.trim();
    if ((obj?.type === "title" || obj?.type === "title_change") && obj.title?.trim()) title = obj.title.trim();
    if (
      (obj?.type === "custom" && TAINT_TYPES.has(obj.customType) && obj.data?.tainted !== false) ||
      (obj?.type === "custom_message" && obj.details?.private === true)
    ) {
      tainted = true;
      continue;
    }
    const ts = Date.parse(obj?.timestamp ?? "");
    if (Number.isFinite(ts) && ts > maxTs) maxTs = ts;
    const isMessage = obj?.type === "message" && (obj.message?.role === "user" || obj.message?.role === "assistant");
    if (obj?.id) graph.push({ id: String(obj.id), parentId: obj.parentId ? String(obj.parentId) : "", isMessage });
    if (!isMessage) continue;
    const text = textBlocks(obj.message.content);
    if (!text) continue;
    if (text.includes(DREAM_MARKER)) return { skip: "self" };
    messages.push({ id: obj.id ? String(obj.id) : "", role: obj.message.role, text, ts: Number.isFinite(ts) ? ts : 0 });
  }
  if (tainted) return { skip: "tainted" };

  const keep = activeBranch(graph);
  const turns = messages
    .filter((m) => (!keep || !m.id || keep.has(m.id)) && (m.ts === 0 || m.ts > sinceMs))
    .map((m) => ({
      role: m.role,
      text: condense(m.text, m.role === "user" ? MAX_USER_CHARS : MAX_ASSISTANT_CHARS),
      ts: m.ts,
    }));
  return { skip: null, source, sessionId, cwd, title, turns, maxTs };
}

function parseAgySession(filePath, sinceMs, { root, areas, home }) {
  const sessionId = path.basename(path.dirname(path.dirname(path.dirname(filePath))));
  let maxTs = 0;
  const turns = [];
  for (const line of fs.readFileSync(filePath, "utf8").split("\n")) {
    if (!line.trim()) continue;
    let obj;
    try {
      obj = JSON.parse(line);
    } catch {
      continue;
    }
    const ts = Date.parse(obj?.created_at ?? "");
    if (Number.isFinite(ts) && ts > maxTs) maxTs = ts;
    if (obj?.type === "USER_INPUT") {
      const content = agyUserText(obj.content);
      if (!content) continue;
      if (content.includes(DREAM_MARKER)) return { skip: "self" };
      if (areas.length > 0 && privateReference(content, { root, areas, mounted: [], cwd: "", home })) {
        return { skip: "tainted" };
      }
      if (!Number.isFinite(ts) || ts > sinceMs) {
        turns.push({ role: "user", text: condense(content, MAX_USER_CHARS), ts: Number.isFinite(ts) ? ts : 0 });
      }
    } else if (obj?.type === "PLANNER_RESPONSE" && !obj.tool_calls?.length && typeof obj.content === "string" && obj.content.trim()) {
      if (!Number.isFinite(ts) || ts > sinceMs) {
        turns.push({ role: "assistant", text: condense(obj.content, MAX_ASSISTANT_CHARS), ts: Number.isFinite(ts) ? ts : 0 });
      }
    }
  }
  return { skip: null, source: "agy", sessionId, cwd: "", title: "", turns, maxTs };
}

function formatTurn(turn) {
  const date = turn.ts > 0 ? ` [${new Date(turn.ts).toISOString().slice(0, 16).replace("T", " ")}Z]` : "";
  return `${turn.role === "user" ? "USER" : "ASSISTANT"}${date}: ${turn.text}`;
}

function packDigest(sessions, maxChars) {
  if (sessions.length === 0) return { text: "", truncated: false, included: [] };
  const byProject = new Map();
  for (const s of sessions) {
    const key = s.cwd || `${s.source}:${s.sessionId}`;
    if (!byProject.has(key)) byProject.set(key, []);
    byProject.get(key).push(s);
  }
  for (const list of byProject.values()) list.sort((a, b) => b.maxTs - a.maxTs);
  const queues = [...byProject.values()].sort((a, b) => b[0].maxTs - a[0].maxTs);
  const ordered = [];
  while (queues.some((q) => q.length > 0)) {
    for (const q of queues) if (q.length > 0) ordered.push(q.shift());
  }

  const blocks = [];
  const included = [];
  let used = 0;
  let truncated = false;
  for (const s of ordered) {
    const head = [
      `### Session ${s.source}/${s.sessionId.slice(0, 12)}`,
      s.cwd ? `cwd=${s.cwd}` : "",
      s.title ? `title="${s.title}"` : "",
    ]
      .filter(Boolean)
      .join(" | ");
    const lines = [head, ...s.turns.map(formatTurn)];
    const block = `${lines.join("\n")}\n`;
    if (used + block.length <= maxChars) {
      blocks.push(block);
      included.push(s);
      used += block.length;
      continue;
    }
    truncated = true;
    const budget = maxChars - used - head.length - 32;
    if (budget > 250) {
      const tail = [];
      let tailLen = 0;
      for (let i = s.turns.length - 1; i >= 0; i--) {
        const line = formatTurn(s.turns[i]);
        if (tailLen + line.length + 1 > budget) break;
        tail.unshift(line);
        tailLen += line.length + 1;
      }
      if (tail.length > 0) {
        blocks.push(`${[head, ...(tail.length < s.turns.length ? ["…(earlier turns omitted)…"] : []), ...tail].join("\n")}\n`);
        included.push(s);
      }
    }
    break;
  }
  return { text: blocks.join("\n"), truncated, included };
}

/**
 * Incrementally harvests recent non-private conversation turns since `lastRunAt`
 * (or the past `days` days). Untouched files (`mtimeMs <= sinceMs`) are skipped in O(1).
 */
export function harvestTranscripts(
  root,
  config,
  state,
  { now = Date.now(), days = null, home = os.homedir(), env = process.env, sourceDirs } = {},
) {
  const areas = Object.keys(config.areas ?? {});
  const sinceMs =
    days !== null
      ? now - days * 86_400_000
      : state.lastRunAt
        ? Date.parse(state.lastRunAt)
        : now - DEFAULT_LOOKBACK_DAYS * 86_400_000;

  const discovered = discoverFiles({ home, env, sourceDirs });
  const sessions = [];
  let totalUserTurns = 0;
  let substantiveTurns = 0;
  let skippedTainted = 0;
  let skippedExcluded = 0;
  let scannedFiles = 0;

  for (const item of discovered) {
    let stat;
    try {
      stat = fs.statSync(item.path);
    } catch {
      continue;
    }
    if (stat.size === 0 || stat.size > MAX_FILE_BYTES || stat.mtimeMs <= sinceMs) continue;
    scannedFiles++;

    let parsed;
    try {
      parsed =
        item.source === "agy"
          ? parseAgySession(item.path, sinceMs, { root, areas, home })
          : parseTreeSession(item.path, item.source, sinceMs);
    } catch {
      continue;
    }
    if (parsed.skip === "tainted") {
      skippedTainted++;
      continue;
    }
    if (parsed.skip) continue;
    if (isExcluded(parsed.cwd, config.dream.excludePaths, root, areas, home)) {
      skippedExcluded++;
      continue;
    }

    const userTurns = parsed.turns.filter((t) => t.role === "user");
    if (userTurns.length === 0) continue;
    totalUserTurns += userTurns.length;
    substantiveTurns += userTurns.filter((t) => t.text.trim().length >= 12 && !TRIVIAL_USER.test(t.text.trim())).length;
    sessions.push({ ...parsed, path: item.path, maxTs: parsed.maxTs || stat.mtimeMs });
  }

  const packed = packDigest(sessions, config.dream.maxInputChars);
  return {
    sessions: packed.included,
    digestText: packed.text,
    truncated: packed.truncated,
    totalUserTurns,
    substantiveTurns,
    skippedTainted,
    skippedExcluded,
    discoveredFiles: discovered.length,
    scannedFiles,
  };
}
