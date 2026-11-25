// The rule for private areas: only a model that runs on this machine reads
// them. These are the checks an agent host builds that rule from. They are a
// guardrail for cooperative agents, not a sandbox: a mounted area is readable
// by any process of the same user.
import os from "node:os";
import path from "node:path";

const LOOPBACK = /^(localhost|127(\.\d{1,3}){3}|\[::1\])$/i;

/** Flags of the `wiki` CLI that take a following argument token. */
const VALUE_FLAGS = new Set([
  "--area",
  "--type",
  "--tag",
  "--limit",
  "--by",
  "--id",
  "--title",
  "--description",
  "--alias",
  "--source",
  "--body",
  "--body-file",
  "--resource",
  "--stale-after",
  "--slug",
  "--superseded-by",
  "--if-match",
  "--status",
  "--remote",
  "--from",
  "--host",
]);

const READ_ONLY_COMMANDS = new Set([
  "lookup",
  "search",
  "show",
  "list",
  "recent",
  "status",
  "root",
  "lint",
  "now",
  "recall",
]);

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

/** Splits a shell line into command segments and quoted/unquoted argv tokens. */
function shellSegments(text) {
  const segments = [];
  let tokens = [];
  let current = "";
  let quote = null;
  let hasToken = false;
  const pushToken = () => {
    if (hasToken) tokens.push(current);
    current = "";
    hasToken = false;
  };
  const pushSegment = () => {
    pushToken();
    if (tokens.length > 0) segments.push(tokens);
    tokens = [];
  };
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quote === "'") {
      if (ch === "'") quote = null;
      else current += ch;
      continue;
    }
    if (quote === '"') {
      if (ch === '"') quote = null;
      else if (ch === "\\" && i + 1 < text.length) current += text[++i];
      else current += ch;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      hasToken = true;
    } else if (ch === "\\" && i + 1 < text.length) {
      current += text[++i];
      hasToken = true;
    } else if (ch === "\n" || ch === ";") {
      pushSegment();
    } else if (ch === "&" || ch === "|") {
      pushSegment();
      if (text[i + 1] === ch) i++;
    } else if (/\s/.test(ch)) {
      pushToken();
    } else {
      current += ch;
      hasToken = true;
    }
  }
  pushSegment();
  return segments;
}

function isWikiBin(token) {
  const base = path.posix.basename(token);
  return base === "wiki" || base === "wiki.js";
}

/**
 * Parsed `wiki` CLI invocations inside shell text: subcommand, positional args,
 * `--area` values, and boolean flags.
 */
export function wikiInvocations(text) {
  const calls = [];
  for (const raw of shellSegments(text)) {
    let i = 0;
    while (i < raw.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(raw[i])) i++;
    if (i < raw.length && raw[i] === "env") {
      i++;
      while (i < raw.length && (raw[i].startsWith("-") || /^[A-Za-z_][A-Za-z0-9_]*=/.test(raw[i]))) i++;
    }
    if (i >= raw.length) continue;
    let argsStart = -1;
    if (isWikiBin(raw[i])) {
      argsStart = i + 1;
    } else if (path.posix.basename(raw[i]) === "node" && i + 1 < raw.length && isWikiBin(raw[i + 1])) {
      argsStart = i + 2;
    }
    if (argsStart === -1) continue;
    const args = raw.slice(argsStart);
    const positionals = [];
    const areas = [];
    let hasSet = false;
    let hasSync = false;
    let afterDashDash = false;
    for (let j = 0; j < args.length; j++) {
      const tok = args[j];
      if (!afterDashDash && tok === "--") {
        afterDashDash = true;
        continue;
      }
      if (!afterDashDash && tok.startsWith("-")) {
        const eq = tok.indexOf("=");
        const flag = eq === -1 ? tok : tok.slice(0, eq);
        const val = eq === -1 ? undefined : tok.slice(eq + 1);
        if (flag === "--area") {
          const areaVal = val !== undefined ? val : args[++j];
          if (areaVal !== undefined) areas.push(areaVal);
        } else if (flag === "--set") {
          hasSet = true;
        } else if (flag === "--sync") {
          hasSync = true;
        } else if (eq === -1 && VALUE_FLAGS.has(flag)) {
          j++;
        }
      } else {
        positionals.push(tok);
      }
    }
    calls.push({ subcommand: positionals[0] ?? "", positionals, areas, hasSet, hasSync, raw });
  }
  return calls;
}

function spellings(root, home, cwd) {
  const forms = new Set([root, "$MEMORY_WIKI_HOME", "${MEMORY_WIKI_HOME}"]);
  if (root.startsWith(`${home}/`)) {
    const rest = root.slice(home.length);
    for (const prefix of ["~", "$HOME", "${HOME}"]) forms.add(prefix + rest);
  }
  const data = path.dirname(root);
  const base = path.basename(root);
  if ((base === "memory-wiki" || base === "llm-wiki") && data.endsWith("/share")) {
    forms.add(`$XDG_DATA_HOME/${base}`).add(`\${XDG_DATA_HOME}/${base}`);
  }
  if (cwd && root.startsWith(`${cwd === "/" ? "" : cwd}/`)) {
    const rel = path.relative(cwd, root).split(path.sep).join("/");
    if (rel && !rel.startsWith("..")) {
      forms.add(rel).add(`./${rel}`);
    }
  }
  return [...forms];
}

const TOKEN_BOUNDARY = /(?:^|[\s"'=:(,[\]{}])/;

/**
 * How a tool call's input reaches private content, or null when it does not:
 * a path into a private area (including `..` traversals and ancestor-cwd
 * relative paths), the wiki root as a whole while an area is unlocked, a CLI
 * call asking for any non-default `--area`, or asking to unlock/create one.
 * @param {string} text from `inputText`
 * @param {{ root: string, areas: string[], mounted: string[], cwd: string, home?: string, checkCommands?: boolean }} where
 */
export function privateReference(
  text,
  { root, areas, mounted, cwd, home = os.homedir(), checkCommands = true },
) {
  const protectedRoots = new Set([...areas, ".vaults", ".memory-wiki.local.yml"]);
  const protectedReason = (first) => {
    if (first === ".vaults") return "the encrypted vaults directory";
    if (first === ".memory-wiki.local.yml") return "the local sync settings file";
    return `a path into the ${first} area`;
  };
  const inside = cwd === root || cwd.startsWith(`${root}/`);
  if (inside) {
    const first = path.relative(root, cwd).split(path.sep)[0];
    if (areas.includes(first)) return `the working directory is inside the ${first} area`;
    if (first === ".vaults") return "the working directory is inside the encrypted vaults directory";
  }
  for (const form of spellings(root, home, cwd)) {
    for (let at = text.indexOf(form); at !== -1; at = text.indexOf(form, at + 1)) {
      if (at > 0 && !TOKEN_BOUNDARY.test(text[at - 1])) continue;
      const rest = text.slice(at + form.length);
      if (/^[\w.-]/.test(rest)) continue; // a longer name, such as memory-wiki-backup
      const suffix = /^[^\s"'`;|&()<>]*/.exec(rest)?.[0] ?? "";
      if (suffix === "" || /^\/+$/.test(suffix)) {
        if (mounted.length > 0) {
          return `the whole wiki directory while ${mounted.join(", ")} is unlocked`;
        }
        continue;
      }
      if (!suffix.startsWith("/")) continue;
      const normalized = path.posix.normalize(suffix.replace(/^\/+/, ""));
      if (normalized === "." || normalized === "") {
        if (mounted.length > 0) {
          return `the whole wiki directory while ${mounted.join(", ")} is unlocked`;
        }
        continue;
      }
      const first = normalized.split("/")[0];
      if (protectedRoots.has(first)) return protectedReason(first);
      if (/[*?[\]{}]/.test(first) && mounted.length > 0) {
        return `a wildcard inside the wiki directory while ${mounted.join(", ")} is unlocked`;
      }
    }
  }
  if (inside) {
    const relFromRoot = path.relative(root, cwd).split(path.sep).join("/");
    for (const token of text.split(/[\s"'=:(,[\]{};|&<>]+/)) {
      if (!token) continue;
      const resolved = path.posix.normalize(
        token.startsWith("/") ? token : path.posix.join(relFromRoot || ".", token),
      );
      if (token.startsWith("/")) continue;
      const first = resolved.split("/")[0];
      if (protectedRoots.has(first)) return protectedReason(first);
      if ((resolved === "." || /[*?[\]{}]/.test(first)) && mounted.length > 0 && token.includes("..")) {
        return `the whole wiki directory while ${mounted.join(", ")} is unlocked`;
      }
    }
  }
  if (!checkCommands) return null;
  for (const call of wikiInvocations(text)) {
    const privateArea = call.areas.find((name) => name !== "default");
    if (privateArea) return `wiki --area ${privateArea}`;
    if (call.subcommand === "area") {
      const action = call.positionals[1] ?? "status";
      if (action === "unlock" || action === "create") return "unlocking a private area";
      if (action === "lock" && call.hasSync) return "syncing private areas (wiki area lock --sync)";
      if (action !== "status" && action !== "lock") {
        return `managing private areas (wiki area ${action})`;
      }
    }
  }
  if (/\bwiki\b/.test(text)) {
    for (const match of text.matchAll(/--area(?:=|\s+)["']?([\w-]+)/g)) {
      if (match[1] !== "default") return `wiki --area ${match[1]}`;
    }
  }
  return null;
}

/** `wiki verify` and `wiki review` record a person's review; an agent never runs them. */
export function runsVerify(text) {
  return wikiInvocations(text).some(
    (call) => call.subcommand === "verify" || call.subcommand === "review",
  );
}

/**
 * True only for a single, metacharacter-free read-only `wiki` CLI command in
 * the default space — safe for hooks to auto-approve.
 */
export function isSafeReadOnlyWikiCommand(command) {
  if (typeof command !== "string" || !command.trim()) return false;
  if (/[;&|`$><\n\r]/.test(command)) return false;
  const segments = shellSegments(command);
  if (segments.length !== 1) return false;
  const calls = wikiInvocations(command);
  if (calls.length !== 1) return false;
  const [call] = calls;
  if (!READ_ONLY_COMMANDS.has(call.subcommand)) return false;
  if (call.subcommand === "now" && call.hasSet) return false;
  if (call.areas.some((area) => area !== "default")) return false;
  return true;
}
