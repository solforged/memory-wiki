// Off-hours window guard, macOS AC-power check, and launchd LaunchAgent management.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const LAUNCH_AGENT_LABEL = "local.memory-wiki.dream";

const toMinutes = (hhmm) => {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
};

/** True when `now` (local timezone) falls inside `[start, end)` of `"HH:MM-HH:MM"`. */
export function inOffHoursWindow(windowStr, now = new Date()) {
  const [startStr, endStr] = String(windowStr).split("-");
  const start = toMinutes(startStr);
  const end = toMinutes(endStr);
  const cur = now.getHours() * 60 + now.getMinutes();
  return start === end ? true : start < end ? cur >= start && cur < end : cur >= start || cur < end;
}

/** True when on AC power or on a desktop/non-macOS host without a battery. */
export function checkAcPower(platform = process.platform) {
  if (platform !== "darwin") return { onAc: true, detail: "non-macOS" };
  const res = spawnSync("pmset", ["-g", "batt"], { encoding: "utf8" });
  if (res.status !== 0) return { onAc: true, detail: "pmset unavailable" };
  return /Battery Power/i.test(res.stdout)
    ? { onAc: false, detail: "Battery Power" }
    : { onAc: true, detail: /AC Power/i.test(res.stdout) ? "AC Power" : "no battery" };
}

const xmlEscape = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export const launchAgentPath = (home = os.homedir()) =>
  path.join(home, "Library", "LaunchAgents", `${LAUNCH_AGENT_LABEL}.plist`);

export function renderLaunchAgentPlist({ nodeBin, wikiBin, slots, logPath, pathEnv, wikiHome }) {
  const args = ["/usr/bin/caffeinate", "-i", nodeBin, wikiBin, "dream"];
  const intervals = slots
    .map((slot) => {
      const [h, m] = slot.split(":").map(Number);
      return `\t\t<dict>\n\t\t\t<key>Hour</key>\n\t\t\t<integer>${h}</integer>\n\t\t\t<key>Minute</key>\n\t\t\t<integer>${m}</integer>\n\t\t</dict>`;
    })
    .join("\n");
  const envXml = [
    ["PATH", pathEnv],
    ["MEMORY_WIKI_DEFAULT_ONLY", "1"],
    ...(wikiHome ? [["MEMORY_WIKI_HOME", wikiHome]] : []),
  ]
    .map(([k, v]) => `\t\t<key>${xmlEscape(k)}</key>\n\t\t<string>${xmlEscape(v)}</string>`)
    .join("\n");

  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>${LAUNCH_AGENT_LABEL}</string>
\t<key>ProgramArguments</key>
\t<array>
${args.map((a) => `\t\t<string>${xmlEscape(a)}</string>`).join("\n")}
\t</array>
\t<key>StartCalendarInterval</key>
\t<array>
${intervals}
\t</array>
\t<key>EnvironmentVariables</key>
\t<dict>
${envXml}
\t</dict>
\t<key>ProcessType</key>
\t<string>Background</string>
\t<key>LowPriorityIO</key>
\t<true/>
\t<key>Nice</key>
\t<integer>10</integer>
\t<key>StandardOutPath</key>
\t<string>${xmlEscape(logPath)}</string>
\t<key>StandardErrorPath</key>
\t<string>${xmlEscape(logPath)}</string>
</dict>
</plist>
`;
}

function stableNodeBin(fallback, home) {
  for (const p of [
    path.join(home, ".local", "share", "mise", "shims", "node"),
    path.join(home, ".local", "share", "aube", "shims", "node"),
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
  ]) {
    if (fs.existsSync(p)) return p;
  }
  return fallback;
}

export function installLaunchAgent({ nodeBin, wikiBin, slots, cacheDir, wikiHome = null, home = os.homedir() }) {
  if (process.platform !== "darwin") throw new Error("launchd scheduling is macOS-only");
  const resolvedNode = nodeBin ?? stableNodeBin(process.execPath, home);
  const plistPath = launchAgentPath(home);
  const logPath = path.join(cacheDir, "launchd.log");
  fs.mkdirSync(path.dirname(plistPath), { recursive: true });
  fs.mkdirSync(cacheDir, { recursive: true });
  const pathEnv = [
    ...new Set([
      path.join(home, ".local", "bin"),
      path.join(home, ".local", "share", "mise", "shims"),
      path.join(home, ".local", "share", "aube", "shims"),
      path.dirname(resolvedNode),
      "/opt/homebrew/bin",
      "/usr/local/bin",
      "/usr/bin",
      "/bin",
    ]),
  ].join(":");
  fs.writeFileSync(plistPath, renderLaunchAgentPlist({ nodeBin: resolvedNode, wikiBin, slots, logPath, pathEnv, wikiHome }));

  const domain = `gui/${process.getuid()}`;
  spawnSync("launchctl", ["bootout", `${domain}/${LAUNCH_AGENT_LABEL}`], { encoding: "utf8" });
  const res = spawnSync("launchctl", ["bootstrap", domain, plistPath], { encoding: "utf8" });
  if (res.status !== 0) throw new Error(`launchctl bootstrap failed: ${(res.stderr || res.stdout).trim()}`);
  return { label: LAUNCH_AGENT_LABEL, plistPath, logPath, slots };
}

export function uninstallLaunchAgent({ home = os.homedir() } = {}) {
  if (process.platform !== "darwin") throw new Error("launchd scheduling is macOS-only");
  const plistPath = launchAgentPath(home);
  spawnSync("launchctl", ["bootout", `gui/${process.getuid()}/${LAUNCH_AGENT_LABEL}`], { encoding: "utf8" });
  const removed = fs.existsSync(plistPath);
  if (removed) fs.rmSync(plistPath, { force: true });
  return { label: LAUNCH_AGENT_LABEL, plistPath, removed };
}

export function launchAgentStatus({ home = os.homedir() } = {}) {
  const plistPath = launchAgentPath(home);
  const installed = fs.existsSync(plistPath);
  if (process.platform !== "darwin") return { label: LAUNCH_AGENT_LABEL, plistPath, installed, loaded: false };
  const res = spawnSync("launchctl", ["print", `gui/${process.getuid()}/${LAUNCH_AGENT_LABEL}`], { encoding: "utf8" });
  return { label: LAUNCH_AGENT_LABEL, plistPath, installed, loaded: res.status === 0 };
}
