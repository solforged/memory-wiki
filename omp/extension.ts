// omp side of llm-wiki: the `wiki` and `wiki_capture` tools, the /wiki
// command, recall of wiki entries a prompt names, and the rule that private
// areas are read only by a model running on this machine.
//
// That rule is enforced where omp lets an extension look: tool calls, prompts,
// the context of each request, and subagent spawns. Requests omp makes on its
// own with other model roles (compaction, titles, judgments) are outside that,
// so /wiki unlock also refuses unless every configured role is local.
import fs from "node:fs";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { WikiFile } from "../src/bundle.js";
import * as wiki from "../src/index.js";
import { inputText, isLocalModel, privateReference, runsVerify } from "../src/policy.js";

type Model = ExtensionContext["model"];

const RECALL = "llm-wiki.recall";
const NOTE = "llm-wiki.note";
const TAINT = "llm-wiki.private";
const ROLES = ["default", "smol", "slow", "task", "plan"];
const IDLE_LOCK_MS = 30 * 60_000;
const TYPES = Object.keys(wiki.TYPE_DIRS).join(", ");
// What a session that holds private content may still submit on a hosted model.
const ALLOWED_WHEN_WITHHELD = /^\/(model|models|wiki|new|clear|resume|exit|quit|settings)\b/;
const WITHHELD =
  "[llm-wiki] This conversation holds private wiki content and has been withheld, because the current model does not run on the user's machine. Reply only that the user must switch back to a local model or start a new session.";

export default function llmWiki(pi: ExtensionAPI) {
  const z = pi.zod;
  const root = wiki.wikiRoot();

  /** The session's context holds text from a private area. */
  let tainted = false;
  /** Areas this session unlocked, which it locks again when it ends or idles. */
  const unlockedHere = new Set<string>();
  let lastPrivateUse = 0;
  let idleWatch = false;
  /** Concepts already recalled into the current context, by path and change time. */
  const recalled = new Set<string>();

  const exists = () => fs.existsSync(root);
  const config = () => wiki.loadConfig(root);
  const local = (model: Model) => isLocalModel(model, config().localProviders);
  const name = (model: Model) => (model ? `${model.provider}/${model.id}` : "no model");
  const refusal = (model: Model) =>
    `Private wiki areas are read only by a model that runs on this machine, and this session uses ${name(model)}.`;

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
      ? wiki.resolveAreas(root, config(), ["all"]).filter((area) => area !== "default")
      : [];
    ctx.ui.setStatus("llm-wiki", open.length > 0 ? `wiki: ${open.join(", ")} unlocked` : undefined);
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
      if (entry.type === "custom" && entry.customType === TAINT) {
        tainted = (entry.data as { tainted?: boolean } | undefined)?.tainted === true;
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
        pi.logger.warn(`llm-wiki: ${(error as Error).message}`);
      }
    }
  });

  // --- Recall -------------------------------------------------------------

  pi.on("before_agent_start", (event, ctx) => {
    if (event.reason !== "prompt" || ctx.agent.kind === "sub" || !exists()) return;
    const areas = local(ctx.model) ? ["all"] : ["default"];
    const key = (file: WikiFile) => `${file.rel}\n${file.fm.generated?.at ?? ""}`;
    const fresh = wiki
      .recall(wiki.concepts(root, config(), { areas }), event.prompt)
      .filter((file) => !recalled.has(key(file)));
    if (fresh.length === 0) return;
    for (const file of fresh) recalled.add(key(file));
    const isPrivate = fresh.some((file) => file.area !== "default");
    if (isPrivate) touchPrivate();
    return {
      message: {
        customType: RECALL,
        content: wiki.formatRecall(fresh.map((file) => wiki.describe(file))),
        display: true,
        attribution: "agent",
        details: { ids: fresh.map((file) => file.id), private: isPrivate },
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
      const reference = privateReference(text, {
        root,
        areas: Object.keys(settings.areas),
        mounted: wiki.resolveAreas(root, settings, ["all"]).filter((area) => area !== "default"),
        cwd: ctx.cwd,
      });
      if (!reference) return;
      if (isLocalModel(ctx.model, settings.localProviders)) {
        touchPrivate();
        return;
      }
      return { block: true, reason: `${refusal(ctx.model)} This call asks for ${reference}.` };
    } catch (error) {
      // A check that cannot run blocks only calls that mention the wiki.
      if (!/\bwiki\b/.test(text)) return;
      return {
        block: true,
        reason: `llm-wiki could not check this call: ${(error as Error).message}`,
      };
    }
  });

  pi.on("input", (event, ctx) => {
    if (!tainted || local(ctx.model) || ALLOWED_WHEN_WITHHELD.test(event.text)) return;
    ctx.ui.notify(
      `This session holds private wiki content, and ${name(ctx.model)} does not run on this machine. Switch back to a local model, or start a new session.`,
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
      "Read the user's wiki, their long-term memory of names, terms, projects and investigations. Look a name up here before guessing what it means. Results carry a status and a trust tier.",
    parameters: z.object({
      op: z
        .enum(["lookup", "search", "show", "list"])
        .describe(
          "lookup: entries with this title or alias, else near names. search: full text. show: one entry with its text. list: every entry.",
        ),
      query: z
        .string()
        .optional()
        .describe("lookup: a name. search: words. show: an id such as concepts/sparse-bundle."),
      type: z.string().optional().describe(`search, list: only this type (${TYPES})`),
      area: z
        .string()
        .optional()
        .describe(
          '"default" when omitted. A private area\'s name or "all" needs a model that runs on this machine.',
        ),
    }),
    approval: "read",
    async execute(_id, params, _signal, _onUpdate, ctx) {
      requireWiki();
      const wantsPrivate = params.area !== undefined && params.area !== "default";
      if (wantsPrivate && !local(ctx.model)) throw new Error(refusal(ctx.model));
      const files = wiki.concepts(root, config(), { areas: [params.area ?? "default"] });
      const summary = (file: WikiFile) => wiki.formatSummary(wiki.describe(file));
      const query = (params.query ?? "").trim();
      if (params.op !== "list" && !query) throw new Error(`${params.op} needs a query`);
      let shown: WikiFile[];
      let text: string;
      if (params.op === "lookup") {
        const found = wiki.lookup(files, query);
        shown = found.exact.length > 0 ? found.exact : found.candidates;
        const summaries = shown.map(summary).join("\n");
        if (found.exact.length > 0) text = summaries;
        else text = `Nothing is named "${query}".${summaries ? `\nNear names:\n${summaries}` : ""}`;
      } else if (params.op === "search") {
        const results = wiki.search(files, query, { type: params.type });
        shown = results.map((result) => result.file);
        text =
          results.map((result) => `${summary(result.file)}\n  > ${result.snippet}`).join("\n") ||
          "No entry matches.";
      } else if (params.op === "show") {
        const file = wiki.findConcept(files, root, query);
        if (!file) throw new Error(`No entry "${query}". Find its id with op=lookup or op=search.`);
        shown = [file];
        text = `${summary(file)}\n\n${fs.readFileSync(file.abs, "utf8").trimEnd()}`;
      } else {
        const type = params.type?.toLowerCase();
        shown = files.filter((file) => !type || file.type.toLowerCase() === type);
        text = shown.map(summary).join("\n") || "The wiki has no entries yet.";
      }
      const isPrivate = shown.some((file) => file.area !== "default");
      if (isPrivate) touchPrivate();
      return {
        content: [{ type: "text", text }],
        details: { ids: shown.map((file) => file.id), private: isPrivate },
      };
    },
  });

  pi.registerTool({
    name: "wiki_capture",
    label: "Wiki Capture",
    description:
      "Record an entry in the user's wiki, or update one by id, when asked to remember something or after working out what a name means. Look the name up first. The entry is a draft until the user verifies it.",
    parameters: z.object({
      id: z.string().optional().describe("Update this entry. Omit to create one."),
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
          "Markdown detail. Link related entries with relative links. Replaces the body on update.",
        ),
      aliases: z.array(z.string()).optional().describe("Other names it goes by."),
      tags: z.array(z.string()).optional(),
      sources: z.array(z.string()).optional().describe("URLs or paths this derives from."),
      area: z
        .string()
        .optional()
        .describe(
          '"default" when omitted. A private area needs a model that runs on this machine.',
        ),
    }),
    approval: "write",
    async execute(_id, params, _signal, _onUpdate, ctx) {
      requireWiki();
      const settings = config();
      const isLocal = isLocalModel(ctx.model, settings.localProviders);
      if (params.area !== undefined && params.area !== "default" && !isLocal)
        throw new Error(refusal(ctx.model));
      const by = `omp/${(ctx.model?.id ?? "unknown").replace(/\s+/g, "-")}`;
      const result = wiki.capture(
        root,
        settings,
        { ...params, by },
        { areas: isLocal ? ["all"] : ["default"] },
      );
      if (result.area !== "default") touchPrivate();
      const text = `${result.created ? "Created" : "Updated"} ${result.id} as a draft. It is not recalled until the user runs /wiki verify ${result.id}.`;
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
        const areas = local(ctx.model) ? ["all"] : ["default"];
        const files = () => wiki.concepts(root, settings, { areas });
        if (action === "status") {
          const all = files();
          const drafts = all.filter((file) => file.status.status === "draft").length;
          const lines = [`${root}: ${all.length} entries, ${drafts} drafts awaiting /wiki verify`];
          for (const area of wiki.areaStatus(root, settings)) {
            lines.push(
              `${area.name}: ${!area.exists ? "not created" : area.mounted ? "unlocked" : "locked"}`,
            );
          }
          const where = local(ctx.model)
            ? "runs on this machine"
            : "is hosted: private areas are closed to it";
          say([...lines, `${name(ctx.model)} ${where}`].join("\n"));
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
          // Every role omp may send context with must be local before an area opens.
          const hosted = local(ctx.model) ? [] : [`this session: ${name(ctx.model)}`];
          for (const role of ROLES) {
            const model = ctx.models.resolve(`@${role}`);
            if (model && !local(model)) hosted.push(`${role}: ${name(model)}`);
          }
          if (hosted.length > 0) {
            throw new Error(
              `private areas open only when every model role runs on this machine. Hosted now: ${hosted.join("; ")}`,
            );
          }
          const unlocked = await wiki.unlockArea(root, settings, argument);
          unlockedHere.add(argument);
          lastPrivateUse = Date.now();
          if (!idleWatch) {
            idleWatch = true;
            // Stops with the session; an interval with nothing unlocked does nothing.
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
