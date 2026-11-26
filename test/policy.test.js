import assert from "node:assert/strict";
import { test } from "node:test";
import {
  inputText,
  isLocalModel,
  isSafeReadOnlyWikiCommand,
  privateReference,
  runsVerify,
} from "../src/policy.js";

const PROVIDERS = ["llama.cpp", "lm-studio", "ollama", "local"];

test("a model is local only on a listed provider at a loopback address", () => {
  const local = (model) => isLocalModel(model, PROVIDERS);
  assert.equal(
    local({ provider: "llama.cpp", id: "qwen3", baseUrl: "http://127.0.0.1:8080" }),
    true,
  );
  assert.equal(
    local({ provider: "lm-studio", id: "x", baseUrl: "http://localhost:1234/v1" }),
    true,
  );
  assert.equal(local({ provider: "ollama", id: "x", baseUrl: "http://[::1]:11434" }), true);
  assert.equal(local({ provider: "local", id: "lfm2-1.2b" }), true);
  assert.equal(
    local({ provider: "anthropic", id: "claude", baseUrl: "https://api.anthropic.com/v1" }),
    false,
  );
  assert.equal(
    local({ provider: "llama.cpp", id: "x", baseUrl: "http://10.0.0.5:8080" }),
    false,
    "a LAN server",
  );
  assert.equal(
    local({ provider: "litellm", id: "x", baseUrl: "http://localhost:4000/v1" }),
    false,
    "a local gateway to hosted models",
  );
  assert.equal(
    local({ provider: "ollama", id: "gpt-oss:120b-cloud", baseUrl: "http://127.0.0.1:11434" }),
    false,
  );
  assert.equal(local({ provider: "llama.cpp", id: "x" }), false);
  assert.equal(local(undefined), false);
});

test("which tool inputs reach into a private area, including traversals and multiple flags", () => {
  const home = "/Users/ada";
  const root = `${home}/.local/share/memory-wiki`;
  const where = { root, areas: ["personal", "work"], mounted: [], cwd: `${home}/src/app`, home };
  const reason = (input, over = {}) => privateReference(inputText(input), { ...where, ...over });

  assert.equal(reason({ path: `${root}/work/concepts/codename.md` }), "a path into the work area");
  assert.equal(
    reason({ command: "cat ~/.local/share/memory-wiki/personal/log.md" }),
    "a path into the personal area",
  );
  assert.equal(
    reason({ command: 'rg secret "$HOME/.local/share/memory-wiki/work"' }),
    "a path into the work area",
  );
  assert.equal(
    reason({ command: "ls ${XDG_DATA_HOME}/memory-wiki/work/" }),
    "a path into the work area",
  );
  assert.equal(
    reason({ command: "cat $MEMORY_WIKI_HOME/concepts/../work/codename.md" }),
    "a path into the work area",
    "dot-dot traversal into a private area",
  );
  assert.equal(
    reason({ command: `cat ${root}/./personal/log.md` }),
    "a path into the personal area",
  );
  assert.equal(
    reason({ command: "cat .local/share/memory-wiki/work/log.md" }, { cwd: home }),
    "a path into the work area",
    "relative path from an ancestor cwd",
  );
  assert.equal(
    reason({ paths: [`${root}/concepts/a.md`, `${root}/work`] }),
    "a path into the work area",
  );

  assert.equal(reason({ path: `${root}/concepts/sparse-bundle.md` }), null);
  assert.equal(reason({ command: `grep -r x ${root}/concepts` }), null);
  assert.equal(
    reason({ path: `${root}-backup/work/x.md` }),
    null,
    "another directory with a longer name",
  );
  assert.equal(reason({ command: "echo work on the personal site" }), null);

  assert.equal(
    reason({ command: `find ${root} -name '*.md'` }),
    null,
    "nothing private is on disk while locked",
  );
  assert.equal(
    reason({ command: `find ${root} -name '*.md'` }, { mounted: ["work"] }),
    "the whole wiki directory while work is unlocked",
  );
  assert.equal(
    reason({ path: `${root}/` }, { mounted: ["work"] }),
    "the whole wiki directory while work is unlocked",
  );
  assert.equal(
    reason({ command: `ls ${root}/wo*` }, { mounted: ["work"] }),
    "a wildcard inside the wiki directory while work is unlocked",
  );
  assert.equal(reason({ path: `${root}/index.md` }, { mounted: ["work"] }), null);

  assert.equal(reason({ command: "cat work/log.md" }), null, "a relative path elsewhere");
  assert.equal(reason({ command: "cat work/log.md" }, { cwd: root }), "a path into the work area");
  assert.equal(
    reason({ command: "cat ../work/log.md" }, { cwd: `${root}/concepts` }),
    "a path into the work area",
  );
  assert.equal(
    reason({ command: "ls" }, { cwd: `${root}/work/concepts` }),
    "the working directory is inside the work area",
  );

  assert.equal(reason({ command: "wiki lookup nightjar --area work" }), "wiki --area work");
  assert.equal(
    reason({ command: "wiki lookup nightjar --area default --area work" }),
    "wiki --area work",
    "second --area flag must still be caught",
  );
  assert.equal(reason({ command: 'wiki search x --area="all"' }), "wiki --area all");
  assert.equal(reason({ command: "wiki lookup nightjar --area default" }), null);
  assert.equal(reason({ command: "wiki area unlock work" }), "unlocking a private area");
  assert.equal(
    reason({ command: "wiki --json area unlock work" }),
    "unlocking a private area",
    "flags before subcommand",
  );
  assert.equal(reason({ command: "wiki area status" }), null);
  assert.equal(reason({ command: "wiki area lock work" }), null);
  assert.equal(
    reason({ command: "wiki area lock --sync work" }),
    "syncing private areas (wiki area lock --sync)",
  );
  assert.equal(
    reason({ command: "wiki area sync work" }),
    "managing private areas (wiki area sync)",
  );
  assert.equal(
    reason({ command: "wiki area restore personal" }),
    "managing private areas (wiki area restore)",
  );
  assert.equal(
    reason({ command: "wiki area compact personal" }),
    "managing private areas (wiki area compact)",
  );
  assert.equal(
    reason({ path: `${root}/.vaults/personal.sparsebundle/token` }),
    "the encrypted vaults directory",
  );
  assert.equal(
    reason({ path: `${root}/.memory-wiki.local.yml` }),
    "the local sync settings file",
  );
  assert.equal(
    reason({ text: "Documenting wiki area sync personal in README" }, { checkCommands: false }),
    null,
  );
  assert.equal(
    reason({ path: `${root}/.vaults/work.sparsebundle` }, { checkCommands: false }),
    "the encrypted vaults directory",
  );
});

test("an agent's bash command never verifies or reviews", () => {
  assert.equal(runsVerify("wiki verify concepts/a"), true);
  assert.equal(runsVerify("wiki --json verify concepts/a"), true);
  assert.equal(runsVerify("~/.local/bin/wiki review"), true);
  assert.equal(runsVerify("cd x && wiki   verify concepts/a"), true);
  assert.equal(runsVerify("wiki lookup verify"), false);
});

test("isSafeReadOnlyWikiCommand accepts only single read-only default-space calls", () => {
  assert.equal(isSafeReadOnlyWikiCommand("wiki lookup 'Sparse bundle'"), true);
  assert.equal(isSafeReadOnlyWikiCommand("wiki search macos --type Concept --json"), true);
  assert.equal(isSafeReadOnlyWikiCommand("wiki show concepts/sparse-bundle"), true);
  assert.equal(isSafeReadOnlyWikiCommand("wiki now"), true);
  assert.equal(isSafeReadOnlyWikiCommand("wiki recent --limit 5"), true);
  assert.equal(isSafeReadOnlyWikiCommand("wiki dream status"), true);
  assert.equal(isSafeReadOnlyWikiCommand("wiki dream schedule status"), true);
  assert.equal(isSafeReadOnlyWikiCommand("wiki dream --now"), false);
  assert.equal(isSafeReadOnlyWikiCommand("wiki dream undo"), false);
  assert.equal(isSafeReadOnlyWikiCommand("wiki dream status --area work"), false);
  assert.equal(isSafeReadOnlyWikiCommand("wiki now --set"), false);
  assert.equal(isSafeReadOnlyWikiCommand("wiki capture --title x --description y"), false);
  assert.equal(isSafeReadOnlyWikiCommand("wiki lookup x --area work"), false);
  assert.equal(isSafeReadOnlyWikiCommand("wiki lookup x && echo hi"), false);
  assert.equal(isSafeReadOnlyWikiCommand("wiki lookup $(whoami)"), false);
  assert.equal(isSafeReadOnlyWikiCommand("wiki verify concepts/a"), false);
});
