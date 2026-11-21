import assert from "node:assert/strict";
import { test } from "node:test";
import { inputText, isLocalModel, privateReference, runsVerify } from "../src/policy.js";

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

test("which tool inputs reach into a private area", () => {
  const home = "/Users/ada";
  const root = `${home}/.local/share/llm-wiki`;
  const where = { root, areas: ["personal", "work"], mounted: [], cwd: `${home}/src/app`, home };
  const reason = (input, over = {}) => privateReference(inputText(input), { ...where, ...over });

  assert.equal(reason({ path: `${root}/work/concepts/codename.md` }), "a path into the work area");
  assert.equal(
    reason({ command: "cat ~/.local/share/llm-wiki/personal/log.md" }),
    "a path into the personal area",
  );
  assert.equal(
    reason({ command: 'rg secret "$HOME/.local/share/llm-wiki/work"' }),
    "a path into the work area",
  );
  assert.equal(
    reason({ command: "ls ${XDG_DATA_HOME}/llm-wiki/work/" }),
    "a path into the work area",
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
  assert.equal(reason({ command: 'wiki search x --area="all"' }), "wiki --area all");
  assert.equal(reason({ command: "wiki lookup nightjar --area default" }), null);
  assert.equal(reason({ command: "wiki area unlock work" }), "unlocking a private area");
  assert.equal(reason({ command: "wiki area status" }), null);
});

test("an agent's bash command never verifies", () => {
  assert.equal(runsVerify("wiki verify concepts/a"), true);
  assert.equal(runsVerify("cd x && wiki   verify concepts/a"), true);
  assert.equal(runsVerify("wiki lookup verify"), false);
});
