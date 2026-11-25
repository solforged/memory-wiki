// Multi-Mac sync and restore for private areas using age-encrypted git bundles.
//
// Each Mac keeps its own local AES-256 APFS sparsebundle in `.vaults/`, and
// syncs committed Git history (Markdown + PDFs/SVGs/PNGs) as per-host
// `<host>.bundle.age` blobs on a WebDAV server (such as a Synology NAS over
// Tailscale) or a local directory. Private repos keep `guard.exempt = true`
// and never run `git push`. Plaintext bundles and decrypted key material never
// touch `/tmp`, the wire, or the remote store.
import { spawn, spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  attachArea,
  createEncryptedImage,
  lockArea,
  needArea,
  readSecret,
  runAsync,
} from "./areas.js";
import { forget, isMounted } from "./bundle.js";
import { defaultHost, defaultOnly } from "./config.js";
import { gitIn } from "./git.js";
import { renderIndexes } from "./write.js";

const BUNDLE_FILE = /^([a-z0-9][a-z0-9._-]*)\.bundle\.age$/i;
const LOOPBACK = /^(localhost|127(\.\d{1,3}){3}|\[::1\])$/i;
const SYNC_STATE_REL = path.join(".git", "memory-wiki-sync.json");

const PROPFIND_BODY =
  '<?xml version="1.0" encoding="utf-8"?>' +
  '<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/><d:getlastmodified/></d:prop></d:propfind>';

function sha256(buffer) {
  return crypto.createHash("sha256").update(buffer).digest("hex");
}

function unescapeXml(text) {
  return text
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

function expandPath(raw, root) {
  if (raw.startsWith("file://")) return fileURLToPath(raw);
  if (raw === "~") return os.homedir();
  if (raw.startsWith("~/")) return path.join(os.homedir(), raw.slice(2));
  return path.resolve(root, raw);
}

/**
 * Encrypts a binary buffer to one or more `age1...` public recipients.
 * @param {Buffer} plaintext
 * @param {string[]} recipients
 * @param {{ ageBin?: string }} [options]
 * @returns {Promise<Buffer>}
 */
export function ageEncrypt(plaintext, recipients, { ageBin = "age" } = {}) {
  if (!Array.isArray(recipients) || recipients.length === 0) {
    throw new Error("no age_recipient configured; set areas.<name>.age_recipient to an age1... key");
  }
  const args = ["-e"];
  for (const recipient of recipients) {
    if (!/^age1[0-9a-z]+$/.test(recipient)) {
      throw new Error(`invalid age recipient "${recipient}"`);
    }
    args.push("-r", recipient);
  }
  return new Promise((resolve, reject) => {
    const child = spawn(ageBin, args, { stdio: ["pipe", "pipe", "pipe"] });
    const out = [];
    let stderr = "";
    child.stdout.on("data", (chunk) => out.push(chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    child.stdin.on("error", () => {});
    child.on("error", (error) => {
      if (error.code === "ENOENT") {
        reject(new Error(`"${ageBin}" is not installed; install age (aqua:FiloSottile/age)`));
      } else {
        reject(new Error(`cannot run ${ageBin}: ${error.message}`));
      }
    });
    child.on("close", (status) => {
      if (status === 0) resolve(Buffer.concat(out));
      else reject(new Error(`age encrypt failed: ${stderr.trim() || `exit ${status}`}`));
    });
    child.stdin.end(plaintext);
  });
}

/**
 * Decrypts ciphertext to `outFile` using `identitySecret`. The temporary
 * identity file is created `0600` inside `workDir` (which lives inside the
 * mounted encrypted sparsebundle), zeroed, and removed immediately after use.
 * @param {Buffer} ciphertext
 * @param {string} identitySecret
 * @param {string} outFile
 * @param {{ workDir: string, ageBin?: string }} options
 */
export async function ageDecryptToFile(ciphertext, identitySecret, outFile, { workDir, ageBin = "age" }) {
  const idFile = path.join(workDir, `.memory-wiki-id-${process.pid}-${crypto.randomBytes(6).toString("hex")}`);
  const idBuf = Buffer.from(`${identitySecret.trim()}\n`, "utf8");
  fs.writeFileSync(idFile, idBuf, { mode: 0o600 });
  try {
    await new Promise((resolve, reject) => {
      const child = spawn(ageBin, ["-d", "-i", idFile, "-o", outFile], {
        stdio: ["pipe", "pipe", "pipe"],
      });
      let stderr = "";
      child.stderr.on("data", (chunk) => (stderr += chunk));
      child.stdin.on("error", () => {});
      child.on("error", (error) => {
        if (error.code === "ENOENT") {
          reject(new Error(`"${ageBin}" is not installed; install age (aqua:FiloSottile/age)`));
        } else {
          reject(new Error(`cannot run ${ageBin}: ${error.message}`));
        }
      });
      child.on("close", (status) => {
        if (status === 0) resolve();
        else reject(new Error(`age decrypt failed: ${stderr.trim() || `exit ${status}`}`));
      });
      child.stdin.end(ciphertext);
    });
  } finally {
    try {
      fs.writeFileSync(idFile, Buffer.alloc(idBuf.length), { mode: 0o600 });
    } catch {
      // best effort wipe before unlink
    }
    fs.rmSync(idFile, { force: true });
  }
}

/** Parses WebDAV PROPFIND XML responses into `<host>.bundle.age` entries. */
export function parseWebDavPropfind(xml) {
  const items = [];
  const seen = new Set();
  const responseRe = /<(?:[A-Za-z0-9_-]+:)?response\b[^>]*>([\s\S]*?)<\/(?:[A-Za-z0-9_-]+:)?response>/gi;
  const tag = (block, name) => {
    const match = new RegExp(
      `<(?:[A-Za-z0-9_-]+:)?${name}\\b[^>]*>([\\s\\S]*?)<\\/(?:[A-Za-z0-9_-]+:)?${name}>`,
      "i",
    ).exec(block);
    return match ? unescapeXml(match[1].trim()) : undefined;
  };
  for (const match of xml.matchAll(responseRe)) {
    const block = match[1];
    const href = tag(block, "href");
    if (!href) continue;
    const cleanHref = href.split("?")[0].replace(/\/+$/, "");
    const rawBase = cleanHref.slice(cleanHref.lastIndexOf("/") + 1);
    let name;
    try {
      name = decodeURIComponent(rawBase);
    } catch {
      continue;
    }
    const bundleMatch = BUNDLE_FILE.exec(name);
    if (!bundleMatch || seen.has(name)) continue;
    seen.add(name);
    items.push({
      name,
      host: bundleMatch[1].toLowerCase(),
      etag: tag(block, "getetag") || null,
      lastModified: tag(block, "getlastmodified") || null,
    });
  }
  return items.sort((a, b) => a.name.localeCompare(b.name));
}

function createLocalRemote(baseDir) {
  return {
    location: baseDir,
    async list(area) {
      const dir = path.join(baseDir, area);
      let names;
      try {
        names = fs.readdirSync(dir);
      } catch (error) {
        if (error.code === "ENOENT") return [];
        throw error;
      }
      const items = [];
      for (const name of names) {
        const match = BUNDLE_FILE.exec(name);
        if (!match) continue;
        const stat = fs.statSync(path.join(dir, name));
        if (!stat.isFile()) continue;
        items.push({
          name,
          host: match[1].toLowerCase(),
          etag: `${stat.size}:${Math.trunc(stat.mtimeMs)}`,
          lastModified: stat.mtime.toISOString(),
        });
      }
      return items.sort((a, b) => a.name.localeCompare(b.name));
    },
    async get(area, name) {
      return fs.readFileSync(path.join(baseDir, area, name));
    },
    async put(area, name, data) {
      const dir = path.join(baseDir, area);
      fs.mkdirSync(dir, { recursive: true });
      const tmp = path.join(dir, `${name}.tmp.${process.pid}.${crypto.randomBytes(4).toString("hex")}`);
      const dst = path.join(dir, name);
      fs.writeFileSync(tmp, data, { mode: 0o600 });
      try {
        fs.renameSync(tmp, dst);
      } catch (error) {
        fs.rmSync(tmp, { force: true });
        throw error;
      }
    },
  };
}

function createWebDavRemote(urlText, { username = null, password = null, fetchImpl = fetch } = {}) {
  const parsed = new URL(urlText);
  if (parsed.protocol === "http:") {
    if (!LOOPBACK.test(parsed.hostname)) {
      throw new Error(
        `WebDAV sync requires https:// (plain http:// is allowed only on loopback, got ${parsed.hostname})`,
      );
    }
  } else if (parsed.protocol !== "https:") {
    throw new Error(`unsupported sync remote scheme "${parsed.protocol}"`);
  }
  const baseUrl = urlText.replace(/\/+$/, "");
  let authHeaderPromise = null;

  const getHeaders = async (extra = {}) => {
    const headers = { ...extra };
    if (username !== null || password !== null) {
      authHeaderPromise ??= (async () => {
        const user = Array.isArray(username)
          ? await readSecret(username, "sync username")
          : (username ?? "");
        const pass = password ? await readSecret(password, "sync password") : "";
        return `Basic ${Buffer.from(`${user}:${pass}`, "utf8").toString("base64")}`;
      })();
      headers.Authorization = await authHeaderPromise;
    }
    return headers;
  };

  const areaUrl = (area) => `${baseUrl}/${encodeURIComponent(area)}`;
  const fileUrl = (area, name) => `${areaUrl(area)}/${encodeURIComponent(name)}`;

  const mkcol = async (url) => {
    const res = await fetchImpl(url, { method: "MKCOL", headers: await getHeaders() });
    return res.status;
  };

  const ensureDir = async (area) => {
    const target = `${areaUrl(area)}/`;
    const status = await mkcol(target);
    if (status === 201 || status === 405 || status === 200 || status === 204 || status === 301 || status === 302) {
      return;
    }
    if (status === 409) {
      await mkcol(`${baseUrl}/`);
      const retry = await mkcol(target);
      if (retry === 201 || retry === 405 || retry === 200 || retry === 204 || retry === 301 || retry === 302) {
        return;
      }
      throw new Error(`WebDAV MKCOL ${target} failed (${retry})`);
    }
    throw new Error(`WebDAV MKCOL ${target} failed (${status})`);
  };

  return {
    location: baseUrl,
    async list(area) {
      const target = `${areaUrl(area)}/`;
      const res = await fetchImpl(target, {
        method: "PROPFIND",
        headers: await getHeaders({
          Depth: "1",
          "Content-Type": "application/xml; charset=utf-8",
        }),
        body: PROPFIND_BODY,
      });
      if (res.status === 404) return [];
      if (res.status !== 207 && !res.ok) {
        throw new Error(`WebDAV PROPFIND ${target} failed (${res.status} ${res.statusText})`);
      }
      return parseWebDavPropfind(await res.text());
    },
    async get(area, name) {
      const target = fileUrl(area, name);
      const res = await fetchImpl(target, { method: "GET", headers: await getHeaders() });
      if (!res.ok) {
        throw new Error(`WebDAV GET ${target} failed (${res.status} ${res.statusText})`);
      }
      return Buffer.from(await res.arrayBuffer());
    },
    async put(area, name, data) {
      await ensureDir(area);
      const tmpName = `${name}.tmp.${process.pid}.${crypto.randomBytes(4).toString("hex")}`;
      const tmpUrl = fileUrl(area, tmpName);
      const dstUrl = fileUrl(area, name);

      const putRes = await fetchImpl(tmpUrl, {
        method: "PUT",
        headers: await getHeaders({ "Content-Type": "application/octet-stream" }),
        body: data,
      });
      if (!putRes.ok) {
        throw new Error(`WebDAV PUT ${tmpUrl} failed (${putRes.status} ${putRes.statusText})`);
      }

      const moveRes = await fetchImpl(tmpUrl, {
        method: "MOVE",
        headers: await getHeaders({ Destination: dstUrl, Overwrite: "T" }),
      });
      if (moveRes.ok || moveRes.status === 201 || moveRes.status === 204) return;

      if (moveRes.status === 405 || moveRes.status === 501) {
        const direct = await fetchImpl(dstUrl, {
          method: "PUT",
          headers: await getHeaders({ "Content-Type": "application/octet-stream" }),
          body: data,
        });
        await fetchImpl(tmpUrl, { method: "DELETE", headers: await getHeaders() }).catch(() => {});
        if (!direct.ok) {
          throw new Error(`WebDAV PUT ${dstUrl} failed (${direct.status} ${direct.statusText})`);
        }
        return;
      }

      await fetchImpl(tmpUrl, { method: "DELETE", headers: await getHeaders() }).catch(() => {});
      throw new Error(`WebDAV MOVE to ${dstUrl} failed (${moveRes.status} ${moveRes.statusText})`);
    },
  };
}

/** Creates a local-directory or WebDAV remote client from resolved area sync config. */
export function createSyncRemote(root, syncConfig, { remoteOverride, env = process.env, fetchImpl = fetch } = {}) {
  const remoteSpec = remoteOverride?.trim() || syncConfig?.remote || env.MEMORY_WIKI_SYNC_REMOTE?.trim();
  if (!remoteSpec) {
    throw new Error(
      "no sync remote configured; set sync.remote in .memory-wiki.local.yml (or pass --remote)",
    );
  }
  if (/^https?:\/\//i.test(remoteSpec)) {
    return createWebDavRemote(remoteSpec, {
      username: syncConfig?.username ?? null,
      password: syncConfig?.password ?? null,
      fetchImpl,
    });
  }
  return createLocalRemote(expandPath(remoteSpec, root));
}

function loadSyncState(dir) {
  const abs = path.join(dir, SYNC_STATE_REL);
  try {
    const parsed = JSON.parse(fs.readFileSync(abs, "utf8"));
    return {
      host: typeof parsed?.host === "string" ? parsed.host : null,
      lastPushedHead: typeof parsed?.lastPushedHead === "string" ? parsed.lastPushedHead : null,
      peers: parsed?.peers && typeof parsed.peers === "object" ? parsed.peers : {},
    };
  } catch {
    return { host: null, lastPushedHead: null, peers: {} };
  }
}

function saveSyncState(dir, state) {
  const abs = path.join(dir, SYNC_STATE_REL);
  fs.writeFileSync(abs, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
}

function gitSync(dir, args) {
  return spawnSync("git", ["-C", dir, ...args], { encoding: "utf8" });
}

function gitChecked(dir, args) {
  const result = gitSync(dir, args);
  if (result.status !== 0) {
    throw new Error(`git ${args[0]} failed: ${(result.stderr || result.stdout).trim()}`);
  }
  return result.stdout;
}

function pickBranch(branches) {
  if (branches.includes("main")) return "main";
  if (branches.includes("master")) return "master";
  return branches[0] ?? null;
}

function resolveIndexOnlyConflicts(root, config, name, dir, env) {
  const unmerged = gitChecked(dir, ["diff", "--name-only", "--diff-filter=U"])
    .trim()
    .split("\n")
    .filter(Boolean);
  if (unmerged.length === 0) return false;
  if (!unmerged.every((rel) => rel === "index.md" || rel.endsWith("/index.md"))) {
    throw new Error(
      `merge conflicts in "${name}" need manual resolution (${unmerged.join(", ")}). Resolve in ${dir}, commit, then re-run: wiki area sync ${name}`,
    );
  }
  // Regenerate all index.md files deterministically and finish the merge commit.
  for (const [abs, text] of renderIndexes(root, config, name, env)) {
    fs.mkdirSync(path.dirname(abs), { recursive: true });
    fs.writeFileSync(abs, text);
    forget(abs);
  }
  gitChecked(dir, ["add", "--", ...unmerged]);
  const commit = spawnSync("git", [...gitIn(dir), "commit", "--quiet", "--no-edit"], {
    encoding: "utf8",
  });
  if (commit.status !== 0) {
    throw new Error(`git commit after index regeneration failed: ${(commit.stderr || commit.stdout).trim()}`);
  }
  return true;
}

/**
 * Syncs an unlocked private area with the configured remote:
 * 1. Verifies the working tree has no uncommitted files (so new PDFs/images are not silently skipped).
 * 2. Pulls and decrypts changed peer `<host>.bundle.age` blobs inside the encrypted volume and merges them.
 * 3. Exports and uploads `<localHost>.bundle.age` encrypted to `age_recipient` when `HEAD` changed.
 */
export async function syncArea(
  root,
  config,
  name,
  {
    env = process.env,
    remote: remoteOverride,
    host: hostOverride,
    forcePush = false,
    pullOnly = false,
    pushOnly = false,
    allowDirty = false,
    ageBin = "age",
    fetchImpl = fetch,
  } = {},
) {
  if (defaultOnly(env)) {
    throw new Error("private areas are disabled in this environment (MEMORY_WIKI_DEFAULT_ONLY)");
  }
  const area = needArea(config, name, { requireMac: false });
  const dir = path.join(root, name);
  if (
    (fs.existsSync(area.image) && !isMounted(root, name)) ||
    (!isMounted(root, name) && !fs.existsSync(path.join(dir, ".git")))
  ) {
    throw new Error(`area "${name}" is locked; unlock it with: wiki area unlock ${name}`);
  }

  const localHost = (hostOverride?.trim() || area.sync.host || defaultHost(env)).toLowerCase();
  const remote = createSyncRemote(root, area.sync, { remoteOverride, env, fetchImpl });

  const dirty = gitChecked(dir, ["status", "--porcelain"]).trim();
  if (dirty && !allowDirty) {
    const sample = dirty
      .split("\n")
      .slice(0, 6)
      .map((line) => line.trim())
      .join(", ");
    throw new Error(
      `area "${name}" has uncommitted changes (${sample}); commit them (including any new PDFs/images) or add local-only files to .gitignore before syncing`,
    );
  }

  const state = loadSyncState(dir);
  state.host = localHost;
  const selfName = `${localHost}.bundle.age`;

  let identitySecret = null;
  const needIdentity = async () => {
    if (identitySecret !== null) return identitySecret;
    if (!area.ageIdentity) {
      throw new Error(
        `areas.${name}.age_identity is required to decrypt peer bundles (e.g. [op, read, "op://Private/llm-wiki ${name}/age_identity"])`,
      );
    }
    identitySecret = await readSecret(area.ageIdentity, "age identity");
    return identitySecret;
  };

  const remoteEntries = await remote.list(name);
  const remoteHasSelf = remoteEntries.some((entry) => entry.name === selfName);
  const pulled = [];
  const merged = [];

  if (!pushOnly) {
    for (const entry of remoteEntries) {
      const isSelf = entry.name === selfName;
      if (isSelf && state.lastPushedHead) continue;
      const cached = state.peers[entry.host];
      if (cached && entry.etag && cached.etag === entry.etag) continue;

      const ciphertext = await remote.get(name, entry.name);
      const digest = sha256(ciphertext);
      if (cached && cached.sha256 === digest) {
        state.peers[entry.host] = { etag: entry.etag, lastModified: entry.lastModified, sha256: digest };
        continue;
      }

      const secret = await needIdentity();
      const tmpBundle = path.join(
        dir,
        ".git",
        `memory-wiki-import-${entry.host}-${process.pid}-${crypto.randomBytes(4).toString("hex")}.bundle`,
      );
      try {
        await ageDecryptToFile(ciphertext, secret, tmpBundle, {
          workDir: path.join(dir, ".git"),
          ageBin,
        });
        const peerPrefix = `refs/remotes/sync-${entry.host}`;
        gitChecked(dir, ["fetch", "--quiet", tmpBundle, `+refs/heads/*:${peerPrefix}/*`]);
        const refs = gitChecked(dir, ["for-each-ref", "--format=%(refname:short)", peerPrefix])
          .trim()
          .split("\n")
          .filter(Boolean);
        const branchNames = refs.map((ref) => ref.slice(`sync-${entry.host}/`.length));
        const chosenBranch = pickBranch(branchNames);
        if (chosenBranch) {
          const peerRef = `${peerPrefix}/${chosenBranch}`;
          pulled.push(entry.host);
          const alreadyMerged = gitSync(dir, ["merge-base", "--is-ancestor", peerRef, "HEAD"]).status === 0;
          if (!alreadyMerged) {
            const ff = gitSync(dir, ["merge", "--quiet", "--ff-only", peerRef]);
            if (ff.status === 0) {
              merged.push({ peer: entry.host, mode: "fast-forward" });
            } else {
              const merge = spawnSync(
                "git",
                [...gitIn(dir), "merge", "--quiet", "--no-edit", "-m", `wiki: sync ${name} from ${entry.host}`, peerRef],
                { encoding: "utf8" },
              );
              if (merge.status === 0) {
                merged.push({ peer: entry.host, mode: "merge" });
              } else {
                resolveIndexOnlyConflicts(root, config, name, dir, env);
                merged.push({ peer: entry.host, mode: "merge-reindexed" });
              }
            }
          }
        }
      } finally {
        fs.rmSync(tmpBundle, { force: true });
      }
      state.peers[entry.host] = { etag: entry.etag, lastModified: entry.lastModified, sha256: digest };
    }
  }

  const head = gitChecked(dir, ["rev-parse", "HEAD"]).trim();
  let pushed = false;
  let bytes = 0;

  if (!pullOnly && (forcePush || state.lastPushedHead !== head || !remoteHasSelf)) {
    const tmpOut = path.join(
      dir,
      ".git",
      `memory-wiki-export-${process.pid}-${crypto.randomBytes(4).toString("hex")}.bundle`,
    );
    let plainBundle;
    try {
      gitChecked(dir, ["bundle", "create", "--quiet", tmpOut, "--branches", "--tags"]);
      plainBundle = fs.readFileSync(tmpOut);
    } finally {
      fs.rmSync(tmpOut, { force: true });
    }
    const encrypted = await ageEncrypt(plainBundle, area.ageRecipients, { ageBin });
    await remote.put(name, selfName, encrypted);
    bytes = encrypted.length;
    pushed = true;
    state.lastPushedHead = head;
    state.lastPushedAt = new Date().toISOString();
  }

  saveSyncState(dir, state);
  return { area: name, host: localHost, remote: remote.location, pulled, merged, pushed, head, bytes };
}

function chooseRestoreEntry(entries, from) {
  if (entries.length === 0) {
    throw new Error("no encrypted *.bundle.age files found on the sync remote");
  }
  if (from) {
    const wanted = from.toLowerCase();
    const hit = entries.find((entry) => entry.host === wanted || entry.name.toLowerCase() === wanted);
    if (!hit) {
      throw new Error(
        `no bundle for "${from}" on remote (found: ${entries.map((entry) => entry.host).join(", ")})`,
      );
    }
    return hit;
  }
  const ranked = [...entries].sort((a, b) => {
    const ta = a.lastModified ? Date.parse(a.lastModified) : 0;
    const tb = b.lastModified ? Date.parse(b.lastModified) : 0;
    if (Number.isFinite(tb) && Number.isFinite(ta) && tb !== ta) return tb - ta;
    return a.name.localeCompare(b.name);
  });
  return ranked[0];
}

/**
 * Restores a private area into a fresh local AES-256 sparsebundle on this Mac
 * from a remote `<host>.bundle.age` or a local `.bundle.age` file, preserving
 * the exact Git history and setting `guard.exempt = true`. Leaves it locked.
 */
export async function restoreArea(
  root,
  config,
  name,
  {
    env = process.env,
    from,
    remote: remoteOverride,
    host: hostOverride,
    ageBin = "age",
    fetchImpl = fetch,
  } = {},
) {
  if (defaultOnly(env)) {
    throw new Error("private areas are disabled in this environment (MEMORY_WIKI_DEFAULT_ONLY)");
  }
  const area = needArea(config, name);
  if (fs.existsSync(area.image)) {
    throw new Error(`area "${name}" already has an image: ${area.image}`);
  }
  if (!area.ageIdentity) {
    throw new Error(
      `areas.${name}.age_identity is required to restore from an encrypted bundle`,
    );
  }

  let ciphertext;
  let source;
  let peerMeta = null;
  if (from && fs.existsSync(from) && fs.statSync(from).isFile()) {
    ciphertext = fs.readFileSync(from);
    source = path.resolve(from);
  } else {
    const remote = createSyncRemote(root, area.sync, { remoteOverride, env, fetchImpl });
    const entries = await remote.list(name);
    const chosen = chooseRestoreEntry(entries, from);
    ciphertext = await remote.get(name, chosen.name);
    source = `${remote.location}/${name}/${chosen.name}`;
    peerMeta = {
      host: chosen.host,
      etag: chosen.etag,
      lastModified: chosen.lastModified,
      sha256: sha256(ciphertext),
    };
  }

  const identitySecret = await readSecret(area.ageIdentity, "age identity");
  const diskSecret = await readSecret(area.passphrase, "passphrase");
  const localHost = (hostOverride?.trim() || area.sync.host || defaultHost(env)).toLowerCase();

  await createEncryptedImage(name, area, diskSecret);
  let attached = false;
  try {
    await attachArea(root, name, area, diskSecret);
    attached = true;
    const dir = path.join(root, name);
    const tmpBundle = path.join(dir, `.restore-${process.pid}-${crypto.randomBytes(4).toString("hex")}.bundle`);
    try {
      await ageDecryptToFile(ciphertext, identitySecret, tmpBundle, { workDir: dir, ageBin });
      await runAsync("git", ["-C", dir, "init", "--quiet"]);
      await runAsync("git", ["-C", dir, "config", "guard.exempt", "true"]);
      gitChecked(dir, [
        "fetch",
        "--quiet",
        "--update-head-ok",
        tmpBundle,
        "+refs/heads/*:refs/heads/*",
        "+refs/tags/*:refs/tags/*",
      ]);
    } finally {
      fs.rmSync(tmpBundle, { force: true });
    }

    const branches = gitChecked(dir, ["for-each-ref", "--format=%(refname:short)", "refs/heads"])
      .trim()
      .split("\n")
      .filter(Boolean);
    const branch = pickBranch(branches);
    if (!branch) throw new Error(`restored bundle for "${name}" contained no Git branches`);

    gitChecked(dir, ["symbolic-ref", "HEAD", `refs/heads/${branch}`]);
    gitChecked(dir, ["reset", "--hard", "--quiet", "HEAD"]);
    if (!fs.existsSync(path.join(dir, ".metadata_never_index"))) {
      fs.writeFileSync(path.join(dir, ".metadata_never_index"), "");
    }
    const head = gitChecked(dir, ["rev-parse", "HEAD"]).trim();
    saveSyncState(dir, {
      host: localHost,
      lastPushedHead: null,
      peers: peerMeta
        ? {
            [peerMeta.host]: {
              etag: peerMeta.etag,
              lastModified: peerMeta.lastModified,
              sha256: peerMeta.sha256,
            },
          }
        : {},
    });
    lockArea(root, config, name, { force: true });
    attached = false;
    return { area: name, source, branch, head };
  } catch (error) {
    if (attached) {
      try {
        lockArea(root, config, name, { force: true });
      } catch {
        // best effort cleanup
      }
    }
    fs.rmSync(area.image, { recursive: true, force: true });
    throw error;
  }
}
