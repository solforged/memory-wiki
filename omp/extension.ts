// omp side of memory-wiki: the `wiki` and `wiki_capture` tools, the /wiki
// command, recall of wiki entries a prompt names, and the rule that private
// areas are read only when the session model AND every configured omp role run
// on this machine.
import fs from "node:fs";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { WikiFile } from "../src/bundle.js";
import * as wiki from "../src/index.js";
import { inputText, isLocalModel, privateReference, runsVerify } from "../src/policy.js";

type Model = ExtensionContext["model"];

const RECALL = "memory-wiki.recall";
const NOTE = "memory-wiki.note";
const TAINT = "memory-wiki.private";
const ROLES = [
  "default",
  "smol",
  "slow",
  "plan",
  "task",
  "tiny",
  "memory",
  "commit",
  "vision",
  "advisor",
  "widget",
  "image",
];
const IDLE_LOCK_MS = 30 * 60_000;
const OUTPUT_MAX = 30_000;
const LIST_LIMIT = 80;
const TYPES = Object.keys(wiki.TYPE_DIRS).join(", ");
// What a session that holds private content may still submit on a hosted model.
const ALLOWED_WHEN_WITHHELD =
  /^\/(model|models|modelpreset|wiki|new|clear|resume|exit|quit|settings)\b/;
const WITHHELD =
  "[memory-wiki] This conversation holds private wiki content and has been withheld, because the current model does not run on the user's machine. Reply only that the user must switch back to a local model or start a new session.";

function clip(text: string): string {
  return text.length > OUTPUT_MAX
    ? `${text.slice(0, OUTPUT_MAX)}\n… output cut at ${OUTPUT_MAX} characters; narrow the query.`
    : text;
}

export default function memoryWiki(pi: ExtensionAPI) {
  const z = pi.zod;
  const root = wiki.wikiRoot();

  /** The session's context holds text from a private area. */
  let tainted = false;
  /** Areas this session unlocked, which it locks again when it ends or idles. */
  const unlockedHere = new Set<string>();
  let lastPrivateUse = 0;
  let idleWatch = false;
  /** Concepts already recalled in the visible context: id -> recall revision. */
  const recalled = new Map<string, string>();

  const exists = () => fs.existsSync(root);
  const config = () => wiki.loadConfig(root);
  const local = (model: Model) => isLocalModel(model, config().localProviders);
  const name = (model: Model) => (model ? `${model.provider}/${model.id}` : "no model");
  const hostedRoles = (ctx: ExtensionContext) => {
    const hosted = local(ctx.model) ? [] : [`this session: ${name(ctx.model)}`];
    for (const role of ROLES) {
      const model = ctx.models.resolve(`@${role}`);
      if (model && !local(model)) hosted.push(`${role}: ${name(model)}`);
    }
    return hosted;
  };
  const allLocal = (ctx: ExtensionContext) => hostedRoles(ctx).length === 0;
  const refusal = (ctx: ExtensionContext) => {
    const hosted = hostedRoles(ctx);
    return `Private wiki areas require the session and every configured model role to run on this machine. Hosted now: ${hosted.join("; ") || name(ctx.model)}.`;
  };

  function requireWiki() {
    if (!exists())
      throw new Error(`There is no wiki at ${root}. The user creates one with: wiki init`);
  }

  function touchPrivate() {
    lastPrivateUse = Date.now();
    if (tainted) return;
    tainted = true;
    pi.appendEntry(TAINT, { tainted: true });
  }

  function showStatus(ctx: ExtensionContext) {
    const open = exists()
      ? wiki
          .areaStatus(root, config())
          .filter((area) => area.mounted)
          .map((area) => area.name)
      : [];
    ctx.ui.setStatus("memory-wiki", open.length > 0 ? `wiki: ${open.join(", ")} unlocked` : undefined);
  }

  function say(text: string) {
    // Drawn for the user and saved, never sent to the model.
    pi.sendMessage(
      {
        customType: NOTE,
        content: text,
        display: true,
        excludeFromContext: true,
        attribution: "agent",
      },
      { triggerTurn: false },
    );
  }

  function restore(_event: unknown, ctx: ExtensionContext) {
    tainted = false;
    recalled.clear();
    for (const entry of ctx.sessionManager.getBranch()) {
      if (entry.type === "custom" && (entry.customType === TAINT || entry.customType === "llm-wiki.private")) {
        tainted = (entry.data as { tainted?: boolean } | undefined)?.tainted === true;
      }
      if (
        entry.type === "custom_message" &&
        (entry.customType === RECALL || entry.customType === "llm-wiki.recall")
      ) {
        const details = entry.details as
          | { ids?: string[]; revisions?: string[] }
          | undefined;
        details?.ids?.forEach((id, i) => recalled.set(id, details.revisions?.[i] ?? ""));
      }
    }
    showStatus(ctx);
  }
  pi.on("session_start", restore);
  pi.on("session_switch", restore);
  pi.on("session_branch", restore);
  pi.on("session_tree", restore);
  pi.on("session_compact", () => recalled.clear());
  pi.on("session_clear", () => {
    recalled.clear();
    if (tainted) pi.appendEntry(TAINT, { tainted: false });
    tainted = false;
  });
  pi.on("turn_end", (_event, ctx) => showStatus(ctx));
  pi.on("session_shutdown", () => {
    for (const area of unlockedHere) {
      try {
        wiki.lockArea(root, config(), area);
      } catch (error) {
        pi.logger.warn(`memory-wiki: ${(error as Error).message}`);
      }
    }
  });

  // --- Recall -------------------------------------------------------------

  pi.on("before_agent_start", (event, ctx) => {
    if (event.reason !== "prompt" || ctx.agent.kind === "sub" || !exists()) return;
    const settings = config();
    // Only auto-recall private areas when every role is local AND this session unlocked one.
    const areas =
      allLocal(ctx) && unlockedHere.size > 0 ? ["default", ...unlockedHere] : ["default"];
    const nowText = wiki
      .readNow(root, settings, { areas })
      .map((page) => page.text)
      .join("\n");
    const fresh = wiki
      .recall(wiki.concepts(root, settings, { areas }), event.prompt, { nowText })
      .map((file) => wiki.describe(file))
      .filter((info) => recalled.get(info.id) !== info.revision);
    if (fresh.length === 0) return;
    for (const info of fresh) recalled.set(info.id, info.revision);
    const isPrivate = fresh.some((info) => info.area !== "default");
    if (isPrivate) touchPrivate();
    return {
      message: {
        customType: RECALL,
        content: wiki.formatRecall(fresh),
        display: true,
        attribution: "agent",
        details: {
          ids: fresh.map((info) => info.id),
          revisions: fresh.map((info) => info.revision),
          private: isPrivate,
        },
      },
    };
  });

  // --- Private areas stay with local models --------------------------------

  pi.on("tool_call", (event, ctx) => {
    if (event.toolName === "wiki" || event.toolName === "wiki_capture") return; // they apply the rule themselves
    const text = inputText(event.input);
    try {
      if (event.toolName === "bash" && runsVerify(text)) {
        return {
          block: true,
          reason: "Only the user verifies wiki entries. Give them the id to run /wiki verify <id>.",
        };
      }
      if (!exists()) return;
      const settings = config();
      const mounted = wiki
        .areaStatus(root, settings)
        .filter((area) => area.mounted)
        .map((area) => area.name);
      const reference = privateReference(text, {
        root,
        areas: Object.keys(settings.areas),
        mounted,
        cwd: ctx.cwd,
      });
      if (!reference) return;
      if (allLocal(ctx)) {
        touchPrivate();
        return;
      }
      return { block: true, reason: `${refusal(ctx)} This call asks for ${reference}.` };
    } catch (error) {
      // A check that cannot run blocks only calls that mention the wiki.
      if (!/\bwiki\b/.test(text)) return;
      return {
        block: true,
        reason: `memory-wiki could not check this call: ${(error as Error).message}`,
      };
    }
  });

  pi.on("input", (event, ctx) => {
    if (!tainted || allLocal(ctx) || ALLOWED_WHEN_WITHHELD.test(event.text)) return;
    ctx.ui.notify(
      `This session holds private wiki content, and a hosted model role is active. Switch back to an all-local profile, or start a new session.`,
      "error",
    );
    return { handled: true };
  });

  // The last line of defence, for any path that still builds a request for a
  // hosted model from a session holding private text: send none of it.
  pi.on("context", (_event, ctx) => {
    if (!tainted || local(ctx.model)) return;
    return {
      messages: [
        {
          role: "user",
          content: [{ type: "text", text: WITHHELD }],
          attribution: "agent",
          timestamp: Date.now(),
        },
      ],
    };
  });

  pi.on("before_subagent_spawn", (event, ctx) => {
    if (!tainted) return;
    const hosted = event.patterns.filter((pattern) => !local(ctx.models.resolve(pattern)));
    if (event.patterns.length > 0 && hosted.length === 0) return;
    return {
      block: true,
      reason: `This session holds private wiki content, and the subagent would run on ${hosted.join(", ") || "an unknown model"}.`,
    };
  });

  // --- Tools ----------------------------------------------------------------

  pi.registerTool({
    name: "wiki",
    label: "Wiki",
    description:
      "Read the user's memory-wiki of names, terms, projects, investigations, recent log entries, and now.md. Look a name up here before guessing what it means. Results carry a status and a trust tier.",
    parameters: z.object({
      op: z
        .enum(["lookup", "search", "show", "list", "recent", "now"])
        .describe(
          "lookup: exact title/alias else near names. search: full text. show: one entry. list: entries. recent: newest log lines. now: current context.",
        ),
      query: z
        .string()
        .optional()
        .describe("lookup: a name. search: words. show: an id such as concepts/sparse-bundle."),
      type: z.string().optional().describe(`search, list: only this type (${TYPES})`),
      status: z.string().optional().describe("search, list: draft, stable, or deprecated"),
      area: z
        .string()
        .optional()
        .describe(
          '"default" when omitted. A private area\'s name or "all" requires an all-local model setup.',
        ),
    }),
    approval: "read",
    async execute(_id, params, _signal, _onUpdate, ctx) {
      requireWiki();
      const wantsPrivate = params.area !== undefined && params.area !== "default";
      if (wantsPrivate && !allLocal(ctx)) throw new Error(refusal(ctx));
      const settings = config();
      const areas = [params.area ?? "default"];
      if (params.op === "now") {
        const pages = wiki.readNow(root, settings, { areas });
        const isPrivate = pages.some((page) => page.area !== "default");
        if (isPrivate) touchPrivate();
        const text =
          pages.map((page) => `${page.ref}:\n\n${page.text}`).join("\n\n---\n\n") ||
          "No now.md in selected areas.";
        return { content: [{ type: "text", text: clip(text) }], details: { private: isPrivate } };
      }
      if (params.op === "recent") {
        const items = wiki.readRecent(root, settings, { areas, limit: 20 });
        const isPrivate = items.some((item) => item.area !== "default");
        if (isPrivate) touchPrivate();
        const text =
          items
            .map((item) =>
              item.area === "default"
                ? `${item.date}  ${item.entry}`
                : `${item.date}  [${item.area}] ${item.entry}`,
            )
            .join("\n") || "No log entries yet.";
        return { content: [{ type: "text", text: clip(text) }], details: { private: isPrivate } };
      }
      const files = wiki.concepts(root, settings, { areas });
      const summary = (file: WikiFile) => wiki.formatSummary(wiki.describe(file));
      const query = (params.query ?? "").trim();
      if ((params.op === "lookup" || params.op === "show") && !query)
        throw new Error(`${params.op} needs a query`);
      let shown: WikiFile[];
      let text: string;
      if (params.op === "lookup") {
        const found = wiki.lookup(files, query);
        shown = found.exact.length > 0 ? found.exact : found.candidates;
        const summaries = shown.map(summary).join("\n");
        if (found.exact.length > 0) text = summaries;
        else text = `Nothing is named "${query}".${summaries ? `\nNear names:\n${summaries}` : ""}`;
      } else if (params.op === "search") {
        if (!query && !params.type && !params.status)
          throw new Error("search needs a query, type, or status");
        const results = wiki.search(files, query, { type: params.type, status: params.status });
        shown = results.map((result) => result.file);
        text =
          results
            .map((result) =>
              result.snippet
                ? `${summary(result.file)}\n  > ${result.snippet}`
                : summary(result.file),
            )
            .join("\n") || "No entry matches.";
      } else if (params.op === "show") {
        const file = wiki.findConcept(files, root, query);
        if (!file) throw new Error(`No entry "${query}". Find its id with op=lookup or op=search.`);
        shown = [file];
        text = `${summary(file)}\n\n${file.body.trim() || "(empty body)"}`;
      } else {
        const type = params.type?.toLowerCase();
        const status = params.status?.toLowerCase();
        const matching = files.filter(
          (file) =>
            (!type || file.type.toLowerCase() === type) &&
            (!status || file.status.status.toLowerCase() === status),
        );
        shown = matching.slice(0, LIST_LIMIT);
        const tail =
          matching.length > LIST_LIMIT
            ? `\n… ${matching.length - LIST_LIMIT} more entries omitted; filter with type or search.`
            : "";
        text = (shown.map(summary).join("\n") || "The wiki has no matching entries.") + tail;
      }
      const isPrivate = shown.some((file) => file.area !== "default");
      if (isPrivate) touchPrivate();
      return {
        content: [{ type: "text", text: clip(text) }],
        details: { ids: shown.map((file) => file.id), private: isPrivate },
      };
    },
  });

  pi.registerTool({
    name: "wiki_capture",
    label: "Wiki Capture",
    description:
      "Record or update an entry in the user's memory-wiki (as a draft), deprecate one, or update now.md. Look a name up first. On update, aliases/tags/sources are merged unless replace_lists is set.",
    parameters: z.object({
      op: z.enum(["capture", "deprecate", "now"]).optional().describe("Default: capture."),
      id: z.string().optional().describe("Update or deprecate this entry. Omit to create one."),
      type: z.string().optional().describe(`For a new entry: ${TYPES}.`),
      title: z.string().optional().describe("The name being defined. For a new entry."),
      description: z
        .string()
        .optional()
        .describe("One sentence: all that a lookup returns. For a new entry."),
      body: z
        .string()
        .optional()
        .describe(
          "Markdown detail (or complete now.md text for op=now). Replaces the body on update.",
        ),
      aliases: z.array(z.string()).optional().describe("Other names it goes by."),
      tags: z.array(z.string()).optional(),
      sources: z.array(z.string()).optional().describe("URLs or paths this derives from."),
      replace_lists: z
        .boolean()
        .optional()
        .describe("Replace aliases/tags/sources on update instead of merging."),
      superseded_by: z.string().optional().describe("Replacement path when op=deprecate."),
      area: z
        .string()
        .optional()
        .describe('"default" when omitted. A private area requires an all-local model setup.'),
    }),
    approval: "write",
    async execute(_id, params, _signal, _onUpdate, ctx) {
      requireWiki();
      const settings = config();
      const localOk = allLocal(ctx);
      const targetArea = params.area ?? "default";
      if (targetArea !== "default" && !localOk) throw new Error(refusal(ctx));
      if (tainted && targetArea === "default") {
        const ok = await ctx.ui.confirm(
          "Write to the default (unencrypted) wiki from a private session?",
          "This session has read private wiki content. Confirm that this write contains nothing private.",
        );
        if (!ok) return { content: [{ type: "text", text: "Cancelled; nothing was written." }] };
      }
      const by = `omp/${(ctx.model?.id ?? "unknown").replace(/\s+/g, "-")}`;
      const areas = localOk ? ["all"] : ["default"];
      const op = params.op ?? "capture";
      if (op === "now") {
        const result = wiki.writeNow(root, settings, {
          text: params.body ?? "",
          area: targetArea,
          areas,
        });
        if (result.area !== "default") touchPrivate();
        return {
          content: [
            {
              type: "text",
              text: `Updated ${result.ref} (${result.bytes}/${result.limit} bytes, refresh ${result.staleAfter}).`,
            },
          ],
          details: { ref: result.ref, private: result.area !== "default" },
        };
      }
      if (op === "deprecate") {
        if (!params.id) throw new Error("deprecate needs an id");
        const result = wiki.deprecate(root, settings, params.id, {
          by,
          supersededBy: params.superseded_by,
          areas,
        });
        if (result.area !== "default") touchPrivate();
        return {
          content: [
            {
              type: "text",
              text: `Deprecated ${result.id}${result.superseded_by ? ` (superseded by ${result.superseded_by})` : ""}.`,
            },
          ],
          details: { id: result.id, private: result.area !== "default" },
        };
      }
      const preview = wiki.capture(
        root,
        settings,
        {
          id: params.id,
          type: params.type,
          title: params.title,
          description: params.description,
          body: params.body,
          aliases: params.aliases,
          tags: params.tags,
          sources: params.sources,
          replaceLists: params.replace_lists,
          area: params.area,
          by,
        },
        { areas, preview: true },
      );
      const result = wiki.capture(
        root,
        settings,
        {
          id: params.id,
          type: params.type,
          title: params.title,
          description: params.description,
          body: params.body,
          aliases: params.aliases,
          tags: params.tags,
          sources: params.sources,
          replaceLists: params.replace_lists,
          area: params.area,
          by,
        },
        { areas, ifMatch: preview.revision },
      );
      if (result.area !== "default") touchPrivate();
      const text = `${result.created ? "Created" : "Updated"} ${result.id} as a draft.`;
      return {
        content: [{ type: "text", text }],
        details: { id: result.id, private: result.area !== "default" },
      };
    },
  });

  // --- /wiki ------------------------------------------------------------------

  function lock(ctx: ExtensionContext, areas: string[]) {
    for (const area of areas) {
      if (wiki.lockArea(root, config(), area)) ctx.ui.notify(`Locked ${area}`, "info");
      unlockedHere.delete(area);
    }
    showStatus(ctx);
  }

  pi.registerCommand("wiki", {
    description: "Wiki: status | lookup <name> | verify <id> | lint | unlock <area> | lock [area]",
    handler: async (args, ctx) => {
      const [action = "status", ...rest] = args.trim().split(/\s+/).filter(Boolean);
      const argument = rest.join(" ");
      try {
        requireWiki();
        const settings = config();
        const areas = allLocal(ctx) ? ["all"] : ["default"];
        const files = () => wiki.concepts(root, settings, { areas });
        if (action === "status") {
          const all = files();
          const drafts = all.filter((file) => file.status.status === "draft").length;
          const lines = [`${root}: ${all.length} entries, ${drafts} drafts awaiting /wiki verify`];
          for (const page of wiki.readNow(root, settings, { areas })) {
            lines.push(`${page.ref}: ${page.bytes}/${page.limit} bytes, refresh ${page.staleAfter ?? "never"}`);
          }
          for (const area of wiki.areaStatus(root, settings)) {
            lines.push(
              `${area.name}: ${!area.exists ? "not created" : area.mounted ? "unlocked" : "locked"}`,
            );
          }
          const hosted = hostedRoles(ctx);
          const where =
            hosted.length === 0
              ? "all roles run on this machine"
              : `hosted roles (${hosted.join("; ")}): private areas are closed`;
          say([...lines, where].join("\n"));
        } else if (action === "lookup") {
          const found = wiki.lookup(files(), argument);
          const shown = found.exact.length > 0 ? found.exact : found.candidates;
          const summaries = shown.map((file) => wiki.formatSummary(wiki.describe(file))).join("\n");
          if (found.exact.length > 0) say(summaries);
          else
            say(`Nothing is named "${argument}".${summaries ? `\nNear names:\n${summaries}` : ""}`);
        } else if (action === "verify") {
          const file = wiki.findConcept(files(), root, argument);
          if (!file) throw new Error(`no entry "${argument}"`);
          const summary = `${wiki.formatSummary(wiki.describe(file))}\n  written by ${file.fm.generated?.by ?? "unknown"}`;
          if (
            !(await ctx.ui.confirm(`Record ${settings.human} as having confirmed this?`, summary))
          )
            return;
          const result = wiki.verify(root, settings, file.id, { areas });
          ctx.ui.notify(`Verified ${result.id}: ${result.status}, ${result.tier}`, "info");
        } else if (action === "lint") {
          const findings = wiki.lint(root, settings, { areas });
          const lines = findings.map(
            (finding) => `${finding.level.padEnd(7)} ${finding.rel}: ${finding.message}`,
          );
          say(lines.join("\n") || "Nothing to report.");
        } else if (action === "unlock") {
          const hosted = hostedRoles(ctx);
          if (hosted.length > 0) {
            throw new Error(
              `private areas open only when every model role runs on this machine. Hosted now: ${hosted.join("; ")}`,
            );
          }
          const unlocked = await wiki.unlockArea(root, settings, argument);
          if (unlocked) unlockedHere.add(argument);
          lastPrivateUse = Date.now();
          if (!idleWatch) {
            idleWatch = true;
            ctx.setInterval(() => {
              if (unlockedHere.size > 0 && Date.now() - lastPrivateUse >= IDLE_LOCK_MS)
                lock(ctx, [...unlockedHere]);
            }, 60_000);
          }
          ctx.ui.notify(
            unlocked ? `Unlocked ${argument}` : `${argument} is already unlocked`,
            "info",
          );
          showStatus(ctx);
        } else if (action === "lock") {
          lock(ctx, argument ? [argument] : Object.keys(settings.areas));
        } else {
          throw new Error(`unknown action "${action}"`);
        }
      } catch (error) {
        ctx.ui.notify(`wiki: ${(error as Error).message}`, "error");
      }
    },
  });
}
