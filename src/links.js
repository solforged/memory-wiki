// Links and path fields (OKF §6): finding them and resolving them.
import path from "node:path";
import { asList, present } from "./okf.js";

const INLINE =
  /(!?)\[((?:[^[\]\\]|\\.|\[[^[\]]*\])*)\]\(\s*(<[^>\n]*>|(?:[^\s()\\]|\\.|\((?:[^\s()\\]|\\.)*\))*)(?:\s+(?:"[^"]*"|'[^']*'|\([^)]*\)))?\s*\)/g;
const REFERENCE = /(!?)\[((?:[^[\]\\]|\\.)+)\](?:\[((?:[^[\]\\]|\\.)*)\])?(?![[(:])/g;
const DEFINITION = /^ {0,3}\[((?:[^[\]\\]|\\.)+)\]:\s*(<[^>\n]*>|\S+)/;

function unwrap(target) {
  const inner = target.startsWith("<") && target.endsWith(">") ? target.slice(1, -1) : target;
  return inner.replace(/\\([!-/:-@[-`{-~])/g, "$1");
}

const label = (text) => text.trim().replace(/\s+/g, " ").toLowerCase();

/**
 * `[text](target)` and reference-style links (§6.1), with images. Code blocks,
 * code spans and footnotes are not links. A scanner, not a CommonMark parser:
 * link text may nest brackets one level deep.
 * @returns {{ target: string, line: number, image: boolean }[]}
 */
export function extractLinks(markdown) {
  const lines = markdown.split("\n");
  const prose = [];
  let fence = null;
  for (const line of lines) {
    const mark = /^ {0,3}(`{3,}|~{3,})/.exec(line);
    if (fence) {
      if (mark && mark[1][0] === fence[0] && mark[1].length >= fence.length) fence = null;
      prose.push("");
    } else if (mark) {
      fence = mark[1];
      prose.push("");
    } else {
      prose.push(line.replace(/(`+)(?:(?!\1).)+\1/g, (span) => " ".repeat(span.length)));
    }
  }
  const definitions = new Map();
  prose.forEach((line, index) => {
    const match = DEFINITION.exec(line);
    if (!match || match[1].startsWith("^")) return; // `[^id]:` is a footnote
    definitions.set(label(match[1]), unwrap(match[2]));
    prose[index] = "";
  });
  const links = [];
  prose.forEach((line, index) => {
    const rest = line.replace(INLINE, (whole, bang, _text, target) => {
      links.push({ target: unwrap(target), line: index + 1, image: bang === "!" });
      return " ".repeat(whole.length);
    });
    for (const match of rest.matchAll(REFERENCE)) {
      const name = label(match[3] || match[2]);
      if (name.startsWith("^") || !definitions.has(name)) continue;
      links.push({ target: definitions.get(name), line: index + 1, image: match[1] === "!" });
    }
  });
  return links;
}

/**
 * §6 resolution: a scheme is a URL; a leading `/` starts at the bundle root;
 * anything else starts at the directory of the containing file. A result
 * outside the root is external and is never read.
 * @param {string} fromRel path of the containing file from the bundle root
 * @returns {{ kind: "url" | "self" | "outside" } | { kind: "internal", rel: string }}
 */
export function resolveTarget(target, fromRel) {
  if (/^[a-z][a-z0-9+.-]*:/i.test(target)) return { kind: "url" };
  let value = target.split("#")[0].split("?")[0];
  if (value === "") return { kind: "self" };
  try {
    value = decodeURIComponent(value);
  } catch {
    // keep the text as written
  }
  const base = value.startsWith("/") ? "" : path.posix.dirname(fromRel);
  const rel = path.posix.normalize(path.posix.join(base, value.replace(/^\/+/, "")));
  if (rel === ".." || rel.startsWith("../")) return { kind: "outside" };
  return { kind: "internal", rel: rel === "." ? "" : rel.replace(/\/$/, "") };
}

/**
 * Path fields of a concept (§6.2) as `{ field, value }`. `resource` and
 * `sources[].resource` may hold a URL or a scope descriptor instead of a path,
 * so only values written as paths (`/…`, `./…`, `../…`) are returned for them.
 */
export function pathFields(fm) {
  const fields = [];
  const pathLike = (value) => typeof value === "string" && /^(\/|\.\.?\/)/.test(value);
  const always = (field, value) => {
    if (typeof value === "string" && present(value)) fields.push({ field, value });
  };
  always("superseded_by", fm.superseded_by);
  always("computation", fm.computation);
  always("executor.resource", fm.executor?.resource);
  always("attester.resource", fm.attester?.resource);
  if (pathLike(fm.resource)) fields.push({ field: "resource", value: fm.resource });
  for (const source of asList(fm.sources)) {
    if (pathLike(source?.resource))
      fields.push({ field: "sources[].resource", value: source.resource });
  }
  return fields;
}
