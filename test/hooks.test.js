import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { handleAgyHook, handleClaudeHook } from "../src/hooks.js";
import { NOW, concept, tempWiki, write } from "./helpers.js";

const BIN = fileURLToPath(new URL("../bin/wiki.js", import.meta.url));

test("agy hooks inject now.md at SessionStart, recall once per turn with revision dedup, and guard PreToolUse", (t) => {
  const { root, config } = tempWiki(
    t,
    {
      "now.md": '---\nstale_after: "2026-11-20"\n---\n\n## Stack\n\n* **Hyperion**: Mac Studio.\n',
      "concepts/prr.md": concept(
        "type: Concept\ntitle: PRR\ndescription: Production readiness review.",
      ),
      "entities/hyperion.md": concept(
        "type: Entity\ntitle: Hyperion\ndescription: Main workstation.",
      ),
    },
    { areas: { work: { passphrase: ["true"] } } },
  );
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "memory-wiki-hook-cache-"));
  t.after(() => fs.rmSync(cacheDir, { recursive: true, force: true }));
  const opts = { root, config, cacheDir, now: NOW.getTime(), home: "/Users/ada" };

  // SessionStart injects now.md without date-flipping staleness wording.
  const start = handleAgyHook({ hookEventName: "SessionStart", conversationId: "c1" }, opts);
  const startText = start.injectSteps[0].systemMessage.systemMessage;
  assert.match(startText, /Hyperion.*Mac Studio/);
  assert.doesNotMatch(startText, /stale|refresh/);

  // PreInvocation ignores tool-loop steps (invocationNum > 1).
  assert.deepEqual(
    handleAgyHook(
      { hookEventName: "PreInvocation", conversationId: "c1", invocationNum: 2, lastUserInput: "PRR?" },
      opts,
    ),
    {},
  );

  // Step 1 recalls PRR, skips Hyperion (already in now.md), and dedupes on repeat.
  const first = handleAgyHook(
    {
      hookEventName: "PreInvocation",
      conversationId: "c1",
      invocationNum: 1,
      lastUserInput: "Run PRR on Hyperion",
    },
    opts,
  );
  assert.match(first.injectSteps[0].systemMessage.systemMessage, /PRR.*Production readiness review/);
  assert.doesNotMatch(first.injectSteps[0].systemMessage.systemMessage, /Main workstation/);
  assert.deepEqual(
    handleAgyHook(
      { hookEventName: "PreInvocation", conversationId: "c1", invocationNum: 1, lastUserInput: "PRR again" },
      opts,
    ),
    {},
    "already recalled in this conversation",
  );

  // Hand-editing description changes revision, triggering a fresh recall.
  write(
    root,
    "concepts/prr.md",
    concept("type: Concept\ntitle: PRR\ndescription: Launch readiness gate."),
  );
  const refreshed = handleAgyHook(
    { hookEventName: "PreInvocation", conversationId: "c1", invocationNum: 1, lastUserInput: "PRR?" },
    opts,
  );
  assert.match(refreshed.injectSteps[0].systemMessage.systemMessage, /Launch readiness gate/);

  // PreToolUse blocks wiki verify and private areas, allows clean read-only wiki calls.
  assert.equal(
    handleAgyHook(
      {
        hookEventName: "PreToolUse",
        toolCall: { name: "run_command", args: { CommandLine: "wiki --json verify concepts/prr" } },
      },
      opts,
    ).decision,
    "deny",
  );
  assert.equal(
    handleAgyHook(
      {
        hookEventName: "PreToolUse",
        toolCall: { name: "read_file", args: { Path: `${root}/work/secret.md` } },
      },
      opts,
    ).decision,
    "deny",
  );
  assert.equal(
    handleAgyHook(
      {
        hookEventName: "PreToolUse",
        toolCall: { name: "run_command", args: { CommandLine: "wiki lookup PRR" } },
      },
      opts,
    ).decision,
    "allow",
  );
  assert.deepEqual(
    handleAgyHook(
      {
        hookEventName: "PreToolUse",
        toolCall: { name: "run_command", args: { CommandLine: "git status" } },
      },
      opts,
    ),
    { decision: "ask" },
  );
  // File edits mentioning CLI commands in body text are allowed, while private targets are denied.
  assert.deepEqual(
    handleAgyHook(
      {
        hookEventName: "PreToolUse",
        toolCall: {
          name: "edit_file",
          args: {
            Path: "/Users/ada/src/memory-wiki/README.md",
            Target: "old",
            Replacement: "Run wiki area sync work or wiki verify concepts/prr",
          },
        },
      },
      opts,
    ),
    { decision: "ask" },
  );
  assert.equal(
    handleAgyHook(
      {
        hookEventName: "PreToolUse",
        toolCall: {
          name: "edit_file",
          args: {
            Path: `${root}/work/concepts/secret.md`,
            Target: "old",
            Replacement: "new",
          },
        },
      },
      opts,
    ).decision,
    "deny",
  );
  assert.equal(
    handleAgyHook(
      {
        hookEventName: "PreToolUse",
        toolCall: {
          name: "write_file",
          args: {
            Path: `${root}/.memory-wiki.local.yml`,
            Content: "sync: {}",
          },
        },
      },
      opts,
    ).decision,
    "deny",
  );
});

test("claude hooks mirror guardrails and prompt recall", (t) => {
  const { root, config } = tempWiki(
    t,
    { "concepts/prr.md": concept("type: Concept\ntitle: PRR\ndescription: Launch gate.") },
    { areas: { work: { passphrase: ["true"] } } },
  );
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "memory-wiki-claude-cache-"));
  t.after(() => fs.rmSync(cacheDir, { recursive: true, force: true }));
  const opts = { root, config, cacheDir, now: NOW.getTime() };

  const recalled = handleClaudeHook(
    { hook_event_name: "UserPromptSubmit", session_id: "s1", prompt: "Check PRR" },
    opts,
  );
  assert.match(recalled.hookSpecificOutput.additionalContext, /PRR.*Launch gate/);
  assert.deepEqual(
    handleClaudeHook(
      { hook_event_name: "UserPromptSubmit", session_id: "s1", prompt: "Check PRR again" },
      opts,
    ),
    {},
  );
  assert.equal(
    handleClaudeHook(
      {
        hook_event_name: "PreToolUse",
        tool_name: "Bash",
        tool_input: { command: "wiki lookup x --area default --area work" },
      },
      opts,
    ).hookSpecificOutput.permissionDecision,
    "deny",
  );
  assert.equal(
    handleClaudeHook(
      {
        hook_event_name: "PreToolUse",
        tool_name: "Bash",
        tool_input: { command: "wiki search gate" },
      },
      opts,
    ).hookSpecificOutput.permissionDecision,
    "allow",
  );
});

test("CLI prints search snippets, supports tag-only search, status, context, and hook agy", (t) => {
  const { root } = tempWiki(
    t,
    {
      "now.md": '---\nstale_after: "2026-12-01"\n---\n\n* Active focus.\n',
      "concepts/sparse.md": concept(
        "type: Concept\ntitle: Sparse bundle\ndescription: Band-file image.\ntags: [macos, disk]",
        "Created using hdiutil create.\n",
      ),
    },
    { git: true },
  );
  const cli = (args, input = "", extraEnv = {}) =>
    spawnSync(process.execPath, [BIN, ...args], {
      input,
      encoding: "utf8",
      env: { ...process.env, MEMORY_WIKI_HOME: root, ...extraEnv },
    });

  const searchRes = cli(["search", "hdiutil"]);
  assert.equal(searchRes.status, 0, searchRes.stderr);
  assert.match(searchRes.stdout, /> Created using hdiutil create\./);

  const tagOnly = cli(["search", "--tag", "macos", "--tag", "disk"]);
  assert.equal(tagOnly.status, 0, tagOnly.stderr);
  assert.match(tagOnly.stdout, /Sparse bundle/);

  const ctxRes = cli(["context"]);
  assert.equal(ctxRes.status, 0, ctxRes.stderr);
  const ctx = JSON.parse(ctxRes.stdout);
  assert.equal(ctx.concepts.length, 1);
  assert.equal(ctx.now[0].ref, "now.md");

  const hookRes = cli(
    ["hook", "agy"],
    JSON.stringify({ hookEventName: "SessionStart", conversationId: "cli-test" }),
  );
  assert.equal(hookRes.status, 0, hookRes.stderr);
  assert.match(JSON.parse(hookRes.stdout).injectSteps[0].systemMessage.systemMessage, /Active focus/);

  // Auto-infers --by from ANTIGRAVITY_AGENT when omitted.
  const capRes = cli(
    ["capture", "--type", "Entity", "--title", "Rex", "--description", "Terminal multiplexer."],
    "",
    { ANTIGRAVITY_AGENT: "1" },
  );
  assert.equal(capRes.status, 0, capRes.stderr);
  assert.match(fs.readFileSync(path.join(root, "entities/rex.md"), "utf8"), /antigravity\/cli/);
});
