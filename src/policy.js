// The rule for private areas: only a model that runs on this machine reads
// them. These are the checks an agent host builds that rule from. They are a
// guardrail for cooperative agents, not a sandbox: a mounted area is readable
// by any process of the same user.
import os from "node:os";
import path from "node:path";

const LOOPBACK = /^(localhost|127(\.\d{1,3}){3}|\[::1\])$/i;

/**
 * True for a model served from this machine: its provider is one the settings
 * list as local, and it answers on a loopback address. A loopback address
 * alone is not enough, since a local gateway may forward to a hosted model.
 * @param {{ provider?: string, id?: string, baseUrl?: string } | undefined} model
 * @param {string[]} providers `localProviders` from the settings
 */
export function isLocalModel(model, providers) {
  if (!model || !providers.includes(model.provider)) return false;
  if (model.provider === "local") return true; // omp's on-device catalog has no server
  if (/(^|[-:])cloud$/.test(model.id ?? "")) return false; // Ollama serves these from its cloud
  try {
    return LOOPBACK.test(new URL(model.baseUrl).hostname);
  } catch {
    return false;
  }
}

/** Every string inside a tool call's input, one per line. */
export function inputText(input) {
  const parts = [];
  const visit = (value) => {
    if (typeof value === "string") parts.push(value);
    else if (Array.isArray(value)) value.forEach(visit);
    else if (value && typeof value === "object") Object.values(value).forEach(visit);
  };
  visit(input);
  return parts.join("\n");
}

function spellings(root, home) {
  const forms = new Set([root, "$LLM_WIKI_HOME", "${LLM_WIKI_HOME}"]);
  if (root.startsWith(`${home}/`)) {
    const rest = root.slice(home.length);
    for (const prefix of ["~", "$HOME", "${HOME}"]) forms.add(prefix + rest);
  }
  const data = path.dirname(root);
  if (path.basename(root) === "llm-wiki" && data.endsWith("/share")) {
    forms.add("$XDG_DATA_HOME/llm-wiki").add("${XDG_DATA_HOME}/llm-wiki");
  }
  return [...forms];
}

/**
 * How a tool call's input reaches private content, or null when it does not:
 * a path into a private area, the wiki root as a whole while an area is
 * unlocked (a recursive read would descend into it), the CLI asked for a
 * private area, or the CLI asked to unlock one.
 * @param {string} text from `inputText`
 * @param {{ root: string, areas: string[], mounted: string[], cwd: string, home?: string }} where
 */
export function privateReference(text, { root, areas, mounted, cwd, home = os.homedir() }) {
  const inside = cwd === root || cwd.startsWith(`${root}/`);
  if (inside) {
    const first = path.relative(root, cwd).split("/")[0];
    if (areas.includes(first)) return `the working directory is inside the ${first} area`;
  }
  for (const form of spellings(root, home)) {
    for (let at = text.indexOf(form); at !== -1; at = text.indexOf(form, at + 1)) {
      const rest = text.slice(at + form.length);
      if (/^[\w.-]/.test(rest)) continue; // a longer name, such as llm-wiki-backup
      const child = /^\/+([\w.-]*)/.exec(rest)?.[1] ?? "";
      if (areas.includes(child)) return `a path into the ${child} area`;
      if (child === "" && mounted.length > 0) {
        return `the whole wiki directory while ${mounted.join(", ")} is unlocked`;
      }
    }
  }
  if (inside) {
    for (const area of areas) {
      // Area names are [a-z0-9-], so they need no escaping in a pattern.
      const relative = new RegExp(
        `(?:^|[\\s"'=:(,\\[])(?:\\.{1,2}/)*${area}(?:/|["'\\s),\\]]|$)`,
        "m",
      );
      if (relative.test(text)) return `a path into the ${area} area`;
    }
  }
  if (/\bwiki\b/.test(text)) {
    const flag = /--area(?:=|\s+)["']?([\w-]+)/.exec(text);
    if (flag && flag[1] !== "default") return `wiki --area ${flag[1]}`;
    if (/\bwiki\s+area\s+(unlock|create)\b/.test(text)) return "unlocking a private area";
  }
  return null;
}

/** `wiki verify` records a person's review; an agent never runs it. */
export const runsVerify = (text) => /\bwiki\s+verify\b/.test(text);
