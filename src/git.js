import { spawnSync } from "node:child_process";

/**
 * The arguments that start one of the tool's own git commands in `dir`.
 *
 * Its commits are never signed. They are made unattended, where a signing
 * program can wait on an approval nobody is there to give, and a signature
 * would say the user wrote them. Where Git has no author identity configured
 * they carry the tool's, so a change can always be recorded: who wrote or
 * confirmed a concept is in its frontmatter.
 */
export function gitIn(dir) {
  const args = ["-C", dir, "-c", "commit.gpgsign=false"];
  const email = spawnSync("git", ["-C", dir, "config", "user.email"], { encoding: "utf8" });
  if (email.status === 0 && email.stdout.trim()) return args;
  return [...args, "-c", "user.name=memory-wiki", "-c", "user.email=memory-wiki@localhost"];
}
