import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { concepts, describe, lint, lookup, recall, scan, search } from "../src/index.js";
import { extractLinks, resolveTarget } from "../src/links.js";
import { staleness, trustTier } from "../src/okf.js";
import { NOW, concept, tempWiki } from "./helpers.js";

test("only current verification events give the trust tier (§5.3)", () => {
  const human = { by: "human:ada", at: "2026-06-25T09:00:00Z" };
  const process = { by: "process:nightly", at: "2026-06-26T02:00:00Z" };
  assert.equal(trustTier({}), "unverified");
  assert.equal(trustTier({ verified: human }), "human-reviewed", "a bare mapping is one event");
  assert.equal(trustTier({ verified: [process] }), "machine-confirmed");
  assert.equal(
    trustTier({ verified: [{ by: "team:finance", at: human.at }] }),
    "machine-confirmed",
  );
  const rewritten = { generated: { by: "agent/1", at: "2026-06-25T12:00:00Z" } };
  assert.equal(trustTier({ ...rewritten, verified: [human] }), "unverified");
  assert.equal(trustTier({ ...rewritten, verified: [human, process] }), "machine-confirmed");
  assert.equal(trustTier({ verified: [{ by: "human:ada" }] }), "unverified", "an event needs at");
  assert.equal(
    trustTier({ generated: { by: "agent/1", at: "yesterday" }, verified: [human] }),
    "unverified",
    "an unreadable change time leaves no event provably current",
  );
});

test("stale_after that does not parse is never fresh (§5.5)", () => {
  const now = NOW.getTime();
  assert.equal(staleness({}, now), false);
  assert.equal(staleness({ stale_after: "" }, now), false);
  assert.equal(
    staleness({ stale_after: "2026-11-21" }, now),
    true,
    "a date is the start of that day, UTC",
  );
  assert.equal(staleness({ stale_after: "2026-11-22" }, now), false);
  assert.equal(staleness({ stale_after: "2026-11-21T13:00:00+02:00" }, now), true);
  assert.equal(staleness({ stale_after: "2026-11-22T00:00:00" }, now), "malformed", "no offset");
  assert.equal(staleness({ stale_after: "next week" }, now), "malformed");
});

test("which files are concepts (§3.1, §3.2)", (t) => {
  const { root, config } = tempWiki(t, {
    "index.md": "---\ntype: Concept\n---\n",
    "README.md": "# No frontmatter\n",
    "notes/untyped.md": "---\ntitle: No type\n---\n",
    "notes/kept.md": "---\ntype: Concept\n---\n",
    "notes/diagram.png": "",
    ".hidden/secret.md": "---\ntype: Concept\n---\n",
    "notes/.draft.md": "---\ntype: Concept\n---\n",
  });
  const outside = path.join(root, "..", `${path.basename(root)}-outside.md`);
  fs.writeFileSync(outside, "---\ntype: Concept\ntitle: Outside\n---\n");
  t.after(() => fs.rmSync(outside));
  fs.symlinkSync(outside, path.join(root, "notes", "escape.md"));

  const { files, problems } = scan(root, config);
  const kinds = Object.fromEntries(files.map((file) => [file.rel, file.kind]));
  assert.deepEqual(kinds, {
    "README.md": "page",
    "index.md": "index",
    "notes/diagram.png": "asset",
    "notes/kept.md": "concept",
    "notes/untyped.md": "page",
  });
  assert.deepEqual(problems, [
    { rel: "notes/escape.md", problem: "symbolic link leaves the bundle root" },
  ]);
  assert.equal(concepts(root, config)[0].title, "kept", "a missing title comes from the file name");
});

test("links resolve from the file or from the bundle root (§6)", () => {
  const from = "concepts/a.md";
  assert.deepEqual(resolveTarget("b.md", from), { kind: "internal", rel: "concepts/b.md" });
  assert.deepEqual(resolveTarget("../entities/c.md#top", from), {
    kind: "internal",
    rel: "entities/c.md",
  });
  assert.deepEqual(resolveTarget("/entities/my%20c.md", from), {
    kind: "internal",
    rel: "entities/my c.md",
  });
  assert.deepEqual(resolveTarget("../../elsewhere.md", from), { kind: "outside" });
  assert.deepEqual(resolveTarget("https://example.com/x.md", from), { kind: "url" });
  assert.deepEqual(resolveTarget("#section", from), { kind: "self" });
});

test("code, footnotes and wikilinks are not links (§6.1)", () => {
  const markdown = [
    "See [one](one.md) and ![shot](img/shot.png), `[not](code.md)`.",
    "A [reference link][ref], a [[wikilink]], a claim.[^src]",
    "```",
    "[fenced](fenced.md)",
    "```",
    "[ref]: <two.md>",
    "[^src]: A footnote, not a definition",
  ].join("\n");
  assert.deepEqual(
    extractLinks(markdown).map((link) => link.target),
    ["one.md", "img/shot.png", "two.md"],
  );
});

const GLOSSARY = {
  "concepts/sparse-bundle.md": concept(
    "type: Concept\ntitle: Sparse bundle\ndescription: A disk image kept as band files.\nx-llm-wiki-aliases: [sparsebundle]\ntags: macos",
    "Made with hdiutil.\n",
  ),
  "concepts/ci.md": concept("type: Concept\ntitle: Continuous integration\nx-llm-wiki-aliases: CI"),
  "entities/git-guard.md": concept(
    "type: Entity\ntitle: git-guard\ndescription: Keeps listed terms out of repositories.\nstatus: draft",
  ),
  "concepts/old.md": concept(
    "type: Concept\ntitle: Band file\nstatus: deprecated\nsuperseded_by: sparse-bundle.md",
  ),
};

test("lookup matches titles and aliases whatever the case or punctuation", (t) => {
  const { root, config } = tempWiki(t, GLOSSARY);
  const files = concepts(root, config);
  const ids = (term) => lookup(files, term).exact.map((file) => file.id);
  assert.deepEqual(ids("SPARSE-BUNDLE"), ["concepts/sparse-bundle"]);
  assert.deepEqual(ids("sparsebundle"), ["concepts/sparse-bundle"]);
  assert.deepEqual(ids("Git Guard"), ["entities/git-guard"]);
  assert.deepEqual(ids("ci"), ["concepts/ci"], "one scalar alias is a list of one (§11.2)");
  const near = lookup(files, "sparse bundel");
  assert.deepEqual(near.exact, []);
  assert.equal(near.candidates[0].id, "concepts/sparse-bundle", "a typo still finds the name");
});

test("recall returns stable concepts named in a text, the longest name first", (t) => {
  const { root, config } = tempWiki(t, {
    ...GLOSSARY,
    "concepts/bundle.md": concept("type: Concept\ntitle: Bundle"),
  });
  const files = concepts(root, config);
  const ids = (text) => recall(files, text).map((file) => file.id);
  assert.deepEqual(ids("Is a sparse bundle encrypted?"), ["concepts/sparse-bundle"]);
  assert.deepEqual(ids("ship the bundle"), ["concepts/bundle"]);
  assert.deepEqual(ids("does git-guard run here"), [], "drafts are not recalled");
  assert.deepEqual(ids("what is a band file"), [], "deprecated concepts are not recalled");
  assert.deepEqual(ids("the CI is red"), ["concepts/ci"]);
  assert.deepEqual(ids("ci sono due file"), [], "a two-letter name needs its capitals");
  assert.deepEqual(ids("bundles and sparsebundles"), [], "names match whole words");
});

test("search ranks a title match above a body match", (t) => {
  const { root, config } = tempWiki(t, {
    ...GLOSSARY,
    "projects/backup.md": concept(
      "type: Project\ntitle: Backups",
      "Uses a sparse bundle on the NAS.\n",
    ),
  });
  const files = concepts(root, config);
  const found = search(files, "sparse bundle");
  assert.deepEqual(
    found.map((result) => result.file.id),
    ["concepts/sparse-bundle", "projects/backup"],
  );
  assert.equal(found[1].snippet, "Uses a sparse bundle on the NAS.");
  assert.deepEqual(
    search(files, "bundle", { type: "project" }).map((result) => result.file.id),
    ["projects/backup"],
  );
  assert.deepEqual(
    search(files, "", { tag: "macos" }).map((result) => result.file.id),
    ["concepts/sparse-bundle"],
  );
});

test("describe reports status, tier and staleness", (t) => {
  const { root, config } = tempWiki(t, {
    "concepts/a.md": concept(
      'type: Concept\ntitle: A\nstatus: retired\nstale_after: "2026-01-01"\nverified: { by: "human:ada", at: "2026-01-01" }',
    ),
  });
  const info = describe(concepts(root, config)[0], NOW.getTime());
  assert.deepEqual(
    { status: info.status, tier: info.tier, stale: info.stale },
    { status: "retired", tier: "human-reviewed", stale: true },
  );
  assert.equal(
    concepts(root, config)[0].status.current,
    false,
    "an unknown status is not stable (§5.4)",
  );
});

test("lint separates non-conformance from maintenance findings", (t) => {
  const { root, config } = tempWiki(
    t,
    {
      "index.md": '---\nokf_version: "0.3"\n---\n',
      "log.md": "# Log\n\n## 21 Nov 2026\n\n* something\n",
      "concepts/index.md": "---\ntitle: nope\n---\n",
      "concepts/broken.md": "---\ntype: [unclosed\n---\n",
      "concepts/twice.md": "---\ntype: Concept\ntype: Entity\n---\n",
      "concepts/a.md": concept(
        'type: Concept\ntitle: A\ndescription: First.\nstale_after: "2020-01-01"\nverified: { by: "human:ada" }',
        "[gone](missing.md) [b](b.md) [secret](../work/concepts/x.md) [out](../../x.md)\n",
      ),
      "concepts/b.md": concept(
        "type: Concept\ntitle: B\ndescription: Second.\nx-llm-wiki-aliases: [A]",
      ),
      "stray.md": "no frontmatter\n",
    },
    { areas: { work: { passphrase: ["true"] } } },
  );
  const found = lint(root, config, { now: NOW.getTime() }).map(
    ({ level, rel, message }) => `${level} ${rel}: ${message.replace(/ \(§.*/, "")}`,
  );
  const expected = [
    'error concepts/a.md: line 1: link "../work/concepts/x.md" leads from the default space into a private area',
    "error concepts/broken.md: frontmatter:",
    "error concepts/index.md: only the bundle-root index.md may have frontmatter",
    "error concepts/twice.md: frontmatter: Map keys must be unique",
    'error log.md: date heading "21 Nov 2026" is not YYYY-MM-DD',
    'warning concepts/a.md: line 1: link "missing.md" has no target',
    'warning concepts/a.md: line 1: link "../../x.md" leaves the bundle root',
    "warning concepts/a.md: stale since 2020-01-01: check it again",
    "warning concepts/a.md: verified[0] needs by and at, so it is ignored",
    'warning concepts/b.md: "A" also names concepts/a.md',
    "warning stray.md: not a concept: no frontmatter type",
    "info concepts/a.md: no other concept links here",
  ];
  for (const line of expected) {
    assert.ok(
      found.some((entry) => entry.startsWith(line)),
      `missing: ${line}\n${found.join("\n")}`,
    );
  }
  assert.equal(found.length, expected.length, found.join("\n"));
});
