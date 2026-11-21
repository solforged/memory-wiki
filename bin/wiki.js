#!/usr/bin/env node
import fs from "node:fs";
import readline from "node:readline/promises";
import { parseArgs } from "node:util";
import * as wiki from "../src/index.js";

const USAGE = `Usage: wiki <command> [options]

Read
  lookup <name>        concepts with that title or alias; failing that, near names
  search <words>       full-text search          [--type T] [--tag TAG] [--limit N]
  show <id>            one concept: status, trust tier, staleness and its text
  list                 every concept             [--type T]
  recall [text]        stable concepts whose names a text mentions (stdin without text)

Write
  capture              create a concept, or update one with --id, as a draft
      --by ACTOR --type Concept|Entity|Project|Investigation --title TEXT --description TEXT
      [--alias NAME]... [--tag TAG]... [--source URL]... [--body TEXT | --body-file FILE|-]
      [--resource URI] [--stale-after DATE] [--slug NAME]
  verify <id>          record that you confirmed a concept; asks on the terminal
  index                regenerate index.md files after edits by hand

Maintain
  init                 create the wiki
  lint [--strict]      conformance, links, staleness; --strict also fails on warnings
  area [status]        private areas, and whether each is unlocked
  area create|unlock|lock <name>      also: lock --all, lock --force
  root                 print the wiki's directory

Options
  --area NAME   "default" (when omitted), a private area, or "all"; repeatable.
                A private area is read only when named, and only while unlocked.
  --json        machine-readable output

The wiki is $LLM_WIKI_HOME, else llm-wiki under $XDG_DATA_HOME (~/.local/share).`;

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
        limit: { type: "string" },
        by: { type: "string" },
        id: { type: "string" },
        title: { type: "string" },
        description: { type: "string" },
        alias: { type: "string", multiple: true },
        source: { type: "string", multiple: true },
        body: { type: "string" },
        "body-file": { type: "string" },
        resource: { type: "string" },
        "stale-after": { type: "string" },
        slug: { type: "string" },
        strict: { type: "boolean" },
        all: { type: "boolean" },
        force: { type: "boolean" },
      },
    });
  } catch (error) {
    throw new UsageError(error.message);
  }
}

const print = (value) => process.stdout.write(`${value}\n`);
const json = (value) => print(JSON.stringify(value, null, 2));

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
      const limit = flags.limit === undefined ? 10 : Number(flags.limit);
      if (!Number.isInteger(limit) || limit < 1)
        throw new UsageError("--limit takes a positive number");
      const results = wiki
        .search(read(), text(), { type: flags.type, tag: flags.tag?.[0], limit })
        .map(({ file, score, snippet }) => ({ ...describe(file), score, snippet }));
      if (flags.json) json(results);
      else show(results, "No concept matches.");
      return 0;
    }
    case "list": {
      const infos = read()
        .filter((file) => !flags.type || file.type.toLowerCase() === flags.type.toLowerCase())
        .map(describe);
      if (flags.json) json(infos);
      else show(infos, "The wiki has no concepts yet.");
      return 0;
    }
    case "recall": {
      const prompt = rest.length > 0 ? rest.join(" ") : fs.readFileSync(0, "utf8");
      const infos = wiki.recall(read(), prompt).map(describe);
      if (flags.json) json(infos);
      else if (infos.length > 0) print(wiki.formatRecall(infos));
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
      if (!flags.by)
        throw new UsageError("capture needs --by <producer>/<version> or <scheme>:<id>");
      let body = flags.body;
      if (flags["body-file"] !== undefined) {
        body = fs.readFileSync(flags["body-file"] === "-" ? 0 : flags["body-file"], "utf8");
      }
      const result = wiki.capture(
        root,
        config,
        {
          by: flags.by,
          id: flags.id,
          type: flags.type,
          title: flags.title,
          description: flags.description,
          body,
          aliases: flags.alias,
          tags: flags.tag,
          sources: flags.source,
          resource: flags.resource,
          staleAfter: flags["stale-after"],
          slug: flags.slug,
          area: flags.area?.[0],
        },
        { areas: flags.id ? areas : ["all"] },
      );
      if (flags.json) json(result);
      else
        print(`${result.created ? "Created" : "Updated"} ${result.id} as a draft: ${result.path}`);
      return 0;
    }
    case "verify": {
      const file = wiki.findConcept(wiki.concepts(root, config, { areas: ["all"] }), root, text());
      if (!file) throw new Error(`no concept "${text()}"`);
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
      const result = wiki.verify(root, config, file.id);
      print(`Verified ${result.id}: ${result.status}, ${result.tier}`);
      return 0;
    }
    case "index": {
      const written = wiki.reindex(root, config, { areas: flags.area ?? ["all"] });
      print(written.length > 0 ? written.join("\n") : "Every index.md is current.");
      return 0;
    }
    case "lint": {
      const findings = wiki.lint(root, config, { areas: flags.area ?? ["all"] });
      const total = (level) => findings.filter((finding) => finding.level === level).length;
      if (flags.json) json(findings);
      else {
        for (const { level, rel, message } of findings)
          print(`${level.padEnd(7)} ${rel}: ${message}`);
        print(`${total("error")} errors, ${total("warning")} warnings, ${total("info")} notes`);
      }
      return total("error") > 0 || (flags.strict && total("warning") > 0) ? 1 : 0;
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
        const locked = wiki.lockArea(root, config, name, { force: flags.force });
        print(locked ? `Locked ${name}` : `${name} is already locked`);
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
