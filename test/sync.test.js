import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import {
  CONFIG_FILE,
  LOCAL_CONFIG_FILE,
  ageDecryptToFile,
  ageEncrypt,
  createSyncRemote,
  loadConfig,
  parseWebDavPropfind,
  syncArea,
} from "../src/index.js";
import { concept, read, run, tempWiki, write } from "./helpers.js";

const FAKE_RECIPIENT = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p";
const FAKE_IDENTITY = "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ8U2Y2W";

/** Creates a tiny mock `age` executable that XOR-envelopes payloads and records calls. */
function createMockAge(dir) {
  const logFile = path.join(dir, "age-calls.log");
  const script = path.join(dir, "mock-age");
  fs.writeFileSync(
    script,
    `#!/usr/bin/env node
import fs from "node:fs";
const args = process.argv.slice(2);
const logFile = ${JSON.stringify(logFile)};
const mode = args[0];
const input = fs.readFileSync(0);
if (mode === "-e") {
  const recipient = args[args.indexOf("-r") + 1];
  fs.appendFileSync(logFile, JSON.stringify({ op: "encrypt", recipient }) + "\\n");
  const xored = Buffer.from(input.map((b) => b ^ 0x5a));
  process.stdout.write(Buffer.concat([Buffer.from("MOCKAGE:" + recipient + "\\n"), xored]));
} else if (mode === "-d") {
  const idFile = args[args.indexOf("-i") + 1];
  const outFile = args[args.indexOf("-o") + 1];
  const stat = fs.statSync(idFile);
  const idText = fs.readFileSync(idFile, "utf8").trim();
  fs.appendFileSync(logFile, JSON.stringify({ op: "decrypt", idFile, mode: stat.mode & 0o777, idText }) + "\\n");
  if (!idText.startsWith("AGE-SECRET-KEY-")) {
    process.stderr.write("invalid identity\\n");
    process.exit(1);
  }
  const nl = input.indexOf(0x0a);
  if (nl === -1 || !input.subarray(0, nl).toString("utf8").startsWith("MOCKAGE:")) {
    process.stderr.write("not mockage ciphertext\\n");
    process.exit(1);
  }
  const plain = Buffer.from(input.subarray(nl + 1).map((b) => b ^ 0x5a));
  fs.writeFileSync(outFile, plain);
} else {
  process.stderr.write("unexpected args\\n");
  process.exit(2);
}
`,
    { mode: 0o755 },
  );
  return {
    ageBin: script,
    readCalls: () =>
      fs.existsSync(logFile)
        ? fs
            .readFileSync(logFile, "utf8")
            .trim()
            .split("\n")
            .filter(Boolean)
            .map((line) => JSON.parse(line))
        : [],
  };
}

/** Minimal in-process WebDAV server supporting MKCOL, PROPFIND, GET, PUT, MOVE, DELETE + Basic auth. */
async function startWebDavServer(t, { username, password }) {
  const storeDir = fs.mkdtempSync(path.join(os.tmpdir(), "memory-wiki-webdav-"));
  t.after(() => fs.rmSync(storeDir, { recursive: true, force: true }));
  const expectedAuth = `Basic ${Buffer.from(`${username}:${password}`, "utf8").toString("base64")}`;

  const resolveSafe = (urlPath) => {
    const clean = decodeURIComponent(urlPath.split("?")[0]).replace(/^\/+/, "");
    const abs = path.resolve(storeDir, clean);
    if (abs !== storeDir && !abs.startsWith(`${storeDir}${path.sep}`)) return null;
    return abs;
  };

  const server = http.createServer((req, res) => {
    if (req.headers.authorization !== expectedAuth) {
      res.writeHead(401, { "WWW-Authenticate": 'Basic realm="test"' });
      res.end("Unauthorized");
      return;
    }
    const target = resolveSafe(req.url || "/");
    if (!target) {
      res.writeHead(403);
      res.end("Forbidden");
      return;
    }

    if (req.method === "MKCOL") {
      if (fs.existsSync(target)) {
        res.writeHead(405);
        res.end();
        return;
      }
      if (!fs.existsSync(path.dirname(target))) {
        res.writeHead(409);
        res.end();
        return;
      }
      fs.mkdirSync(target);
      res.writeHead(201);
      res.end();
      return;
    }

    if (req.method === "PROPFIND") {
      if (!fs.existsSync(target)) {
        res.writeHead(404);
        res.end();
        return;
      }
      const basePath = req.url.endsWith("/") ? req.url : `${req.url}/`;
      const entries = fs.readdirSync(target);
      const responses = [
        `<D:response><D:href>${basePath}</D:href><D:propstat><D:prop/></D:propstat></D:response>`,
      ];
      for (const name of entries) {
        const stat = fs.statSync(path.join(target, name));
        if (!stat.isFile()) continue;
        const etag = `"${stat.size}-${Math.trunc(stat.mtimeMs)}"`;
        responses.push(
          `<D:response><D:href>${basePath}${encodeURIComponent(name)}</D:href>` +
            `<D:propstat><D:prop><D:getetag>${etag}</D:getetag>` +
            `<D:getlastmodified>${stat.mtime.toUTCString()}</D:getlastmodified></D:prop></D:propstat></D:response>`,
        );
      }
      const xml = `<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">${responses.join("")}</D:multistatus>`;
      res.writeHead(207, { "Content-Type": "application/xml; charset=utf-8" });
      res.end(xml);
      return;
    }

    if (req.method === "GET") {
      if (!fs.existsSync(target) || !fs.statSync(target).isFile()) {
        res.writeHead(404);
        res.end();
        return;
      }
      res.writeHead(200, { "Content-Type": "application/octet-stream" });
      res.end(fs.readFileSync(target));
      return;
    }

    if (req.method === "PUT") {
      const chunks = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        fs.mkdirSync(path.dirname(target), { recursive: true });
        fs.writeFileSync(target, Buffer.concat(chunks));
        res.writeHead(201);
        res.end();
      });
      return;
    }

    if (req.method === "MOVE") {
      const destHeader = req.headers.destination;
      const destUrl = new URL(Array.isArray(destHeader) ? destHeader[0] : destHeader);
      const destPath = resolveSafe(destUrl.pathname);
      if (!destPath || !fs.existsSync(target)) {
        res.writeHead(404);
        res.end();
        return;
      }
      fs.renameSync(target, destPath);
      res.writeHead(204);
      res.end();
      return;
    }

    if (req.method === "DELETE") {
      fs.rmSync(target, { force: true });
      res.writeHead(204);
      res.end();
      return;
    }

    res.writeHead(405);
    res.end();
  });

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => server.close());
  const { port } = server.address();
  return { url: `http://127.0.0.1:${port}/memory-wiki`, storeDir };
}

function initAreaDir(root, name) {
  const dir = path.join(root, name);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, ".metadata_never_index"), "");
  fs.writeFileSync(path.join(dir, ".gitignore"), ".DS_Store\n");
  fs.writeFileSync(path.join(dir, ".gitattributes"), "log.md merge=union\n");
  fs.writeFileSync(path.join(dir, "log.md"), "# Update Log\n\n## 2026-11-21\n\n* **Init**: Created.\n");
  run("git", ["-C", dir, "init", "--quiet", "-b", "main"]);
  run("git", ["-C", dir, "config", "guard.exempt", "true"]);
  run("git", ["-C", dir, "add", "--all"]);
  run("git", ["-C", dir, "commit", "--quiet", "-m", "wiki: init area"]);
  return dir;
}

test("loadConfig overlays .memory-wiki.local.yml and rejects inline secrets", (t) => {
  const { root } = tempWiki(t, {});
  write(
    root,
    CONFIG_FILE,
    `human: human:sol
areas:
  personal:
    passphrase: [printf, "%s", disk-secret]
    age_recipient: ${FAKE_RECIPIENT}
    age_identity: [printf, "%s", "${FAKE_IDENTITY}"]
`,
  );
  write(
    root,
    LOCAL_CONFIG_FILE,
    `sync:
  remote: https://dsm.example.ts.net/memory-wiki
  username: memory-wiki
  password: [printf, "%s", webdav-pass]
  host: hyperion
areas:
  personal:
    sync:
      host: hyperion-override
`,
  );

  const config = loadConfig(root);
  assert.deepEqual(config.areas.personal.ageRecipients, [FAKE_RECIPIENT]);
  assert.equal(config.sync.remote, "https://dsm.example.ts.net/memory-wiki");
  assert.equal(config.sync.host, "hyperion");
  assert.equal(config.areas.personal.sync.remote, "https://dsm.example.ts.net/memory-wiki");
  assert.equal(config.areas.personal.sync.host, "hyperion-override");

  // Rejects plaintext string password in local config.
  write(root, LOCAL_CONFIG_FILE, 'sync:\n  remote: https://dsm.example.ts.net/w\n  password: "plain"\n');
  assert.throws(() => loadConfig(root), /never store secrets inline/);

  // Rejects non-loopback plain http:// WebDAV remote.
  assert.throws(
    () => createSyncRemote(root, { remote: "http://nas.example.ts.net/wiki" }),
    /WebDAV sync requires https:\/\//,
  );
});

test("parseWebDavPropfind handles Synology D: XML and ignores temp files", () => {
  const xml = `<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/memory-wiki/personal/</D:href>
    <D:propstat><D:prop/></D:propstat>
  </D:response>
  <D:response>
    <D:href>/memory-wiki/personal/hyperion.bundle.age</D:href>
    <D:propstat><D:prop>
      <D:getetag>&quot;abc-123&quot;</D:getetag>
      <D:getlastmodified>Wed, 25 Nov 2026 10:00:00 GMT</D:getlastmodified>
    </D:prop></D:propstat>
  </D:response>
  <D:response>
    <D:href>/memory-wiki/personal/sigil.bundle.age.tmp.999</D:href>
    <D:propstat><D:prop><D:getetag>"tmp"</D:getetag></D:prop></D:propstat>
  </D:response>
</D:multistatus>`;
  assert.deepEqual(parseWebDavPropfind(xml), [
    {
      name: "hyperion.bundle.age",
      host: "hyperion",
      etag: '"abc-123"',
      lastModified: "Wed, 25 Nov 2026 10:00:00 GMT",
    },
  ]);
});

test("multi-Mac WebDAV sync encrypts bundles, carries binary attachments, auto-merges offline edits, and skips unchanged peers", async (t) => {
  const dav = await startWebDavServer(t, { username: "wiki-sync", password: "dav-secret" });
  const binDir = fs.mkdtempSync(path.join(os.tmpdir(), "memory-wiki-mockage-"));
  t.after(() => fs.rmSync(binDir, { recursive: true, force: true }));
  const { ageBin, readCalls } = createMockAge(binDir);

  const areaConfig = {
    personal: {
      passphrase: ["printf", "%s", "disk-pass"],
      age_recipient: FAKE_RECIPIENT,
      age_identity: ["printf", "%s", FAKE_IDENTITY],
      sync: {
        remote: dav.url,
        username: "wiki-sync",
        password: ["printf", "%s", "dav-secret"],
      },
    },
  };

  const commitConcept = (root, slug, title, description) => {
    const dir = path.join(root, "personal");
    write(root, `personal/concepts/${slug}.md`, concept(`type: Concept\ntitle: ${title}\ndescription: ${description}`));
    const logPath = path.join(dir, "log.md");
    fs.appendFileSync(logPath, `* Added ${title}.\n`);
    run("git", ["-C", dir, "add", "--all"]);
    run("git", ["-C", dir, "commit", "--quiet", "-m", `wiki: add ${slug}`]);
  };

  // Host 1 (hyperion) initializes the personal area and commits an entry + binary PNG.
  const mac1 = tempWiki(t, { "index.md": '---\nokf_version: "0.3"\n---\n' }, { areas: areaConfig });
  const dir1 = initAreaDir(mac1.root, "personal");
  commitConcept(mac1.root, "project-atlas", "Project Atlas", "Private codename from hyperion.");
  const fakePng = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4]);
  fs.mkdirSync(path.join(dir1, "assets"), { recursive: true });
  fs.writeFileSync(path.join(dir1, "assets", "diagram.png"), fakePng);
  run("git", ["-C", dir1, "add", "assets/diagram.png"]);
  run("git", ["-C", dir1, "commit", "--quiet", "-m", "Add diagram"]);

  // Sync refuses uncommitted attachments so nothing is silently skipped.
  fs.writeFileSync(path.join(dir1, "assets", "untracked.pdf"), "PDF bytes");
  await assert.rejects(
    syncArea(mac1.root, mac1.config, "personal", { host: "hyperion", ageBin }),
    /uncommitted changes.*untracked\.pdf/,
  );
  fs.rmSync(path.join(dir1, "assets", "untracked.pdf"));

  // MEMORY_WIKI_DEFAULT_ONLY refuses syncArea.
  await assert.rejects(
    syncArea(mac1.root, mac1.config, "personal", {
      host: "hyperion",
      ageBin,
      env: { MEMORY_WIKI_DEFAULT_ONLY: "1" },
    }),
    /MEMORY_WIKI_DEFAULT_ONLY/,
  );

  // Hyperion pushes encrypted bundle to WebDAV (no decrypt / no Touch ID needed).
  const push1 = await syncArea(mac1.root, mac1.config, "personal", { host: "hyperion", ageBin });
  assert.equal(push1.pushed, true);
  assert.equal(push1.host, "hyperion");
  const uploaded = fs.readFileSync(path.join(dav.storeDir, "memory-wiki", "personal", "hyperion.bundle.age"));
  assert.equal(uploaded.includes(Buffer.from("Project Atlas")), false, "plaintext must not appear on NAS");
  assert.deepEqual(
    readCalls().map((c) => c.op),
    ["encrypt"],
    "first push only encrypts to public recipient",
  );

  // Second sync on hyperion with no changes does ZERO encrypt/decrypt work.
  const noop = await syncArea(mac1.root, mac1.config, "personal", { host: "hyperion", ageBin });
  assert.equal(noop.pushed, false);
  assert.equal(readCalls().length, 1);

  // Host 2 (sigil) starts from hyperion's initial history (as restoreArea creates), then syncs.
  const mac2 = tempWiki(t, { "index.md": '---\nokf_version: "0.3"\n---\n' }, { areas: areaConfig });
  const dir2 = path.join(mac2.root, "personal");
  run("git", ["clone", "--quiet", dir1, dir2]);
  run("git", ["-C", dir2, "remote", "remove", "origin"]);
  run("git", ["-C", dir2, "config", "guard.exempt", "true"]);

  // Both Macs now edit OFFLINE on the same day (different concepts + same-day log.md!).
  commitConcept(mac1.root, "hyperion-note", "Hyperion Note", "Written offline on hyperion.");
  commitConcept(mac2.root, "sigil-note", "Sigil Note", "Written offline on sigil.");

  // Hyperion pushes its offline commit, then Sigil syncs -> auto-merges log.md via merge=union!
  await syncArea(mac1.root, mac1.config, "personal", { host: "hyperion", ageBin });
  const sigilSync = await syncArea(mac2.root, mac2.config, "personal", { host: "sigil", ageBin });
  assert.equal(sigilSync.pushed, true);
  assert.equal(sigilSync.merged.length, 1);
  assert.equal(sigilSync.merged[0].peer, "hyperion");
  assert.ok(fs.existsSync(path.join(dir2, "concepts", "hyperion-note.md")));
  assert.ok(fs.existsSync(path.join(dir2, "concepts", "sigil-note.md")));
  assert.deepEqual(fs.readFileSync(path.join(dir2, "assets", "diagram.png")), fakePng);
  const sigilLog = read(mac2.root, "personal/log.md");
  assert.match(sigilLog, /Hyperion Note/);
  assert.match(sigilLog, /Sigil Note/);

  // Verify the temporary age identity file was created 0600 inside the encrypted area (.git), NOT /tmp.
  const decryptCall = readCalls().find((c) => c.op === "decrypt");
  assert.ok(decryptCall, "expected age decrypt call");
  assert.equal(decryptCall.mode, 0o600);
  assert.ok(
    decryptCall.idFile.startsWith(path.join(dir2, ".git") + path.sep),
    `identity temp file must stay inside mounted encrypted area, got ${decryptCall.idFile}`,
  );
  assert.equal(fs.existsSync(decryptCall.idFile), false, "identity temp file must be removed immediately");

  // Hyperion now pulls Sigil's merge and fast-forwards cleanly.
  const hyperionPull = await syncArea(mac1.root, mac1.config, "personal", { host: "hyperion", ageBin });
  assert.deepEqual(hyperionPull.merged, [{ peer: "sigil", mode: "fast-forward" }]);
  assert.ok(fs.existsSync(path.join(dir1, "concepts", "sigil-note.md")));

  // Real conflict on the same file stops cleanly and names the conflicted path.
  commitConcept(mac1.root, "project-atlas", "Project Atlas", "Edited on hyperion.");
  commitConcept(mac2.root, "project-atlas", "Project Atlas", "Edited differently on sigil.");
  await syncArea(mac1.root, mac1.config, "personal", { host: "hyperion", ageBin });
  await assert.rejects(
    syncArea(mac2.root, mac2.config, "personal", { host: "sigil", ageBin }),
    /merge conflicts.*concepts\/project-atlas\.md/,
  );
});

const hasRealAge =
  spawnSync("sh", ["-c", "command -v age && command -v age-keygen"], { encoding: "utf8" }).status === 0;

test("real age encrypt and decrypt round-trip works end to end", { skip: !hasRealAge }, async (t) => {
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "memory-wiki-real-age-"));
  t.after(() => fs.rmSync(workDir, { recursive: true, force: true }));
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

  const payload = Buffer.from("secret bundle payload\0\x01\x02", "binary");
  const cipher = await ageEncrypt(payload, [recipient]);
  assert.match(cipher.subarray(0, 32).toString("utf8"), /^age-encryption\.org\/v1/);
  const outFile = path.join(workDir, "out.bin");
  await ageDecryptToFile(cipher, identity, outFile, { workDir });
  assert.deepEqual(fs.readFileSync(outFile), payload);
});
