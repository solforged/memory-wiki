// Creates a new wiki: an OKF v0.3 bundle root in its own git repository.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CONFIG_FILE, LOCAL_CONFIG_FILE, VAULT_DIR } from "./config.js";
import { gitIn } from "./git.js";
import { localDate } from "./okf.js";
import { ROOT_INDEX } from "./write.js";

const TEMPLATES = fileURLToPath(new URL("../templates/", import.meta.url));

const settings = (human) => `# memory-wiki settings. Dot-files are outside the OKF bundle.

# Who "wiki verify" records as the reviewer.
human: ${human}

# Private areas: encrypted disk images mounted at <root>/<name> while unlocked.
# "passphrase" is a command that prints the image's passphrase.
areas:
  personal:
    image: ${VAULT_DIR}/personal.sparsebundle
    size: 2g
    passphrase: [op, read, "op://Private/llm-wiki personal/password"]
  work:
    image: ${VAULT_DIR}/work.sparsebundle
    size: 2g
    passphrase: [op, read, "op://Private/llm-wiki work/password"]
`;

/** @param {{ now?: Date }} [options] */
export function init(root, { now = new Date() } = {}) {
  if (fs.existsSync(path.join(root, "index.md"))) throw new Error(`${root} already holds a wiki`);
  fs.mkdirSync(root, { recursive: true });
  const date = localDate(now);
  const files = {
    "index.md": ROOT_INDEX,
    "log.md": `# Update Log\n\n## ${date}\n\n* **Initialization**: Created the wiki.\n`,
    "AGENTS.md": fs.readFileSync(path.join(TEMPLATES, "AGENTS.md"), "utf8"),
    [CONFIG_FILE]: settings(`human:${os.userInfo().username}`),
    ".gitignore": `/${VAULT_DIR}/\n/${LOCAL_CONFIG_FILE}\n/personal/\n/work/\n.DS_Store\n`,
  };
  for (const [name, text] of Object.entries(files)) fs.writeFileSync(path.join(root, name), text);
  const git = (...args) => {
    const result = spawnSync("git", [...gitIn(root), ...args], { encoding: "utf8" });
    if (result.status !== 0) {
      throw new Error(`git ${args[0]} failed: ${(result.stderr || result.stdout).trim()}`);
    }
  };
  git("init", "--quiet");
  git("add", "--", ...Object.keys(files));
  git("commit", "--quiet", "-m", "wiki: init");
}
