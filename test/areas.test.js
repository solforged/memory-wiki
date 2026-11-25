import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
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
  restoreArea,
  syncArea,
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
    assert.match(read(root, "work/.gitattributes"), /log\.md merge=union/);

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

const hasRealAge =
  spawnSync("sh", ["-c", "command -v age && command -v age-keygen"], { encoding: "utf8" }).status === 0;

test(
  "restoreArea bootstraps a second Mac sparsebundle from a real age-encrypted bundle",
  { skip: process.platform !== "darwin" || !hasRealAge },
  async (t) => {
    const keygen = run("age-keygen", []);
    const identity = keygen
      .split("\n")
      .find((line) => line.startsWith("AGE-SECRET-KEY-"))
      .trim();
    const recipient = keygen
      .split("\n")
      .find((line) => line.includes("public key: age1"))
      .replace(/.*public key:\s*/, "")
      .trim();

    const remoteDir = fs.mkdtempSync(path.join(path.dirname(tempWiki(t).root), "memory-wiki-remote-"));
    t.after(() => fs.rmSync(remoteDir, { recursive: true, force: true }));

    const areaSpec = {
      work: {
        size: "20m",
        passphrase: ["printf", "%s", "vault-pass"],
        age_recipient: recipient,
        age_identity: ["printf", "%s", identity],
        sync: { remote: remoteDir },
      },
    };

    const mac1 = tempWiki(t, { "index.md": ROOT_INDEX }, { git: true, areas: areaSpec });
    await createArea(mac1.root, mac1.config, "work", { now: NOW });
    await unlockArea(mac1.root, mac1.config, "work");
    capture(
      mac1.root,
      mac1.config,
      {
        by: "pi/m",
        type: "Concept",
        title: "Vault Entry",
        description: "Synced across real encrypted sparsebundles.",
        area: "work",
      },
      { now: NOW },
    );
    const pdfBytes = Buffer.from("%PDF-1.4\nfake-pdf-body\n");
    fs.mkdirSync(path.join(mac1.root, "work", "assets"), { recursive: true });
    fs.writeFileSync(path.join(mac1.root, "work", "assets", "spec.pdf"), pdfBytes);
    run("git", ["-C", path.join(mac1.root, "work"), "add", "assets/spec.pdf"]);
    run("git", ["-C", path.join(mac1.root, "work"), "commit", "--quiet", "-m", "Add spec.pdf"]);
    const expectedHead = run("git", ["-C", path.join(mac1.root, "work"), "rev-parse", "HEAD"]).trim();
    await syncArea(mac1.root, mac1.config, "work", { host: "hyperion" });
    lockArea(mac1.root, mac1.config, "work");

    // Second Mac restores into a brand-new sparsebundle without unrelated Git root commits.
    const mac2 = tempWiki(t, { "index.md": ROOT_INDEX }, { git: true, areas: areaSpec });
    const restored = await restoreArea(mac2.root, mac2.config, "work", { host: "sigil" });
    assert.equal(restored.head, expectedHead);
    assert.deepEqual(
      areaStatus(mac2.root, mac2.config).map(({ name, exists, mounted }) => ({ name, exists, mounted })),
      [{ name: "work", exists: true, mounted: false }],
      "restored area starts locked",
    );

    await unlockArea(mac2.root, mac2.config, "work");
    const work2 = path.join(mac2.root, "work");
    assert.equal(run("git", ["-C", work2, "rev-parse", "HEAD"]).trim(), expectedHead);
    assert.equal(run("git", ["-C", work2, "config", "--local", "guard.exempt"]).trim(), "true");
    assert.equal(fs.existsSync(path.join(work2, ".metadata_never_index")), true);
    assert.deepEqual(fs.readFileSync(path.join(work2, "assets", "spec.pdf")), pdfBytes);

    // Concurrent offline captures on both Macs auto-merge log.md and regenerate index.md cleanly.
    await unlockArea(mac1.root, mac1.config, "work");
    capture(
      mac1.root,
      mac1.config,
      {
        by: "pi/m",
        type: "Concept",
        title: "Hyperion Entry",
        description: "Captured offline on hyperion.",
        area: "work",
      },
      { now: NOW },
    );
    capture(
      mac2.root,
      mac2.config,
      {
        by: "pi/m",
        type: "Concept",
        title: "Sigil Entry",
        description: "Captured offline on sigil.",
        area: "work",
      },
      { now: NOW },
    );
    await syncArea(mac1.root, mac1.config, "work", { host: "hyperion" });
    const merged = await syncArea(mac2.root, mac2.config, "work", { host: "sigil" });
    assert.equal(merged.merged.length, 1);
    assert.match(read(mac2.root, "work/concepts/index.md"), /Hyperion Entry/);
    assert.match(read(mac2.root, "work/concepts/index.md"), /Sigil Entry/);
    assert.match(read(mac2.root, "work/log.md"), /Hyperion Entry/);
    assert.match(read(mac2.root, "work/log.md"), /Sigil Entry/);

    lockArea(mac1.root, mac1.config, "work");
    lockArea(mac2.root, mac2.config, "work");
  },
);
