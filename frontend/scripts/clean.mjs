import { readdirSync, rmSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(frontendRoot, "..");
const everything = process.argv.includes("--all");

const BUILD_OUTPUTS = [
  join(frontendRoot, "dist"),
  join(repoRoot, "local_digest.egg-info"),
  join(repoRoot, ".pytest_cache"),
];
const GENERATED_DATA = [join(repoRoot, "reports")];
const DEPENDENCIES = [join(frontendRoot, "node_modules")];
const SKIPPED_DIRS = new Set(["node_modules", ".git", ".venv"]);

function findPycacheDirs(dir) {
  return readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && !SKIPPED_DIRS.has(entry.name))
    .flatMap((entry) => {
      const path = join(dir, entry.name);
      return entry.name === "__pycache__" ? [path] : findPycacheDirs(path);
    });
}

const targets = [
  ...BUILD_OUTPUTS,
  ...findPycacheDirs(repoRoot),
  ...(everything ? [...GENERATED_DATA, ...DEPENDENCIES] : []),
];

for (const target of targets) {
  rmSync(target, { recursive: true, force: true });
  console.log(`removed ${relative(repoRoot, target)}`);
}
