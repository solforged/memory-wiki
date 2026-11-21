// Checks a bundle: OKF v0.3 conformance (§11.1) as errors, and what a
// maintainer wants to hear about (stale, broken, ambiguous) as warnings.
import fs from "node:fs";
import path from "node:path";
import { areaOf, scan } from "./bundle.js";
import { TYPE_DIRS } from "./config.js";
import { extractLinks, pathFields, resolveTarget } from "./links.js";
import { normalizeKey } from "./lookup.js";
import { asList, present, staleness, timestamp } from "./okf.js";

// Files a bundle may hold that are not concepts (§3.2 rule 2).
const PAGES = new Set([
  "AGENTS.md",
  "CODE_OF_CONDUCT.md",
  "CONTRIBUTING.md",
  "LICENSE.md",
  "README.md",
  "SKILL.md",
]);

/**
 * @typedef {{ level: "error" | "warning" | "info", rel: string, message: string }} Finding
 * @param {{ areas?: string[], now?: number }} [options]
 * @returns {Finding[]} in path order
 */
export function lint(root, config, { areas = ["all"], now = Date.now() } = {}) {
  /** @type {Finding[]} */
  const findings = [];
  const add = (level, rel, message) => findings.push({ level, rel, message });
  const { files, problems } = scan(root, config, { areas });
  for (const { rel, problem } of problems) add("warning", rel, problem);

  const byRel = new Map(files.map((file) => [file.rel, file]));
  const dirs = new Set();
  for (const file of files) {
    for (let dir = path.posix.dirname(file.rel); dir !== "."; dir = path.posix.dirname(dir)) {
      dirs.add(dir);
    }
  }
  const exists = (rel) =>
    rel === "" || byRel.has(rel) || dirs.has(rel) || fs.existsSync(path.join(root, rel));

  const rootIndex = byRel.get("index.md");
  if (!rootIndex || !present(rootIndex.fm.okf_version)) {
    add("warning", "index.md", "the bundle-root index.md should declare okf_version (§8.1)");
  }

  const lower = new Map();
  for (const file of files) {
    const key = file.rel.toLowerCase();
    if (lower.has(key))
      add("warning", file.rel, `differs from ${lower.get(key)} only by case (§6)`);
    else lower.set(key, file.rel);
  }

  const inbound = new Set();
  const named = new Map();
  for (const file of files) {
    if (file.kind === "asset") continue;
    for (const error of file.errors) add("error", file.rel, `frontmatter: ${error} (§4)`);

    if (file.kind === "index" && file.rel !== "index.md" && file.hasBlock) {
      add("error", file.rel, "only the bundle-root index.md may have frontmatter (§8)");
    }
    if (file.kind === "log") {
      if (file.hasBlock) add("error", file.rel, "log.md has no frontmatter (§9)");
      for (const line of file.body.split("\n")) {
        const heading = /^## +(.*?)\s*$/.exec(line);
        if (!heading) continue;
        const date = heading[1];
        if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || Number.isNaN(Date.parse(`${date}T00:00:00Z`))) {
          add("error", file.rel, `date heading "${date}" is not YYYY-MM-DD (§9)`);
        }
      }
    }
    if (file.kind === "page" && file.errors.length === 0) {
      if (!PAGES.has(path.posix.basename(file.rel))) {
        add("warning", file.rel, "not a concept: no frontmatter type (§3.2)");
      }
      continue;
    }

    const check = (target, what) => {
      const to = resolveTarget(target, file.rel);
      if (to.kind === "outside") {
        add("warning", file.rel, `${what} "${target}" leaves the bundle root (§6)`);
        return;
      }
      if (to.kind !== "internal") return;
      if (file.area === "default" && areaOf(to.rel, config) !== "default") {
        add(
          "error",
          file.rel,
          `${what} "${target}" leads from the default space into a private area`,
        );
        return;
      }
      if (!exists(to.rel)) add("warning", file.rel, `${what} "${target}" has no target`);
      else if (file.kind === "concept" && to.rel !== file.rel) inbound.add(to.rel);
    };
    for (const link of extractLinks(file.body)) check(link.target, `line ${link.line}: link`);
    if (file.kind !== "concept") continue;

    const fm = file.fm;
    for (const { field, value } of pathFields(fm)) check(value, field);

    if (!file.status.known) {
      add(
        "warning",
        file.rel,
        `status "${file.status.status}" is not draft, stable or deprecated (§5.4)`,
      );
    }
    if (present(fm.superseded_by) && file.status.status !== "deprecated") {
      add("warning", file.rel, "superseded_by means something only with status: deprecated (§5.4)");
    }
    const stale = staleness(fm, now);
    if (stale === true)
      add("warning", file.rel, `stale since ${fm.stale_after}: check it again (§5.5)`);

    const times = [
      ["generated.at", fm.generated?.at],
      ["stale_after", fm.stale_after],
      ["usage_window.from", fm.usage_window?.from],
      ["usage_window.to", fm.usage_window?.to],
    ];
    if (present(fm.generated) && !present(fm.generated?.by)) {
      add("warning", file.rel, "generated needs by (§5.2)");
    }
    asList(fm.verified).forEach((event, index) => {
      if (!present(event?.by) || !present(event?.at)) {
        add("warning", file.rel, `verified[${index}] needs by and at, so it is ignored (§5.2)`);
      }
      times.push([`verified[${index}].at`, event?.at]);
    });
    asList(fm.sources).forEach((source, index) => {
      if (!present(source?.resource)) {
        add("warning", file.rel, `sources[${index}] needs resource, so it is ignored (§5.1)`);
      }
      times.push([`sources[${index}].last_modified`, source?.last_modified]);
    });
    for (const [key, value] of times) {
      if (present(value) && Number.isNaN(timestamp(value))) {
        add(
          "warning",
          file.rel,
          `${key} "${value}" is not an RFC 3339 date or date-time with an offset (§5)`,
        );
      }
    }
    if (file.type === "Attested Computation" && !present(fm.runtime)) {
      add("warning", file.rel, "an Attested Computation needs runtime (§10.2)");
    }
    if (Object.hasOwn(TYPE_DIRS, file.type)) {
      if (!present(fm.title)) add("warning", file.rel, "no title");
      if (!present(fm.description))
        add("warning", file.rel, "no description: lookups return nothing to read");
    }
    for (const name of [file.title, ...file.aliases]) {
      const key = `${file.area}\n${normalizeKey(name)}`;
      const other = named.get(key);
      if (other && other !== file.rel) add("warning", file.rel, `"${name}" also names ${other}`);
      else named.set(key, file.rel);
    }
  }

  for (const file of files) {
    if (file.kind === "concept" && !inbound.has(file.rel)) {
      add("info", file.rel, "no other concept links here");
    }
  }
  const order = { error: 0, warning: 1, info: 2 };
  return findings.sort((a, b) => a.rel.localeCompare(b.rel) || order[a.level] - order[b.level]);
}
