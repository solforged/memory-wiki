import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { capture, concepts, lint, recall, reindex, verify } from "../src/index.js";
import { append, edit, parse, stamp } from "../src/frontmatter.js";
import { NOW, concept, read, run, tempWiki, write } from "./helpers.js";

const HAND_WRITTEN = `---
# Reviewed quarterly.
type:   Concept
title: 'Band size'   # the default is 8 MB
tags: [ macos,disks ]
x-acme-owner: { team: storage }
status: stable
verified:
    - { by: human:ada, at: 2026-01-05 }
description: >
  How large each band file
  of a sparse bundle is.
---

Body text.
`;

test("an edit changes only the lines of the keys it sets (§4)", () => {
  const edited = edit(HAND_WRITTEN, [
    ["status", "draft"],
    ["generated", stamp("omp/model-1", "2026-11-21T12:00:00Z")],
  ]);
  const expected = HAND_WRITTEN.replace("status: stable", "status: draft").replace(
    "---\n\nBody",
    "---\n\nBody".replace(
      "---",
      'generated: { by: "omp/model-1", at: "2026-11-21T12:00:00Z" }\n---',
    ),
  );
  assert.equal(edited, expected);
});

test("a verification event is one added line; a bare mapping becomes a list", () => {
  const event = stamp("human:tester", "2026-11-21T12:00:00Z");
  const added = append(HAND_WRITTEN, "verified", event);
  assert.equal(
    added,
    HAND_WRITTEN.replace(
      "2026-01-05 }\n",
      '2026-01-05 }\n    - { by: "human:tester", at: "2026-11-21T12:00:00Z" }\n',
    ),
  );
  const bare = "---\ntype: Concept\nverified: { by: human:ada, at: 2026-01-05 }\n---\n";
  assert.deepEqual(parse(append(bare, "verified", event)).data.verified, [
    { by: "human:ada", at: "2026-01-05" },
    { by: "human:tester", at: "2026-11-21T12:00:00Z" },
  ]);
  const none = "---\ntype: Concept\n---\n";
  assert.deepEqual(parse(append(none, "verified", event)).data.verified, [
    { by: "human:tester", at: "2026-11-21T12:00:00Z" },
  ]);
});

test("capture writes a draft, the indexes, the log, and one commit", (t) => {
  const { root, config } = tempWiki(
    t,
    { "index.md": '---\nokf_version: "0.3"\ntitle: Mine\n---\n' },
    { git: true },
  );
  const made = capture(
    root,
    config,
    {
      by: "omp/model-1",
      type: "concept",
      title: "Sparse bundle",
      description: "A disk image kept\nas band files.",
      aliases: ["sparsebundle"],
      tags: ["macos"],
      sources: ["https://example.com/hdiutil"],
      body: "# Notes\n\nMade with hdiutil.",
    },
    { now: NOW },
  );
  assert.deepEqual(
    {
      id: made.id,
      status: made.status,
      tier: made.tier,
      created: made.created,
      committed: made.committed,
    },
    {
      id: "concepts/sparse-bundle",
      status: "draft",
      tier: "unverified",
      created: true,
      committed: true,
    },
  );
  assert.equal(
    read(root, "concepts/sparse-bundle.md"),
    `---
type: Concept
title: Sparse bundle
description: A disk image kept as band files.
tags: [macos]
x-llm-wiki-aliases: [sparsebundle]
status: draft
generated: { by: "omp/model-1", at: "2026-11-21T12:00:00Z" }
sources:
  - resource: https://example.com/hdiutil
---

# Notes

Made with hdiutil.
`,
  );
  assert.equal(
    read(root, "index.md"),
    '---\nokf_version: "0.3"\ntitle: Mine\n---\n\n# Directories\n\n* [concepts](concepts/) - 1 document\n',
    "the bundle-root index keeps its frontmatter (§8.1)",
  );
  assert.equal(
    read(root, "concepts/index.md"),
    "# Concept\n\n* [Sparse bundle](sparse-bundle.md) - A disk image kept as band files.\n",
  );
  assert.match(
    read(root, "log.md"),
    /^# Update Log\n\n## 2026-11-21\n\n\* \*\*Creation\*\*: Added \[Sparse bundle\]\(concepts\/sparse-bundle.md\)/,
  );
  assert.equal(
    run("git", ["-C", root, "status", "--porcelain", "--untracked-files=all"]).trim(),
    "?? .llm-wiki.yml",
  );
  assert.deepEqual(
    lint(root, config, { now: NOW.getTime() }).filter((finding) => finding.level !== "info"),
    [],
  );
});

test("verify makes a draft recallable; revising it withdraws that", (t) => {
  const { root, config } = tempWiki(t);
  const input = {
    by: "omp/model-1",
    type: "Concept",
    title: "Sparse bundle",
    description: "A disk image.",
  };
  const { id } = capture(root, config, input, { now: NOW });
  const recalled = () =>
    recall(concepts(root, config), "about the sparse bundle").map((file) => file.id);
  assert.deepEqual(recalled(), []);

  const later = new Date(NOW.getTime() + 60_000);
  const verified = verify(root, config, id, { now: later });
  assert.deepEqual([verified.status, verified.tier], ["stable", "human-reviewed"]);
  assert.deepEqual(recalled(), [id]);

  const revised = capture(
    root,
    config,
    { by: "omp/model-2", id, description: "A disk image kept as band files." },
    { now: new Date(NOW.getTime() + 120_000) },
  );
  assert.deepEqual([revised.status, revised.tier, revised.created], ["draft", "unverified", false]);
  assert.deepEqual(recalled(), []);
  assert.match(
    read(root, `${id}.md`),
    /verified:\n {2}- \{ by: "human:tester", at: "2026-11-21T12:01:00Z" \}/,
    "the earlier review stays as history",
  );
});

test("capture refuses a second concept under a name already taken", (t) => {
  const { root, config } = tempWiki(t);
  const base = { by: "human:tester", type: "Concept", description: "x" };
  capture(root, config, { ...base, title: "Sparse bundle", aliases: ["SB"] }, { now: NOW });
  assert.throws(
    () => capture(root, config, { ...base, title: "sparse  bundle" }),
    /exists; pass its id/,
  );
  assert.throws(
    () => capture(root, config, { ...base, title: "Bundles", aliases: ["sb"] }),
    /already names concepts\/sparse-bundle/,
  );
  assert.throws(
    () => capture(root, config, { ...base, title: "X", by: "someone" }),
    /by must be an actor/,
  );
  assert.throws(
    () => capture(root, config, { ...base, title: "X", type: "Metric" }),
    /type must be one of/,
  );
});

test("a change is recorded where git has no author identity", (t) => {
  const names = ["AUTHOR", "COMMITTER"].flatMap((role) => [
    `GIT_${role}_NAME`,
    `GIT_${role}_EMAIL`,
  ]);
  const saved = names.map((name) => process.env[name]);
  for (const name of names) delete process.env[name];
  t.after(() => names.forEach((name, i) => (process.env[name] = saved[i])));
  const { root, config } = tempWiki(t, {}, { git: true });

  capture(
    root,
    config,
    { by: "omp/m", type: "Entity", title: "Kept", description: "Stays." },
    { now: NOW },
  );

  assert.equal(
    run("git", ["-C", root, "log", "--format=%an <%ae>"]),
    "llm-wiki <llm-wiki@localhost>\n",
  );
});

test("a change is recorded without the signature git is set to make", (t) => {
  const { root, config } = tempWiki(t, {}, { git: true });
  run("git", ["-C", root, "config", "commit.gpgsign", "true"]);
  run("git", ["-C", root, "config", "gpg.program", "false"]);

  capture(
    root,
    config,
    { by: "omp/m", type: "Entity", title: "Kept", description: "Stays." },
    { now: NOW },
  );

  assert.equal(run("git", ["-C", root, "log", "--format=%s"]), "wiki: capture entities/kept\n");
});

test("a commit that git refuses leaves no trace of the change", (t) => {
  const { root, config } = tempWiki(t, {}, { git: true });
  capture(
    root,
    config,
    { by: "omp/m", type: "Entity", title: "Kept", description: "Stays." },
    { now: NOW },
  );
  const before = { index: read(root, "index.md"), log: read(root, "log.md") };
  write(root, ".git/hooks/pre-commit", "#!/bin/sh\necho 'listed terms found' >&2\nexit 1\n");
  fs.chmodSync(path.join(root, ".git/hooks/pre-commit"), 0o755);

  assert.throws(
    () =>
      capture(
        root,
        config,
        { by: "omp/m", type: "Entity", title: "Refused", description: "Goes." },
        { now: NOW },
      ),
    /git refused the change, so nothing was kept:\nlisted terms found/,
  );
  assert.equal(fs.existsSync(path.join(root, "entities/refused.md")), false);
  assert.deepEqual({ index: read(root, "index.md"), log: read(root, "log.md") }, before);
  assert.equal(run("git", ["-C", root, "status", "--porcelain", "--untracked-files=no"]), "");
});

test("reindex picks up a concept written by hand", (t) => {
  const { root, config } = tempWiki(t, {
    "projects/backup.md": concept(
      "type: Project\ntitle: Backups [nightly]\ndescription: Copies the wiki.",
    ),
  });
  assert.deepEqual(
    reindex(root, config)
      .map((abs) => path.relative(root, abs))
      .sort(),
    ["index.md", "projects/index.md"],
  );
  assert.equal(
    read(root, "projects/index.md"),
    "# Project\n\n* [Backups \\[nightly\\]](backup.md) - Copies the wiki.\n",
  );
  assert.deepEqual(reindex(root, config), [], "nothing to rewrite the second time");
});
