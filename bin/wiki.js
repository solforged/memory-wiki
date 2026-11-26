#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import readline from "node:readline/promises";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

// Fast path for harness lifecycle hooks: avoids loading write/area/sync modules
// on every PreToolUse invocation.
if (process.argv[2] === "hook" && !process.argv.includes("--help") && !process.argv.includes("-h")) {
  const target = process.argv[3];
  if (target !== "agy" && target !== "claude") {
    process.stderr.write('wiki: hook expects "agy" or "claude"\nRun wiki --help for usage.\n');
    process.exit(2);
  }
  const [{ wikiRoot }, { handleAgyHook, handleClaudeHook }] = await Promise.all([
    import("../src/config.js"),
    import("../src/hooks.js"),
  ]);
  const raw = fs.readFileSync(0, "utf8").trim();
  let payload = {};
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch {
      process.stdout.write("{}\n");
      process.exit(0);
    }
  }
  const root = wikiRoot();
  const output =
    target === "agy" ? handleAgyHook(payload, { root }) : handleClaudeHook(payload, { root });
  process.stdout.write(`${JSON.stringify(output)}\n`);
  process.exit(0);
}

const wiki = await import("../src/index.js");

const USAGE = `Usage: wiki <command> [options]

Read
  lookup <name>        concepts with that title or alias; failing that, near names
  search [words]       full-text search  [--type T] [--tag TAG]... [--status S] [--stale] [--limit N]
  show <id>            one concept: status, trust tier, staleness and its text
  list                 concepts          [--type T] [--tag TAG]... [--status S] [--stale] [--limit N]
  recall [text]        stable and draft concepts whose names a text mentions (stdin without text)
  recent               newest log.md entries [--limit N]
  now                  show current context (now.md); --set replaces it from stdin or --body
  status               entry counts, drafts, now.md, and private area states
  context              machine-readable snapshot for agent harnesses (--json)

Write
  capture              create a concept, or update one with --id, as a draft
      [--by ACTOR] --type Concept|Entity|Project|Investigation --title TEXT --description TEXT
      [--alias NAME]... [--tag TAG]... [--source URL]... [--replace-lists]
      [--body TEXT | --body-file FILE|-] [--resource URI] [--stale-after DATE] [--slug NAME]
      [--preview] [--if-match SHA256]
  deprecate <id>       retire a concept [--by ACTOR] [--superseded-by PATH] [--preview] [--if-match SHA256]
  verify <id>          record that you confirmed a concept; asks on the terminal
  review               interactively review and verify pending drafts on the terminal
  index                regenerate index.md files after edits by hand

Maintain
  init                 create the wiki
  lint [--strict]      conformance, links, staleness; --strict also fails on warnings
  dream [run|status|undo|schedule]
                       off-hours default-space consolidation from local agent transcripts
      run:      [--now] [--dry-run] [--force] [--days N] [--model ID] [--thinking LVL]
      schedule: [status|install|uninstall]  (macOS launchd LaunchAgent)
  area [status]        private areas, and whether each is unlocked
  area create|unlock|lock|restore|sync <name>
      lock: [--all] [--force] [--sync]   sync: [--remote URL|DIR] [--pull|--push] [--force]
      restore: [--from HOST|FILE] [--remote URL|DIR]
  hook <agy|claude>    run a harness lifecycle hook (JSON on stdin -> stdout)
  root                 print the wiki's directory

Options
  --area NAME   "default" (when omitted), a private area, or "all"; repeatable.
                A private area is read only when named, and only while unlocked.
  --json        machine-readable output

The wiki is $MEMORY_WIKI_HOME, else memory-wiki under $XDG_DATA_HOME (~/.local/share).
Set MEMORY_WIKI_DEFAULT_ONLY=1 to forbid private areas in hosted-agent environments.`;

class UsageError extends Error {}

function parse(argv) {
  try {
    return parseArgs({
      args: argv,
      allowPositionals: true,
      options: {
        area: { type: "string", multiple: true },
        json: { type: "boolean" },
        help: { type: "boolean", short: "h" },
        type: { type: "string" },
        tag: { type: "string", multiple: true },
        status: { type: "string" },
        stale: { type: "boolean" },
        limit: { type: "string" },
        by: { type: "string" },
        id: { type: "string" },
        title: { type: "string" },
        description: { type: "string" },
        alias: { type: "string", multiple: true },
        source: { type: "string", multiple: true },
        "replace-lists": { type: "boolean" },
        body: { type: "string" },
        "body-file": { type: "string" },
        resource: { type: "string" },
        "stale-after": { type: "string" },
        slug: { type: "string" },
        "superseded-by": { type: "string" },
        set: { type: "boolean" },
        preview: { type: "boolean" },
        "if-match": { type: "string" },
        strict: { type: "boolean" },
        all: { type: "boolean" },
        force: { type: "boolean" },
        sync: { type: "boolean" },
        remote: { type: "string" },
        from: { type: "string" },
        host: { type: "string" },
        pull: { type: "boolean" },
        push: { type: "boolean" },
        "allow-dirty": { type: "boolean" },
        now: { type: "boolean" },
        "dry-run": { type: "boolean" },
        days: { type: "string" },
        model: { type: "string" },
        thinking: { type: "string" },
      },
    });
  } catch (error) {
    throw new UsageError(error.message);
  }
}

function inferActor(explicit, env = process.env) {
  if (explicit?.trim()) return explicit.trim();
  if (env.MEMORY_WIKI_ACTOR?.trim()) return env.MEMORY_WIKI_ACTOR.trim();
  if (env.ANTIGRAVITY_AGENT) return "antigravity/cli";
  if (env.CLAUDECODE) return "claude-code/cli";
  if (env.PI_CODING_AGENT_DIR) return "pi/cli";
  if (env.OMP_PROFILE) return "omp/cli";
  return "";
}

const print = (value) => process.stdout.write(`${value}\n`);
const json = (value) => print(JSON.stringify(value, null, 2));

function parseLimit(raw, fallback) {
  if (raw === undefined) return fallback;
  const limit = Number(raw);
  if (!Number.isInteger(limit) || limit < 1) {
    throw new UsageError("--limit takes a positive number");
  }
  return limit;
}

async function main() {
  const { values: flags, positionals } = parse(process.argv.slice(2));
  const [command, ...rest] = positionals;
  if (flags.help || !command) {
    print(USAGE);
    return flags.help ? 0 : 2;
  }
  const root = wiki.wikiRoot();
  if (command === "root") {
    print(root);
    return 0;
  }
  if (command === "hook") {
    const [target] = rest;
    if (target !== "agy" && target !== "claude") {
      throw new UsageError('hook expects "agy" or "claude"');
    }
    const raw = fs.readFileSync(0, "utf8").trim();
    let payload = {};
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch {
        print("{}");
        return 0;
      }
    }
    const output =
      target === "agy" ? wiki.handleAgyHook(payload, { root }) : wiki.handleClaudeHook(payload, { root });
    print(JSON.stringify(output));
    return 0;
  }
  if (command === "init") {
    wiki.init(root);
    print(`Created the wiki in ${root}`);
    return 0;
  }
  if (!fs.existsSync(root)) throw new Error(`no wiki at ${root}; run: wiki init`);
  const config = wiki.loadConfig(root);
  const areas = flags.area ?? ["default"];
  const now = Date.now();
  const describe = (file) => wiki.describe(file, now);
  const read = () => wiki.concepts(root, config, { areas });
  const text = () => {
    if (rest.length === 0) throw new UsageError(`${command} needs an argument`);
    return rest.join(" ");
  };
  const show = (infos, none) =>
    print(infos.length > 0 ? infos.map(wiki.formatSummary).join("\n") : none);

  switch (command) {
    case "lookup": {
      const found = wiki.lookup(read(), text());
      const exact = found.exact.map(describe);
      const candidates = found.candidates.map(describe);
      if (flags.json) json({ term: text(), exact, candidates });
      else if (exact.length > 0) show(exact);
      else {
        print(`Nothing is named "${text()}".`);
        if (candidates.length > 0)
          print(`Near names:\n${candidates.map(wiki.formatSummary).join("\n")}`);
      }
      return 0;
    }
    case "search": {
      const limit = parseLimit(flags.limit, 10);
      const query = rest.join(" ");
      if (!query.trim() && !flags.type && !flags.tag?.length && !flags.status && !flags.stale) {
        throw new UsageError("search needs words or a filter (--type, --tag, --status, --stale)");
      }
      const results = wiki
        .search(read(), query, {
          type: flags.type,
          tags: flags.tag,
          status: flags.status,
          stale: flags.stale,
          limit,
          now,
        })
        .map(({ file, score, snippet }) => ({ ...describe(file), score, snippet }));
      if (flags.json) json(results);
      else if (results.length === 0) print("No concept matches.");
      else {
        print(
          results
            .map((result) =>
              result.snippet
                ? `${wiki.formatSummary(result)}\n  > ${result.snippet}`
                : wiki.formatSummary(result),
            )
            .join("\n"),
        );
      }
      return 0;
    }
    case "list": {
      const limit = flags.limit !== undefined ? parseLimit(flags.limit, 0) : undefined;
      const wantedTags = (flags.tag ?? []).map((item) => item.toLowerCase());
      let infos = read()
        .filter((file) => !flags.type || file.type.toLowerCase() === flags.type.toLowerCase())
        .filter((file) => !flags.status || file.status.status.toLowerCase() === flags.status.toLowerCase())
        .filter((file) => {
          if (wantedTags.length === 0) return true;
          const fileTags = new Set(file.tags.map((item) => item.toLowerCase()));
          return wantedTags.every((wanted) => fileTags.has(wanted));
        })
        .map(describe)
        .filter((info) => !flags.stale || Boolean(info.stale));
      if (limit !== undefined) infos = infos.slice(0, limit);
      if (flags.json) json(infos);
      else show(infos, "The wiki has no matching concepts.");
      return 0;
    }
    case "recall": {
      const prompt = rest.length > 0 ? rest.join(" ") : fs.readFileSync(0, "utf8");
      const nowText = wiki
        .readNow(root, config, { areas, now })
        .map((page) => page.text)
        .join("\n");
      const infos = wiki.recall(read(), prompt, { nowText }).map(describe);
      if (flags.json) json(infos);
      else if (infos.length > 0) print(wiki.formatRecall(infos));
      return 0;
    }
    case "recent": {
      const limit = parseLimit(flags.limit, 15);
      const items = wiki.readRecent(root, config, { areas, limit });
      if (flags.json) json(items);
      else if (items.length === 0) print("No log entries yet.");
      else {
        print(
          items
            .map((item) =>
              item.area === "default"
                ? `${item.date}  ${item.entry}`
                : `${item.date}  [${item.area}] ${item.entry}`,
            )
            .join("\n"),
        );
      }
      return 0;
    }
    case "now": {
      if (flags.set) {
        const body =
          flags.body !== undefined
            ? flags.body
            : flags["body-file"] !== undefined
              ? fs.readFileSync(flags["body-file"] === "-" ? 0 : flags["body-file"], "utf8")
              : rest.length > 0
                ? rest.join(" ")
                : fs.readFileSync(0, "utf8");
        const area = flags.area?.[0] ?? "default";
        const result = wiki.writeNow(root, config, {
          text: body,
          staleAfter: flags["stale-after"],
          area,
          areas: flags.area ?? ["default"],
          preview: Boolean(flags.preview),
          ifMatch: flags["if-match"],
        });
        if (flags.json || flags.preview) json(result);
        else print(`Updated ${result.ref} (${result.bytes}/${result.limit} bytes, refresh ${result.staleAfter}).`);
        return 0;
      }
      const pages = wiki.readNow(root, config, { areas, now });
      if (flags.json) json(pages);
      else if (pages.length === 0) print("No now.md in selected areas.");
      else {
        print(
          pages
            .map((page) => {
              const flagsText = [
                page.stale === true ? "stale" : page.stale === "malformed" ? "stale_after unreadable" : "",
                page.truncated ? "truncated" : "",
              ]
                .filter(Boolean)
                .join(", ");
              const head = `${page.ref} (${page.bytes}/${page.limit} bytes${page.staleAfter ? `, refresh ${page.staleAfter}` : ""}${flagsText ? `, ${flagsText}` : ""})`;
              return `${head}\n\n${page.text}`;
            })
            .join("\n\n---\n\n"),
        );
      }
      return 0;
    }
    case "status": {
      const all = read().map(describe);
      const drafts = all.filter((info) => info.status === "draft").length;
      const stale = all.filter((info) => Boolean(info.stale)).length;
      const nowPages = wiki.readNow(root, config, { areas, now });
      const areaStates = wiki.areaStatus(root, config);
      if (flags.json) {
        json({
          root,
          defaultOnly: wiki.defaultOnly(),
          entries: all.length,
          drafts,
          stale,
          now: nowPages,
          areas: areaStates,
        });
      } else {
        const lines = [`${root}: ${all.length} entries, ${drafts} drafts, ${stale} stale`];
        for (const page of nowPages) {
          const marks = [page.stale && "stale", page.truncated && "cut off"].filter(Boolean).join(", ");
          lines.push(
            `${page.ref}: ${page.bytes}/${page.limit} bytes, refresh ${page.staleAfter ?? "never"}${marks ? ` (${marks})` : ""}`,
          );
        }
        for (const area of areaStates) {
          lines.push(
            `${area.name}: ${!area.exists ? "not created" : area.mounted ? "unlocked" : "locked"}`,
          );
        }
        print(lines.join("\n"));
      }
      return 0;
    }
    case "context": {
      const all = read();
      const payload = {
        root,
        defaultOnly: wiki.defaultOnly(),
        localProviders: config.localProviders,
        now: wiki.readNow(root, config, { areas, now }),
        concepts: all.map(describe),
        areas: wiki.areaStatus(root, config).map(({ name, exists, mounted }) => ({
          name,
          exists,
          mounted,
        })),
      };
      json(payload);
      return 0;
    }
    case "show": {
      const file = wiki.findConcept(read(), root, text());
      if (!file) throw new Error(`no concept "${text()}" in ${areas.join(", ")}`);
      if (flags.json) json({ ...describe(file), frontmatter: file.fm, body: file.body });
      else
        print(
          `${wiki.formatSummary(describe(file))}\n\n${fs.readFileSync(file.abs, "utf8").trimEnd()}`,
        );
      return 0;
    }
    case "capture": {
      const by = inferActor(flags.by);
      if (!by) throw new UsageError("capture needs --by <producer>/<version> or <scheme>:<id>");
      let body = flags.body;
      if (flags["body-file"] !== undefined) {
        body = fs.readFileSync(flags["body-file"] === "-" ? 0 : flags["body-file"], "utf8");
      }
      const captureAreas = flags.area ?? ["default"];
      const result = wiki.capture(
        root,
        config,
        {
          by,
          id: flags.id,
          type: flags.type,
          title: flags.title,
          description: flags.description,
          body,
          aliases: flags.alias,
          tags: flags.tag,
          sources: flags.source,
          replaceLists: Boolean(flags["replace-lists"]),
          resource: flags.resource,
          staleAfter: flags["stale-after"],
          slug: flags.slug,
          area: flags.area?.[0],
        },
        {
          areas: captureAreas,
          preview: Boolean(flags.preview),
          ifMatch: flags["if-match"],
        },
      );
      if (flags.json || flags.preview) json(result);
      else
        print(`${result.created ? "Created" : "Updated"} ${result.id} as a draft: ${result.path}`);
      return 0;
    }
    case "deprecate": {
      const by = inferActor(flags.by);
      if (!by) throw new UsageError("deprecate needs --by <producer>/<version> or <scheme>:<id>");
      const result = wiki.deprecate(root, config, text(), {
        by,
        supersededBy: flags["superseded-by"],
        areas,
        preview: Boolean(flags.preview),
        ifMatch: flags["if-match"],
      });
      if (flags.json || flags.preview) json(result);
      else print(`Deprecated ${result.id}${result.superseded_by ? ` (superseded by ${result.superseded_by})` : ""}`);
      return 0;
    }
    case "verify": {
      const verifyAreas = flags.area ?? (wiki.defaultOnly() ? ["default"] : ["all"]);
      const file = wiki.findConcept(wiki.concepts(root, config, { areas: verifyAreas }), root, text());
      if (!file) throw new Error(`no concept "${text()}"`);
      if (process.env.MEMORY_WIKI_HUMAN_CONFIRMED !== "1") {
        if (!process.stdin.isTTY || !process.stdout.isTTY) {
          throw new Error("verify records a person's review, so it needs a terminal");
        }
        print(
          `${wiki.formatSummary(describe(file))}\n  written by ${file.fm.generated?.by ?? "unknown"}\n`,
        );
        const prompt = readline.createInterface({ input: process.stdin, output: process.stdout });
        const answer = await prompt.question(
          `Record ${config.human} as having confirmed this? [y/N] `,
        );
        prompt.close();
        if (!/^y(es)?$/i.test(answer.trim())) {
          print("Not verified.");
          return 1;
        }
      }
      const result = wiki.verify(root, config, file.id, { areas: verifyAreas });
      print(`Verified ${result.id}: ${result.status}, ${result.tier}`);
      return 0;
    }
    case "review": {
      if (!process.stdin.isTTY || !process.stdout.isTTY) {
        throw new Error("review records a person's review, so it needs a terminal");
      }
      const reviewAreas = flags.area ?? ["default"];
      const drafts = wiki
        .concepts(root, config, { areas: reviewAreas })
        .filter((file) => file.status.status === "draft");
      if (drafts.length === 0) {
        print("No drafts awaiting verification.");
        return 0;
      }
      const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
      let verifiedCount = 0;
      try {
        for (let i = 0; i < drafts.length; i++) {
          const file = drafts[i];
          print(`\n[${i + 1}/${drafts.length}] ${wiki.formatSummary(describe(file))}`);
          print(`  written by ${file.fm.generated?.by ?? "unknown"}`);
          const dir = file.area === "default" ? root : `${root}/${file.area}`;
          const relInArea = file.area === "default" ? file.rel : file.rel.slice(file.area.length + 1);
          const diff = spawnSync("git", ["-C", dir, "log", "-n", "1", "-p", "--", relInArea], {
            encoding: "utf8",
          });
          if (diff.status === 0 && diff.stdout.trim()) {
            const snippet = diff.stdout.trim().split("\n").slice(0, 40).join("\n");
            print(`\n${snippet}\n`);
          } else {
            print(`\n${fs.readFileSync(file.abs, "utf8").trimEnd()}\n`);
          }
          const answer = (
            await rl.question(`Verify as ${config.human}? [v]erify / [s]kip / [q]uit: `)
          )
            .trim()
            .toLowerCase();
          if (answer === "q" || answer === "quit") break;
          if (answer === "v" || answer === "y" || answer === "yes") {
            const result = wiki.verify(root, config, file.id, { areas: [file.area] });
            print(`Verified ${result.id}: ${result.status}, ${result.tier}`);
            verifiedCount++;
          }
        }
      } finally {
        rl.close();
      }
      print(`Done: verified ${verifiedCount} of ${drafts.length} drafts.`);
      return 0;
    }
    case "index": {
      const written = wiki.reindex(root, config, { areas });
      print(written.length > 0 ? written.join("\n") : "Every index.md is current.");
      return 0;
    }
    case "lint": {
      const findings = wiki.lint(root, config, { areas, now });
      const total = (level) => findings.filter((finding) => finding.level === level).length;
      if (flags.json) json(findings);
      else {
        for (const { level, rel, message } of findings)
          print(`${level.padEnd(7)} ${rel}: ${message}`);
        print(`${total("error")} errors, ${total("warning")} warnings, ${total("info")} notes`);
      }
      return total("error") > 0 || (flags.strict && total("warning") > 0) ? 1 : 0;
    }
    case "dream": {
      if (areas.some((area) => area !== "default")) {
        throw new UsageError("dream operates on the default space only (never private areas)");
      }
      const [sub = "run", subArg] = rest;
      if (sub === "status") {
        const info = wiki.dreamStatus(root, config);
        if (flags.json) json(info);
        else {
          const sched = info.schedule.loaded ? "loaded" : info.schedule.installed ? "installed (not loaded)" : "not installed";
          const lines = [
            `Schedule: ${sched} (${info.config.window}, slots ${info.config.slots.join(", ")})`,
            `Model:    ${info.config.model}${info.config.thinking ? ` (thinking: ${info.config.thinking})` : ""}`,
            `Last run: ${info.lastRunAt ?? "never"}`,
          ];
          if (info.lastDigest) lines.push("", info.lastDigest.trimEnd());
          print(lines.join("\n"));
        }
        return 0;
      }
      if (sub === "undo") {
        const undone = wiki.undoDream(root, config);
        if (flags.json) json(undone);
        else print(`Reverted ${undone.runId} (${undone.reverted.length} commits): ${undone.reverted.map((s) => s.slice(0, 8)).join(", ")}`);
        return 0;
      }
      if (sub === "schedule") {
        const action = subArg ?? "status";
        if (action === "status") {
          const st = wiki.launchAgentStatus();
          if (flags.json) json({ ...st, window: config.dream.window, slots: config.dream.slots });
          else print(`${st.label}: ${st.loaded ? "loaded" : st.installed ? "installed" : "not installed"} (${config.dream.window}, slots ${config.dream.slots.join(", ")})`);
          return 0;
        }
        if (action === "install") {
          const res = wiki.installLaunchAgent({
            wikiBin: fileURLToPath(import.meta.url),
            slots: config.dream.slots,
            cacheDir: wiki.dreamCacheDir(),
            wikiHome: process.env.MEMORY_WIKI_HOME?.trim() || null,
          });
          if (flags.json) json(res);
          else print(`Installed ${res.label} at ${res.plistPath} (slots: ${res.slots.join(", ")}, log: ${res.logPath})`);
          return 0;
        }
        if (action === "uninstall") {
          const res = wiki.uninstallLaunchAgent();
          if (flags.json) json(res);
          else print(res.removed ? `Uninstalled ${res.label}` : `${res.label} was not installed`);
          return 0;
        }
        throw new UsageError(`unknown dream schedule action "${action}"`);
      }
      if (sub !== "run") throw new UsageError(`unknown dream action "${sub}"`);
      let days = null;
      if (flags.days !== undefined) {
        days = Number(flags.days);
        if (!Number.isFinite(days) || days <= 0) throw new UsageError("--days takes a positive number");
      }
      const result = await wiki.runDream(root, config, {
        bypassGuards: Boolean(flags.now),
        force: Boolean(flags.force),
        dryRun: Boolean(flags["dry-run"]),
        days,
        model: flags.model ?? null,
        thinking: flags.thinking === undefined ? undefined : flags.thinking === "none" ? null : flags.thinking,
      });
      if (flags.json) json(result);
      else if (result.status === "skipped" || result.status === "noop") print(`dream ${result.status}: ${result.reason}`);
      else {
        print(result.digest.trimEnd());
        if (flags["dry-run"] && result.applied.length > 0) {
          for (const a of result.applied) print(`\n=== ${a.kind.toUpperCase()} ${a.id} ===\n${a.preview?.after?.trimEnd() ?? ""}`);
        }
      }
      return 0;
    }
    case "area": {
      const [action = "status", name] = rest;
      if (action === "status") {
        const status = wiki.areaStatus(root, config);
        if (flags.json) json(status);
        else {
          for (const area of status) {
            const state = !area.exists ? "not created" : area.mounted ? "unlocked" : "locked";
            print(`${area.name}: ${state}`);
          }
        }
        return 0;
      }
      if (action === "lock" && flags.all) {
        for (const area of wiki.areaStatus(root, config)) {
          if (flags.sync && area.mounted) {
            const synced = await wiki.syncArea(root, config, area.name, {
              remote: flags.remote,
              host: flags.host,
              forcePush: Boolean(flags.force),
              allowDirty: Boolean(flags["allow-dirty"]),
            });
            print(
              `Synced ${area.name}: ${synced.merged.length} merged, ${synced.pushed ? `uploaded ${synced.host}.bundle.age` : "up to date"}`,
            );
          }
          if (wiki.lockArea(root, config, area.name, { force: flags.force }))
            print(`Locked ${area.name}`);
        }
        return 0;
      }
      if (!name) throw new UsageError(`area ${action} needs an area name`);
      if (action === "create") {
        await wiki.createArea(root, config, name);
        print(`Created ${name}; it is locked. Unlock it with: wiki area unlock ${name}`);
      } else if (action === "unlock") {
        const unlocked = await wiki.unlockArea(root, config, name);
        print(unlocked ? `Unlocked ${name}` : `${name} is already unlocked`);
      } else if (action === "lock") {
        if (flags.sync && wiki.isMounted(root, name)) {
          const synced = await wiki.syncArea(root, config, name, {
            remote: flags.remote,
            host: flags.host,
            forcePush: Boolean(flags.force),
            allowDirty: Boolean(flags["allow-dirty"]),
          });
          print(
            `Synced ${name}: ${synced.merged.length} merged, ${synced.pushed ? `uploaded ${synced.host}.bundle.age` : "up to date"}`,
          );
        }
        const locked = wiki.lockArea(root, config, name, { force: flags.force });
        print(locked ? `Locked ${name}` : `${name} is already locked`);
      } else if (action === "sync") {
        if (flags.pull && flags.push) {
          throw new UsageError("area sync takes at most one of --pull or --push");
        }
        const result = await wiki.syncArea(root, config, name, {
          remote: flags.remote,
          host: flags.host,
          forcePush: Boolean(flags.force),
          pullOnly: Boolean(flags.pull),
          pushOnly: Boolean(flags.push),
          allowDirty: Boolean(flags["allow-dirty"]),
        });
        if (flags.json) json(result);
        else {
          const parts = [];
          if (result.merged.length > 0) {
            parts.push(
              `merged ${result.merged.map((m) => `${m.peer} (${m.mode})`).join(", ")}`,
            );
          } else if (result.pulled.length > 0) {
            parts.push(`checked ${result.pulled.join(", ")} (already up to date)`);
          }
          if (result.pushed) {
            parts.push(`uploaded ${result.host}.bundle.age (${result.bytes} bytes)`);
          }
          print(`Synced ${name}${parts.length > 0 ? `: ${parts.join("; ")}` : ": already up to date"}`);
        }
      } else if (action === "restore") {
        const result = await wiki.restoreArea(root, config, name, {
          from: flags.from,
          remote: flags.remote,
          host: flags.host,
        });
        if (flags.json) json(result);
        else {
          print(
            `Restored ${name} from ${result.source} (${result.branch}@${result.head.slice(0, 8)}); it is locked.`,
          );
        }
      } else {
        throw new UsageError(`unknown area command "${action}"`);
      }
      return 0;
    }
    default:
      throw new UsageError(`unknown command "${command}"`);
  }
}

try {
  process.exitCode = await main();
} catch (error) {
  process.stderr.write(`wiki: ${error.message}\n`);
  if (error instanceof UsageError) process.stderr.write("Run wiki --help for usage.\n");
  process.exitCode = error instanceof UsageError ? 2 : 1;
}
