// YAML frontmatter (OKF §4): split, read, write, and edit one key at a time
// so every other line of the block stays byte for byte as it was.
import { Document, Scalar, isMap, isScalar, isSeq, parseDocument } from "yaml";

const YAML = { version: "1.2" }; // the core schema is YAML 1.2's default
const scratch = new Document({}, YAML);

/**
 * The block between a first line that is exactly `---` and the next such line.
 * @returns {{ yaml: string | null, body: string, unterminated?: boolean }}
 */
export function split(text) {
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  if (!/^---\r?\n/.test(text)) return { yaml: null, body: text };
  const start = text.indexOf("\n") + 1;
  for (let at = start; at <= text.length;) {
    const end = text.indexOf("\n", at);
    const line = (end === -1 ? text.slice(at) : text.slice(at, end)).replace(/\r$/, "");
    if (line === "---") {
      return { yaml: text.slice(start, at), body: end === -1 ? "" : text.slice(end + 1) };
    }
    if (end === -1) break;
    at = end + 1;
  }
  return { yaml: null, body: text, unterminated: true };
}

const firstLine = (error) => error.message.split("\n")[0];

/**
 * @returns {{ data: Record<string, any> | null, body: string, errors: string[], hasBlock: boolean }}
 * `data` is null when there is no block or it is not a YAML mapping.
 */
export function parse(text) {
  const parts = split(text);
  if (parts.yaml === null) {
    return {
      data: null,
      body: parts.body,
      errors: parts.unterminated ? ["frontmatter has no closing --- line"] : [],
      hasBlock: Boolean(parts.unterminated),
    };
  }
  const doc = parseDocument(parts.yaml, YAML);
  const errors = doc.errors.map(firstLine);
  let data = null;
  if (errors.length === 0) {
    const value = doc.toJS() ?? {};
    if (typeof value === "object" && !Array.isArray(value)) data = value;
    else errors.push("frontmatter is not a YAML mapping");
  }
  return { data, body: parts.body, errors, hasBlock: true };
}

/** A double-quoted scalar: the spec's examples quote timestamps and actors. */
export function quoted(value) {
  const scalar = new Scalar(String(value));
  scalar.type = Scalar.QUOTE_DOUBLE;
  return scalar;
}

/** `[a, b]` */
export function flowList(values) {
  const node = scratch.createNode(values);
  node.flow = true;
  return node;
}

/** `{ by: "…", at: "…" }`: the shape of `generated` and of a `verified` event. */
export function stamp(by, at) {
  const node = scratch.createNode({});
  node.flow = true;
  node.set("by", quoted(by));
  node.set("at", quoted(at));
  return node;
}

// `[a, b]` unpadded and `{ by: … }` padded, as the spec writes them.
const render = (doc, value) =>
  doc.toString({ lineWidth: 0, flowCollectionPadding: !(isSeq(value) && value.flow) });

/** `key: value` as YAML lines. */
function renderPair(key, value) {
  const doc = new Document({}, YAML);
  doc.set(key, value);
  return render(doc, value);
}

/** A new file from `[key, value]` pairs, in order, and a Markdown body. */
export function compose(pairs, body) {
  const text = (body ?? "").trim();
  const yaml = pairs.map(([key, value]) => renderPair(key, value)).join("");
  return `---\n${yaml}---\n${text ? `\n${text}\n` : ""}`;
}

/**
 * Where each top-level key's lines start and end in a block mapping. Null
 * when the block is some other shape, such as one flow mapping.
 */
function layout(yaml) {
  const doc = parseDocument(yaml, YAML);
  if (doc.errors.length > 0) throw new Error(firstLine(doc.errors[0]));
  const spans = new Map();
  if (doc.contents === null) return spans;
  if (!isMap(doc.contents) || doc.contents.flow) return null;
  for (const pair of doc.contents.items) {
    if (!isScalar(pair.key) || !pair.key.range) return null;
    const start = yaml.lastIndexOf("\n", pair.key.range[0] - 1) + 1;
    let end = pair.value?.range ? pair.value.range[1] : pair.key.range[1];
    if (yaml[end - 1] !== "\n") {
      const newline = yaml.indexOf("\n", end);
      end = newline === -1 ? yaml.length : newline + 1;
    }
    spans.set(String(pair.key.value), { start, end, value: pair.value });
  }
  return spans;
}

const assemble = (yaml, body) => `---\n${yaml}---\n${body}`;

/**
 * Sets top-level keys. A key that exists has only its own lines replaced; a
 * new key goes at the end. Everything else in the block keeps its text,
 * order, comments and style (§4), and unknown keys survive (§4.1).
 * @param {[string, unknown][]} pairs
 */
export function edit(text, pairs) {
  const parts = split(text);
  if (parts.yaml === null) throw new Error("no frontmatter to edit");
  const spans = layout(parts.yaml);
  if (spans === null) {
    const doc = parseDocument(parts.yaml, YAML);
    for (const [key, value] of pairs) doc.set(key, value);
    return assemble(doc.toString({ lineWidth: 0 }), parts.body);
  }
  let yaml = parts.yaml;
  const replaced = [];
  let added = "";
  for (const [key, value] of pairs) {
    const span = spans.get(key);
    if (span) replaced.push({ ...span, text: renderPair(key, value) });
    else added += renderPair(key, value);
  }
  for (const { start, end, text: lines } of replaced.sort((a, b) => b.start - a.start)) {
    yaml = yaml.slice(0, start) + lines + yaml.slice(end);
  }
  return assemble(yaml + added, parts.body);
}

/**
 * Adds an item to the list at a top-level key. A block list gains one line;
 * a missing key becomes a list, and a single value becomes a list of two
 * (§11.2: one item reads as a list with one item).
 */
export function append(text, key, item) {
  const parts = split(text);
  if (parts.yaml === null) throw new Error("no frontmatter to edit");
  const span = layout(parts.yaml)?.get(key);
  const list = span?.value;
  if (isSeq(list) && !list.flow && list.items.length > 0 && list.items[0].range) {
    const yaml = parts.yaml;
    const first = list.items[0].range[0];
    const lineStart = yaml.lastIndexOf("\n", first - 1) + 1;
    const indent = /^ */.exec(yaml.slice(lineStart))[0];
    const doc = new Document(item, YAML);
    const line = `${indent}- ${render(doc, item).trimEnd()}\n`;
    return assemble(yaml.slice(0, span.end) + line + yaml.slice(span.end), parts.body);
  }
  const node = scratch.createNode([]);
  if (isSeq(list)) for (const existing of list.items) node.add(existing);
  else if (list && !(isScalar(list) && (list.value === null || list.value === ""))) node.add(list);
  node.add(item);
  return edit(text, [[key, node]]);
}
