import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";
import {
  areaStatus,
  capture,
  concepts,
  createArea,
  lint,
  loadConfig,
  lockArea,
  lookup,
  unlockArea,
} from "../src/index.js";
import { CONFIG_FILE } from "../src/config.js";
import { NOW, read, run, tempWiki } from "./helpers.js";

const passphrase = (secret) => ({ work: { size: "20m", passphrase: ["printf", "%s", secret] } });
const ROOT_INDEX = '---\nokf_version: "0.3"\n---\n';

// Creates, mounts and unmounts a real encrypted disk image: a few seconds.
test(
  "a private area is readable only while unlocked and respects MEMORY_WIKI_DEFAULT_ONLY",
  { skip: process.platform !== "darwin" },
  async (t) => {
    const { root, config } = tempWiki(
      t,
      { "index.md": ROOT_INDEX },
      { git: true, areas: passphrase("correct horse") },
    );
    const work = path.join(root, "work");
    const names = (areas, env) => concepts(root, config, { areas, env }).map((file) => file.id);

    await createArea(root, config, "work", { now: NOW });
    assert.deepEqual(
      areaStatus(root, config).map(({ name, exists, mounted }) => ({ name, exists, mounted })),
      [{ name: "work", exists: true, mounted: false }],
      "a new area is left locked",
    );
    assert.deepEqual(fs.readdirSync(work), [], "locked, the mountpoint holds no names");

    assert.throws(
      () =>
        capture(root, config, {
          by: "pi/m",
          type: "Concept",
          title: "Codename",
          description: "x",
          area: "work",
        }),
      /area "work" is locked/,
    );

    await assert.rejects(
      unlockArea(root, config, "work", { env: { MEMORY_WIKI_DEFAULT_ONLY: "1" } }),
      /MEMORY_WIKI_DEFAULT_ONLY/,
    );

    assert.equal(await unlockArea(root, config, "work"), true);
    assert.equal(await unlockArea(root, config, "work"), false, "unlocking twice is not an error");
    assert.equal(run("git", ["-C", work, "config", "--local", "guard.exempt"]).trim(), "true");

    const made = capture(
      root,
      config,
      {
        by: "pi/m",
        type: "Concept",
        title: "Codename",
        description: "The launch name.",
        area: "work",
      },
      { now: NOW },
    );
    assert.deepEqual(
      [made.id, made.area, made.committed],
      ["work/concepts/codename", "work", true],
    );
    assert.match(
      run("git", ["-C", work, "log", "--format=%s", "-1"]),
      /^wiki: capture work\/concepts\/codename/,
    );
    assert.equal(
      read(root, "work/index.md"),
      "# Directories\n\n* [concepts](concepts/) - 1 document\n",
    );
    assert.match(read(root, "work/log.md"), /Added \[Codename\]\(concepts\/codename\.md\)/);
    assert.equal(read(root, "index.md"), ROOT_INDEX, "the default space records nothing about it");
    assert.equal(fs.existsSync(path.join(root, "log.md")), false);

    assert.deepEqual(names(["default"]), [], "a read that does not name the area skips it");
    assert.deepEqual(names(["all"]), ["work/concepts/codename"]);
    assert.throws(
      () => names(["all"], { MEMORY_WIKI_DEFAULT_ONLY: "1" }),
      /MEMORY_WIKI_DEFAULT_ONLY/,
    );
    assert.deepEqual(
      lint(root, config, { areas: ["default"], now: NOW.getTime() }).filter(
        (finding) => finding.level !== "info",
      ),
      [],
      "default lint does not scan unlocked private areas",
    );
    assert.deepEqual(
      lint(root, config, { areas: ["all"], now: NOW.getTime() }).filter(
        (finding) => finding.level !== "info",
      ),
      [],
    );

    assert.equal(lockArea(root, config, "work"), true);
    assert.deepEqual(fs.readdirSync(work), []);
    assert.deepEqual(names(["all"]), []);
    assert.deepEqual(lookup(concepts(root, config, { areas: ["all"] }), "codename"), {
      exact: [],
      candidates: [],
    });

    fs.writeFileSync(path.join(root, CONFIG_FILE), JSON.stringify({ areas: passphrase("wrong") }));
    await assert.rejects(unlockArea(root, loadConfig(root), "work"), /could not unlock "work"/);
    assert.deepEqual(fs.readdirSync(work), []);
  },
);
