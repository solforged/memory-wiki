// Private areas: one AES-256 encrypted sparse bundle each, mounted at
// <root>/<name> while unlocked. Locked, the mountpoint is an empty directory:
// no names and no text are on disk outside the image. macOS only.
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { forget, isMounted } from "./bundle.js";
import { defaultOnly } from "./config.js";
import { gitIn } from "./git.js";
import { localDate } from "./okf.js";

const IGNORED = "/.fseventsd/\n/.Spotlight-V100/\n/.TemporaryItems/\n/.Trashes/\n.DS_Store\n";
const GITATTRIBUTES = "log.md merge=union\n";

export function needArea(config, name, { requireMac = true } = {}) {
  if (requireMac && process.platform !== "darwin") {
    throw new Error("private areas need macOS disk images");
  }
  const area = config.areas[name];
  if (!area) throw new Error(`unknown area "${name}"`);
  return area;
}

/**
 * Runs a program without blocking: a passphrase command may wait on a Touch ID
 * prompt for as long as the user takes. Resolves with its stdout.
 */
export function runAsync(command, args, input = "") {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    child.stdin.on("error", () => {}); // a program that exits without reading its stdin
    child.on("error", (error) => reject(new Error(`cannot run ${command}: ${error.message}`)));
    child.on("close", (status) => {
      if (status === 0) resolve(stdout);
      else reject(new Error((stderr || stdout).trim() || `${command} exited with ${status}`));
    });
    child.stdin.end(input);
  });
}

/**
 * Runs a configured secret command (such as `[op, read, ...]`). Its stdout is
 * kept in memory only and never passed as an argument, file, or log line.
 */
export async function readSecret(commandSpec, label = "passphrase") {
  const [command, ...args] = commandSpec;
  let secret;
  try {
    secret = (await runAsync(command, args)).replace(/\r?\n$/, "");
  } catch (error) {
    throw new Error(`the ${label} command failed: ${error.message}`);
  }
  if (!secret) throw new Error(`the ${label} command printed nothing`);
  return secret;
}

export async function attachArea(root, name, area, secret) {
  const mountpoint = path.join(root, name);
  fs.mkdirSync(mountpoint, { recursive: true });
  if (fs.readdirSync(mountpoint).length > 0) {
    throw new Error(
      `${mountpoint} is not empty: files written there while "${name}" was locked are not encrypted, move them first`,
    );
  }
  const args = [
    "image",
    "attach",
    "--stdinpass",
    "--nobrowse",
    "--mountPoint",
    mountpoint,
    area.image,
  ];
  try {
    await runAsync("diskutil", args, secret);
  } catch (error) {
    throw new Error(`could not unlock "${name}": ${error.message}`);
  }
  if (!isMounted(root, name))
    throw new Error(`"${name}" attached but is not mounted at ${mountpoint}`);
}

export async function createEncryptedImage(name, area, secret) {
  fs.mkdirSync(path.dirname(area.image), { recursive: true });
  await runAsync(
    "hdiutil",
    [
      "create",
      "-size",
      area.size,
      "-type",
      "SPARSEBUNDLE",
      "-fs",
      "APFS",
      "-encryption",
      "AES-256",
      "-stdinpass",
      "-volname",
      `memory-wiki-${name}`,
      area.image,
    ],
    secret,
  );
}

/** @returns {{ name: string, image: string, exists: boolean, mounted: boolean, mountpoint: string }[]} */
export function areaStatus(root, config) {
  return Object.entries(config.areas).map(([name, area]) => ({
    name,
    image: area.image,
    exists: fs.existsSync(area.image),
    mounted: isMounted(root, name),
    mountpoint: path.join(root, name),
  }));
}

/** Mounts the area. False when it already was. */
export async function unlockArea(root, config, name, { env = process.env } = {}) {
  if (defaultOnly(env)) {
    throw new Error("private areas are disabled in this environment (MEMORY_WIKI_DEFAULT_ONLY)");
  }
  const area = needArea(config, name);
  if (isMounted(root, name)) return false;
  if (!fs.existsSync(area.image)) {
    throw new Error(`area "${name}" has no image yet; run: wiki area create ${name} (or wiki area restore ${name})`);
  }
  await attachArea(root, name, area, await readSecret(area.passphrase, "passphrase"));
  return true;
}

/** Unmounts the area. False when it already was. `force` unmounts with files open. */
export function lockArea(root, config, name, { force = false } = {}) {
  needArea(config, name);
  if (!isMounted(root, name)) return false;
  const mountpoint = path.join(root, name);
  const result = spawnSync("hdiutil", ["detach", mountpoint, ...(force ? ["-force"] : [])], {
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`could not lock "${name}": ${(result.stderr || result.stdout || "").trim()}`);
  }
  forget(mountpoint);
  return true;
}

/**
 * Creates the area's image with the passphrase its command prints, gives it a
 * log and its own git repository, and leaves it locked. The repository sets
 * guard.exempt: it holds terms that git-guard keeps out of every other
 * repository, and git-guard then refuses to push it.
 */
export async function createArea(root, config, name, { now = new Date(), env = process.env } = {}) {
  if (defaultOnly(env)) {
    throw new Error("private areas are disabled in this environment (MEMORY_WIKI_DEFAULT_ONLY)");
  }
  const area = needArea(config, name);
  if (fs.existsSync(area.image))
    throw new Error(`area "${name}" already has an image: ${area.image}`);
  const secret = await readSecret(area.passphrase, "passphrase");
  await createEncryptedImage(name, area, secret);
  await attachArea(root, name, area, secret);
  const dir = path.join(root, name);
  try {
    const date = localDate(now);
    fs.writeFileSync(path.join(dir, ".metadata_never_index"), ""); // keeps Spotlight out
    fs.writeFileSync(path.join(dir, ".gitignore"), IGNORED);
    fs.writeFileSync(path.join(dir, ".gitattributes"), GITATTRIBUTES);
    fs.writeFileSync(
      path.join(dir, "log.md"),
      `# Update Log\n\n## ${date}\n\n* **Initialization**: Created the ${name} area.\n`,
    );
    await runAsync("git", ["-C", dir, "init", "--quiet"]);
    await runAsync("git", ["-C", dir, "config", "guard.exempt", "true"]);
    await runAsync("git", ["-C", dir, "add", "--all"]);
    await runAsync("git", [...gitIn(dir), "commit", "--quiet", "-m", `wiki: create the ${name} area`]);
  } finally {
    lockArea(root, config, name, { force: true });
  }
}
