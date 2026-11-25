// Lifecycle hook adapters for harnesses without an in-process extension API
// (Antigravity CLI / `agy` and Claude Code). Both run hosted models, so hooks
// read the default space only, block private-area access and `wiki verify`,
// auto-allow clean read-only `wiki` commands, inject `now.md` at session start,
// and perform deduplicated prompt recall once per user turn.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { concepts, describe, isMounted, readNow } from "./bundle.js";
import { loadConfig, wikiRoot } from "./config.js";
import { formatRecall, recall } from "./lookup.js";
import { inputText, isSafeReadOnlyWikiCommand, privateReference, runsVerify } from "./policy.js";

const STATE_TTL_MS = 7 * 24 * 60 * 60_000;

export function hookCacheDir(env = process.env) {
  return path.join(env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "memory-wiki", "hooks");
}

function safeId(value) {
  return String(value || "default").replace(/[^a-zA-Z0-9_.-]/g, "_").slice(0, 96);
}

function loadSeen(cacheDir, id) {
  const file = path.join(cacheDir, `${safeId(id)}.json`);
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    return { file, recalled: parsed?.recalled && typeof parsed.recalled === "object" ? parsed.recalled : {} };
  } catch {
    return { file, recalled: {} };
  }
}

function saveSeen(cacheDir, file, recalled, now = Date.now()) {
  try {
    fs.mkdirSync(cacheDir, { recursive: true });
    fs.writeFileSync(file, JSON.stringify({ recalled, updatedAt: now }));
    for (const entry of fs.readdirSync(cacheDir)) {
      const abs = path.join(cacheDir, entry);
      if (abs === file) continue;
      try {
        if (now - fs.statSync(abs).mtimeMs > STATE_TTL_MS) fs.rmSync(abs, { force: true });
      } catch {
        // ignore races
      }
    }
  } catch {
    // hook cache is best-effort; never fail a turn over cache permissions
  }
}

function formatNowStart(pages) {
  if (pages.length === 0) return "";
  const lines = [];
  for (const page of pages) {
    lines.push(
      `Current context from the user's memory-wiki (${page.ref}), in their words. Live evidence and the user's instructions take precedence:`,
      "",
      page.text,
      "",
    );
    // Keep staleness out of the injected context so midnight rollovers never invalidate the prompt cache.
    if (page.truncated) {
      lines.push(`${page.ref} is over its ${page.limit}-byte limit and was cut off here.`, "");
    }
  }
  lines.push(
    "More of the memory-wiki is available on demand via `wiki lookup <name>`, `wiki search <words>`, `wiki show <id>`, and `wiki recent`.",
    "When the user corrects a term or a fact about their stack or current work, offer to record it with `wiki capture` or `wiki now --set`; never write unasked.",
  );
  return lines.join("\n").trim();
}

function recallForPrompt(root, config, prompt, sessionId, cacheDir, now, env) {
  if (!prompt?.trim() || !fs.existsSync(root)) return "";
  const nowPages = readNow(root, config, { areas: ["default"], now, env });
  const nowText = nowPages.map((page) => page.text).join("\n");
  const files = concepts(root, config, { areas: ["default"], env });
  const hits = recall(files, prompt, { nowText });
  if (hits.length === 0) return "";
  const { file: stateFile, recalled } = loadSeen(cacheDir, sessionId);
  const fresh = hits
    .map((entry) => describe(entry, now))
    .filter((info) => recalled[info.id] !== info.revision);
  if (fresh.length === 0) return "";
  for (const info of fresh) recalled[info.id] = info.revision;
  saveSeen(cacheDir, stateFile, recalled, now);
  return formatRecall(fresh);
}

const FILE_EDIT_TOOLS = new Set([
  "write_file",
  "edit_file",
  "Write",
  "Edit",
  "MultiEdit",
  "NotebookEdit",
]);
const FILE_BODY_KEYS = new Set([
  "Content",
  "Target",
  "Replacement",
  "content",
  "old_string",
  "new_string",
  "new_source",
]);

function toolGuardText(toolName, args) {
  if (!FILE_EDIT_TOOLS.has(toolName) || !args || typeof args !== "object") {
    return { text: inputText(args), checkCommands: true };
  }
  const filtered = {};
  for (const [key, value] of Object.entries(args)) {
    if (!FILE_BODY_KEYS.has(key)) filtered[key] = value;
  }
  return { text: inputText(filtered), checkCommands: false };
}

function checkToolGuard(root, config, toolName, args, cwd, home) {
  const { text, checkCommands } = toolGuardText(toolName, args);
  if (checkCommands && runsVerify(text)) {
    return "Only the user verifies memory-wiki entries. Give them the id to run: wiki verify <id>";
  }
  if (!fs.existsSync(root)) return null;
  const areas = Object.keys(config.areas);
  const mounted = areas.filter((name) => isMounted(root, name));
  const ref = privateReference(text, { root, areas, mounted, cwd, home, checkCommands });
  if (!ref) return null;
  return `Private memory-wiki areas are restricted to local models, so this hosted session cannot access ${ref}.`;
}

/**
 * Antigravity CLI (`agy`) hook handler (`SessionStart`, `PreInvocation`, `PreToolUse`).
 * @param {Record<string, any>} payload parsed stdin JSON
 * @param {{ root?: string, config?: import("./config.js").Config, env?: NodeJS.ProcessEnv, cacheDir?: string, now?: number, home?: string }} [options]
 */
export function handleAgyHook(payload, options = {}) {
  const env = options.env ?? process.env;
  const root = options.root ?? wikiRoot(env);
  const exists = fs.existsSync(root);
  const config = options.config ?? (exists ? loadConfig(root) : { human: "human:user", areas: {}, localProviders: [], nowLimit: 2000 });
  const cacheDir = options.cacheDir ?? hookCacheDir(env);
  const now = options.now ?? Date.now();
  const event = payload?.hookEventName ?? "";

  if (event === "SessionStart") {
    if (!exists) return {};
    const text = formatNowStart(readNow(root, config, { areas: ["default"], now, env }));
    if (!text) return {};
    return { injectSteps: [{ systemMessage: { systemMessage: text } }] };
  }

  if (event === "PreInvocation") {
    // PreInvocation fires before every model step in a tool loop; recall only on step 1 of each user turn.
    if (payload?.invocationNum !== 1) return {};
    const text = recallForPrompt(
      root,
      config,
      payload?.lastUserInput,
      payload?.conversationId || "agy",
      cacheDir,
      now,
      env,
    );
    if (!text) return {};
    return { injectSteps: [{ systemMessage: { systemMessage: text } }] };
  }

  if (event === "PreToolUse") {
    const toolName = payload?.toolCall?.name ?? "";
    const args = payload?.toolCall?.args ?? {};
    const cwd = payload?.cwd || process.cwd();
    const denied = checkToolGuard(root, config, toolName, args, cwd, options.home);
    if (denied) return { decision: "deny", reason: denied };
    if (toolName === "run_command" && isSafeReadOnlyWikiCommand(args?.CommandLine)) {
      return { decision: "allow", reason: "Read-only default-space memory-wiki lookup" };
    }
    return { decision: "ask" };
  }

  return {};
}

/**
 * Claude Code hook handler (`SessionStart`, `UserPromptSubmit`, `PreToolUse`).
 * @param {Record<string, any>} payload parsed stdin JSON
 * @param {{ root?: string, config?: import("./config.js").Config, env?: NodeJS.ProcessEnv, cacheDir?: string, now?: number, home?: string }} [options]
 */
export function handleClaudeHook(payload, options = {}) {
  const env = options.env ?? process.env;
  const root = options.root ?? wikiRoot(env);
  const exists = fs.existsSync(root);
  const config = options.config ?? (exists ? loadConfig(root) : { human: "human:user", areas: {}, localProviders: [], nowLimit: 2000 });
  const cacheDir = options.cacheDir ?? hookCacheDir(env);
  const now = options.now ?? Date.now();
  const event = payload?.hook_event_name ?? "";

  if (event === "SessionStart") {
    if (!exists) return {};
    const text = formatNowStart(readNow(root, config, { areas: ["default"], now, env }));
    if (!text) return {};
    return { hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: text } };
  }

  if (event === "UserPromptSubmit") {
    const text = recallForPrompt(
      root,
      config,
      payload?.prompt,
      payload?.session_id || "claude",
      cacheDir,
      now,
      env,
    );
    if (!text) return {};
    return { hookSpecificOutput: { hookEventName: "UserPromptSubmit", additionalContext: text } };
  }

  if (event === "PreToolUse") {
    const toolName = payload?.tool_name ?? "";
    const input = payload?.tool_input ?? {};
    const cwd = payload?.cwd || process.cwd();
    const denied = checkToolGuard(root, config, toolName, input, cwd, options.home);
    if (denied) {
      return {
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: "deny",
          permissionDecisionReason: denied,
        },
      };
    }
    if (toolName === "Bash" && isSafeReadOnlyWikiCommand(input?.command)) {
      return {
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: "allow",
          permissionDecisionReason: "Read-only default-space memory-wiki lookup",
        },
      };
    }
    return { decision: "ask" };
  }

  return {};
}
