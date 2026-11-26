import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import {
  DREAM_MARKER,
  LOCAL_CONFIG_FILE,
  buildDreamContext,
  capture,
  concepts,
  dreamStatus,
  extractJsonObject,
  harvestTranscripts,
  inOffHoursWindow,
  loadConfig,
  loadDreamState,
  readNow,
  renderLaunchAgentPlist,
  runDream,
  undoDream,
  verify,
  writeNow,
} from "../src/index.js";
import { NOW, concept, read, run, tempWiki, write } from "./helpers.js";

const BIN = fileURLToPath(new URL("../bin/wiki.js", import.meta.url));

function makeTempDir(t, prefix) {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), prefix)));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

const jsonl = (lines) => `${lines.map((obj) => JSON.stringify(obj)).join("\n")}\n`;

test("loadConfig parses dream defaults, overlays .memory-wiki.local.yml, and validates settings", (t) => {
  const { root, config } = tempWiki(t);
  assert.equal(config.dream.window, "02:00-06:00");
  assert.equal(config.dream.model, "openai/gpt-6.1-terra");
  assert.equal(config.dream.thinking, "medium");
  assert.deepEqual(config.dream.slots, ["03:15", "05:00"]);

  write(
    root,
    LOCAL_CONFIG_FILE,
    `dream:\n  window: "01:30-05:30"\n  max_writes: 4\n  model: openai/gpt-6.2-sol\n  thinking: high\n  exclude_paths: ["~/work"]\n`,
  );
  const overlaid = loadConfig(root);
  assert.equal(overlaid.dream.window, "01:30-05:30");
  assert.equal(overlaid.dream.maxWrites, 4);
  assert.equal(overlaid.dream.model, "openai/gpt-6.2-sol");
  assert.equal(overlaid.dream.thinking, "high");
  assert.deepEqual(overlaid.dream.excludePaths, ["~/work"]);

  write(root, LOCAL_CONFIG_FILE, `dream:\n  window: "25:00-06:00"\n`);
  assert.throws(() => loadConfig(root), /dream\.window/);

  write(root, LOCAL_CONFIG_FILE, `dream:\n  slots: ["99:00"]\n`);
  assert.throws(() => loadConfig(root), /dream\.slots/);
});

test("inOffHoursWindow handles standard and midnight-wrapping local windows", () => {
  const at = (h, m) => new Date(2026, 10, 25, h, m, 0);
  assert.equal(inOffHoursWindow("02:00-06:00", at(2, 0)), true);
  assert.equal(inOffHoursWindow("02:00-06:00", at(5, 59)), true);
  assert.equal(inOffHoursWindow("02:00-06:00", at(6, 0)), false);
  assert.equal(inOffHoursWindow("23:00-05:00", at(23, 30)), true);
  assert.equal(inOffHoursWindow("23:00-05:00", at(12, 0)), false);
});

test("harvestTranscripts parses pi/omp/agy, follows active branch, deduplicates symlinks, and drops tainted or excluded sessions", (t) => {
  const { root, config } = tempWiki(
    t,
    {},
    { areas: { personal: { passphrase: ["true"] }, work: { passphrase: ["true"] } } },
  );
  config.dream.excludePaths = ["/Users/tester/secret-work"];

  const fakeHome = makeTempDir(t, "dream-home-");
  const piDir = path.join(fakeHome, "pi-sessions");
  const piAliasDir = path.join(fakeHome, "pi-sessions-link");
  fs.mkdirSync(path.join(piDir, "proj-a"), { recursive: true });
  fs.symlinkSync(piDir, piAliasDir);

  const ompDir = path.join(fakeHome, "omp-sessions");
  const agyDir = path.join(fakeHome, "agy-brain");
  fs.mkdirSync(path.join(ompDir, "proj-b"), { recursive: true });
  fs.mkdirSync(path.join(agyDir, "conv-1", ".system_generated", "logs"), { recursive: true });
  fs.mkdirSync(path.join(agyDir, "conv-self", ".system_generated", "logs"), { recursive: true });

  const ts1 = "2026-11-21T10:00:00Z";
  const ts2 = "2026-11-21T10:01:00Z";
  const ts3 = "2026-11-21T10:02:00Z";

  fs.writeFileSync(
    path.join(piDir, "proj-a", "clean.jsonl"),
    jsonl([
      { type: "session", version: 3, id: "pi-clean", timestamp: ts1, cwd: "/Users/tester/src/clio" },
      { type: "session_info", id: "n1", timestamp: ts1, name: "Clio Sendspin notes" },
      { type: "message", id: "u1", parentId: "", timestamp: ts1, message: { role: "user", content: [{ type: "text", text: "Explain how Sendspin syncs playback in clio." }] } },
      { type: "message", id: "a-old", parentId: "u1", timestamp: ts2, message: { role: "assistant", content: [{ type: "text", text: "ABANDONED_BRANCH_TEXT" }] } },
      { type: "message", id: "a-new", parentId: "u1", timestamp: ts2, message: { role: "assistant", content: [{ type: "thinking", thinking: "HIDDEN_THINKING" }, { type: "text", text: "Sendspin streams audio from Music Assistant." }] } },
      { type: "message", id: "tr1", parentId: "a-new", timestamp: ts3, message: { role: "toolResult", content: [{ type: "text", text: "RAW_TOOL_NOISE" }] } },
    ]),
  );
  fs.writeFileSync(
    path.join(piDir, "proj-a", "tainted.jsonl"),
    jsonl([
      { type: "session", version: 3, id: "pi-tainted", timestamp: ts1, cwd: "/Users/tester/src/clio" },
      { type: "custom", customType: "memory-private", data: { tainted: true }, id: "c1", timestamp: ts1 },
      { type: "message", id: "u1", parentId: "c1", timestamp: ts2, message: { role: "user", content: [{ type: "text", text: "PRIVATE_SECRET_NEVER_HARVEST" }] } },
    ]),
  );
  fs.writeFileSync(
    path.join(ompDir, "proj-b", "excluded.jsonl"),
    jsonl([
      { type: "session", version: 3, id: "omp-work", timestamp: ts1, cwd: "/Users/tester/secret-work/repo" },
      { type: "message", id: "u1", parentId: "", timestamp: ts2, message: { role: "user", content: [{ type: "text", text: "EXCLUDED_WORK_CONTENT" }] } },
    ]),
  );
  fs.writeFileSync(
    path.join(agyDir, "conv-1", ".system_generated", "logs", "transcript.jsonl"),
    jsonl([
      { type: "USER_INPUT", created_at: ts1, content: "<USER_REQUEST>\nSwitch Tessera tiles to use ULID keys.\n</USER_REQUEST>\n<ADDITIONAL_METADATA>ignored</ADDITIONAL_METADATA>" },
      { type: "PLANNER_RESPONSE", created_at: ts2, tool_calls: [{ name: "run_command" }], content: "INTERMEDIATE_TOOL_STEP" },
      { type: "PLANNER_RESPONSE", created_at: ts3, content: "Updated Tessera tile IDs to ULIDs." },
    ]),
  );
  fs.writeFileSync(
    path.join(agyDir, "conv-self", ".system_generated", "logs", "transcript.jsonl"),
    jsonl([{ type: "USER_INPUT", created_at: ts1, content: `${DREAM_MARKER}\nConsolidate wiki.` }]),
  );

  // Set mtimes slightly before nowMs so a subsequent pass with lastRunAt = nowMs skips them in O(1).
  const fileMtime = new Date("2026-11-21T11:00:00Z");
  for (const rel of [
    path.join(piDir, "proj-a", "clean.jsonl"),
    path.join(piDir, "proj-a", "tainted.jsonl"),
    path.join(ompDir, "proj-b", "excluded.jsonl"),
    path.join(agyDir, "conv-1", ".system_generated", "logs", "transcript.jsonl"),
    path.join(agyDir, "conv-self", ".system_generated", "logs", "transcript.jsonl"),
  ]) {
    fs.utimesSync(rel, fileMtime, fileMtime);
  }

  const nowMs = Date.parse("2026-11-21T12:00:00Z");
  const result = harvestTranscripts(
    root,
    config,
    {},
    { now: nowMs, home: fakeHome, sourceDirs: { pi: [piDir, piAliasDir], omp: [ompDir], agy: [agyDir] } },
  );

  assert.equal(result.skippedTainted, 1);
  assert.equal(result.skippedExcluded, 1);
  assert.equal(result.sessions.length, 2, "clean pi (deduped) + agy");
  assert.match(result.digestText, /Sendspin streams audio/);
  assert.match(result.digestText, /Switch Tessera tiles to use ULID keys/);
  assert.doesNotMatch(result.digestText, /ABANDONED_BRANCH_TEXT|HIDDEN_THINKING|RAW_TOOL_NOISE|PRIVATE_SECRET|EXCLUDED_WORK|INTERMEDIATE_TOOL_STEP|ADDITIONAL_METADATA/);

  const second = harvestTranscripts(
    root,
    config,
    { lastRunAt: new Date(nowMs).toISOString() },
    { now: nowMs + 60_000, home: fakeHome, sourceDirs: { pi: [piDir, piAliasDir], omp: [ompDir], agy: [agyDir] } },
  );
  assert.equal(second.scannedFiles, 0);
  assert.equal(second.sessions.length, 0);
});

test("buildDreamContext detects local repo source drift and short-circuits quiet nights", (t) => {
  const reposDir = makeTempDir(t, "dream-repos-");
  const demoRepo = path.join(reposDir, "demo");
  fs.mkdirSync(demoRepo, { recursive: true });
  run("git", ["-C", demoRepo, "init", "--quiet"]);
  fs.writeFileSync(path.join(demoRepo, "README.md"), "# Demo\n\nOriginal behavior.\n");
  run("git", ["-C", demoRepo, "add", "README.md"]);
  run("git", ["-C", demoRepo, "commit", "--quiet", "-m", "initial"], {
    env: { ...process.env, GIT_AUTHOR_DATE: "2026-11-10T10:00:00Z", GIT_COMMITTER_DATE: "2026-11-10T10:00:00Z" },
  });

  const { root, config } = tempWiki(t, {
    "index.md": '---\nokf_version: "0.3"\ntitle: Test\n---\n',
    "now.md": '---\nstale_after: "2026-12-10"\n---\n\n## Focus\n\n* Building demo.\n',
    "projects/demo.md": concept(
      `type: Project\ntitle: Demo\ndescription: Demo app.\nstatus: stable\ngenerated: { by: "pi/m", at: "2026-11-12T00:00:00Z" }\nsources:\n  - resource: demo/README.md`,
      "Links to [Helper](../entities/helper.md).\n",
    ),
    "entities/helper.md": concept(
      `type: Entity\ntitle: Helper\ndescription: Helper tool.\nstatus: stable`,
      "Used by [Demo](../projects/demo.md).\n",
    ),
  });
  config.dream.repoRoots = [reposDir];

  const emptyHarvest = { sessions: [], digestText: "", substantiveTurns: 0, totalUserTurns: 0 };
  const recentState = { lastHygieneAt: "2026-11-20T00:00:00Z" };

  const quiet = buildDreamContext(root, config, emptyHarvest, recentState, { now: NOW.getTime() });
  assert.equal(quiet.driftedSources.length, 0);
  assert.equal(quiet.hasActionableWork, false);

  fs.writeFileSync(path.join(demoRepo, "README.md"), "# Demo\n\nUpdated architecture using Rust.\n");
  run("git", ["-C", demoRepo, "add", "README.md"]);
  run("git", ["-C", demoRepo, "commit", "--quiet", "-m", "switch demo to Rust"], {
    env: { ...process.env, GIT_AUTHOR_DATE: "2026-11-20T10:00:00Z", GIT_COMMITTER_DATE: "2026-11-20T10:00:00Z" },
  });

  const drifted = buildDreamContext(root, config, emptyHarvest, recentState, { now: NOW.getTime() });
  assert.equal(drifted.driftedSources.length, 1);
  assert.match(drifted.driftedSources[0].excerpt, /Updated architecture using Rust/);
  assert.equal(drifted.hasActionableWork, true);
});

test("runDream enforces guards, dry-run, guardrails, direct stable demotion, now.md auto-write, and undoDream", async (t) => {
  const { root, config } = tempWiki(
    t,
    { "index.md": '---\nokf_version: "0.3"\ntitle: Dream Test\n---\n' },
    { git: true, areas: { work: { passphrase: ["true"] } } },
  );
  config.dream.repoRoots = [];
  config.dream.minTurns = 1;

  const stableEntry = capture(
    root,
    config,
    { by: "pi/m", type: "Entity", title: "Hyperion", description: "Old description of Hyperion." },
    { now: new Date("2026-11-20T10:00:00Z") },
  );
  verify(root, config, stableEntry.id, { now: new Date("2026-11-20T11:00:00Z") });
  capture(
    root,
    config,
    { by: "pi/m", type: "Concept", title: "Legacy Flow", description: "Old workflow." },
    { now: new Date("2026-11-20T10:00:00Z") },
  );

  const cacheDir = makeTempDir(t, "dream-cache-");
  const piDir = makeTempDir(t, "dream-pi-");
  fs.writeFileSync(
    path.join(piDir, "s1.jsonl"),
    jsonl([
      { type: "session", version: 3, id: "s-1", timestamp: "2026-11-21T01:00:00Z", cwd: "/tmp/proj" },
      { type: "message", id: "u1", parentId: "", timestamp: "2026-11-21T01:00:00Z", message: { role: "user", content: [{ type: "text", text: "We replaced Legacy Flow with Dream Loop on Hyperion." }] } },
      { type: "message", id: "a1", parentId: "u1", timestamp: "2026-11-21T01:01:00Z", message: { role: "assistant", content: [{ type: "text", text: "Recorded Dream Loop." }] } },
    ]),
  );

  const daytime = new Date(2026, 10, 21, 9, 30, 0);
  const night = new Date(2026, 10, 21, 3, 15, 0);

  assert.match(
    (await runDream(root, config, { now: daytime, cacheDir, sourceDirs: { pi: [piDir], omp: [], agy: [] }, checkPower: () => ({ onAc: true, detail: "AC Power" }) })).reason,
    /outside off-hours window/,
  );
  assert.match(
    (await runDream(root, config, { now: night, cacheDir, sourceDirs: { pi: [piDir], omp: [], agy: [] }, checkPower: () => ({ onAc: false, detail: "Battery Power" }) })).reason,
    /Battery Power/,
  );

  const mockInvoke = async () =>
    `\`\`\`json\n${JSON.stringify({
      summary: "Added Dream Loop, updated Hyperion, deprecated Legacy Flow, and initialized now.md.",
      creates: [
        { type: "Concept", title: "Dream Loop", description: "Nightly consolidation pass.", tags: ["memory-wiki"], sources: ["session:pi/s-1"], body: "Runs on [Hyperion](../entities/hyperion.md)." },
        { type: "Concept", title: "Leaked Secret", description: "Blocked.", body: "Use op://Private/secret/token here." },
        { type: "Concept", title: "Broken Target", description: "Blocked.", body: "Points to [Nothing](../concepts/does-not-exist.md)." },
        { type: "Concept", title: "Private Leak", description: "Blocked.", body: "See [Work Secret](../work/secret.md)." },
      ],
      updates: [{ id: "entities/hyperion", reason: "Updated host role.", description: "Main MacBook running Dream Loop.", body: "- Runs [Dream Loop](../concepts/dream-loop.md)." }],
      deprecations: [{ id: "concepts/legacy-flow", superseded_by: "dream-loop.md", reason: "Replaced by Dream Loop." }],
      now: "## Stack\n\n* **Hyperion**: Main Mac.\n\n## Focus\n\n* Nightly dreaming.",
      notes: ["Verify Hyperion after reviewing."],
    })}\n\`\`\``;

  const commitsBefore = run("git", ["-C", root, "rev-list", "--count", "HEAD"]).trim();
  const dry = await runDream(root, config, { now: daytime, dryRun: true, cacheDir, sourceDirs: { pi: [piDir], omp: [], agy: [] }, invokeModel: mockInvoke });
  assert.equal(dry.status, "dry-run");
  assert.equal(dry.applied.length, 4);
  assert.equal(dry.rejected.length, 3);
  assert.equal(run("git", ["-C", root, "rev-list", "--count", "HEAD"]).trim(), commitsBefore);
  assert.equal(loadDreamState(cacheDir).lastRunAt, null);

  const live = await runDream(root, config, {
    now: night,
    cacheDir,
    sourceDirs: { pi: [piDir], omp: [], agy: [] },
    checkPower: () => ({ onAc: true, detail: "AC Power" }),
    invokeModel: mockInvoke,
  });
  assert.equal(live.status, "completed");
  assert.equal(live.commits.length, 4);
  assert.equal(live.lintErrors.length, 0);

  const hyperion = concepts(root, config).find((c) => c.id === "entities/hyperion");
  assert.equal(hyperion.status.status, "draft");
  assert.match(read(root, "entities/hyperion.md"), /verified:\n {2}- \{ by: "human:tester"/);
  assert.equal(concepts(root, config).find((c) => c.id === "concepts/legacy-flow").status.status, "deprecated");
  assert.match(readNow(root, config, { now: night.getTime() })[0].text, /Nightly dreaming/);

  const cooled = await runDream(root, config, {
    now: new Date(2026, 10, 21, 5, 0, 0),
    cacheDir,
    sourceDirs: { pi: [piDir], omp: [], agy: [] },
    checkPower: () => ({ onAc: true, detail: "AC Power" }),
    invokeModel: mockInvoke,
  });
  assert.equal(cooled.status, "skipped");
  assert.match(cooled.reason, /cooldown/);

  assert.match(dreamStatus(root, config, { cacheDir }).lastDigest, /Dream Loop/);

  const undone = undoDream(root, config, { cacheDir });
  assert.equal(undone.reverted.length, 4);
  assert.equal(fs.existsSync(path.join(root, "concepts/dream-loop.md")), false);
  assert.equal(fs.existsSync(path.join(root, "now.md")), false);
  assert.equal(concepts(root, config).find((c) => c.id === "entities/hyperion").status.status, "stable");
  assert.throws(() => undoDream(root, config, { cacheDir }), /no dream run with commits to undo/);
});

test("now.md is preserved when fresh and auto-written when stale", async (t) => {
  const { root, config } = tempWiki(t, {}, { git: true });
  config.dream.repoRoots = [];
  const cacheDir = makeTempDir(t, "dream-now-cache-");
  writeNow(root, config, { text: "## Focus\n\n* Current focus.", staleAfter: "2026-12-01", now: NOW });

  const mockNow = async () =>
    JSON.stringify({ summary: "Try now", creates: [], updates: [], deprecations: [], now: "## Focus\n\n* Replaced." });

  const fresh = await runDream(root, config, { now: NOW, bypassGuards: true, force: true, cacheDir, sourceDirs: { pi: [], omp: [], agy: [] }, invokeModel: mockNow });
  assert.equal(fresh.applied.length, 0);
  assert.match(readNow(root, config, { now: NOW.getTime() })[0].text, /Current focus/);

  const afterStale = new Date("2026-12-05T03:15:00Z");
  const stale = await runDream(root, config, { now: afterStale, bypassGuards: true, cacheDir, sourceDirs: { pi: [], omp: [], agy: [] }, invokeModel: mockNow });
  assert.equal(stale.applied.length, 1);
  assert.match(readNow(root, config, { now: afterStale.getTime() })[0].text, /Replaced/);
});

test("renderLaunchAgentPlist, extractJsonObject, and CLI dream commands behave as expected", (t) => {
  const xml = renderLaunchAgentPlist({
    nodeBin: "/Users/ada/.local/share/mise/shims/node",
    wikiBin: "/Users/ada/src/memory-wiki/bin/wiki.js",
    slots: ["03:15", "05:00"],
    logPath: "/Users/ada/.cache/memory-wiki/dream/launchd.log",
    pathEnv: "/usr/local/bin:/usr/bin:/bin",
    wikiHome: null,
  });
  assert.match(xml, /<string>local\.memory-wiki\.dream<\/string>/);
  assert.match(xml, /<string>\/usr\/bin\/caffeinate<\/string>/);
  assert.match(xml, /<key>MEMORY_WIKI_DEFAULT_ONLY<\/key>\s*<string>1<\/string>/);
  assert.deepEqual(extractJsonObject('```json\n{"summary":"ok"}\n```'), { summary: "ok" });

  const { root } = tempWiki(t, {}, { git: true, areas: { work: { passphrase: ["true"] } } });
  const cacheHome = makeTempDir(t, "dream-cli-cache-");
  const cli = (args) =>
    spawnSync(process.execPath, [BIN, ...args], {
      encoding: "utf8",
      env: { ...process.env, MEMORY_WIKI_HOME: root, XDG_CACHE_HOME: cacheHome },
    });

  const statusRes = cli(["dream", "status", "--json"]);
  assert.equal(statusRes.status, 0, statusRes.stderr);
  assert.equal(JSON.parse(statusRes.stdout).config.window, "02:00-06:00");

  const privateRefused = cli(["dream", "--area", "work"]);
  assert.equal(privateRefused.status, 2);
  assert.match(privateRefused.stderr, /default space only/);
});
