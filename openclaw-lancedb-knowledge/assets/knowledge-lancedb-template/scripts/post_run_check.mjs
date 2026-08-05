#!/usr/bin/env node
import fs from "node:fs/promises";
import path from "node:path";

const root = path.resolve(new URL("..", import.meta.url).pathname);
const checks = [];

function record(name, ok, detail = "") {
  checks.push({ name, ok, detail });
}

async function exists(rel) {
  try {
    await fs.access(path.join(root, rel));
    return true;
  } catch {
    return false;
  }
}

async function readJson(rel) {
  return JSON.parse(await fs.readFile(path.join(root, rel), "utf8"));
}

record("required files exist", await exists("src/cli.js") && await exists("src/security.js") && await exists("src/metadata.js") && await exists("src/enrichment.js") && await exists("config/source-map.example.json"));

const pkg = await readJson("package.json");
record("package exposes post-run check", pkg.scripts?.["postrun:check"] === "node scripts/post_run_check.mjs");
record("core scripts present", ["scan", "index", "search", "status", "test", "incremental", "sync-state", "compact-cache", "enrich:prepare", "enrich:validate", "benchmark", "profile"].every((key) => pkg.scripts?.[key]));

const sourceMap = await readJson("config/source-map.example.json");
record("source map has sources", Array.isArray(sourceMap.sources) && sourceMap.sources.length > 0);
record("source map excludes common secret paths", JSON.stringify(sourceMap).includes("secret") && JSON.stringify(sourceMap).includes(".env"));
record("AI enrichment is opt-in", sourceMap.enrichment?.enabled === false);

const benchmark = await readJson("config/benchmark.example.json");
record("release benchmark scaffold has at least 20 cases", Array.isArray(benchmark.cases) && benchmark.cases.length >= 20);
record("enrichment contract exists", await exists("config/enrichment-contract.md"));

const wrapper = await fs.readFile(path.join(root, "scripts/knowledge_index_incremental.sh"), "utf8");
record("incremental wrapper uses lock", wrapper.includes("index.lock") && wrapper.includes("mkdir \"$LOCK_DIR\""));
record("incremental wrapper rotates reports", wrapper.includes("rotate_reports"));

const testDir = path.join(root, "test");
const testFiles = (await fs.readdir(testDir)).filter((name) => name.endsWith(".test.js"));
record("test suite is present", pkg.scripts?.test === "node --test" && testFiles.length >= 8, `${testFiles.length} test files`);
record("post-run check does not execute commands", true, "run npm test explicitly before this check");

const failed = checks.filter((check) => !check.ok);
for (const check of checks) {
  console.log(`${check.ok ? "PASS" : "FAIL"} ${check.name}${check.detail ? ` - ${check.detail}` : ""}`);
}

if (failed.length) {
  console.error(`post-run check failed: ${failed.length} issue(s)`);
  process.exit(1);
}

console.log("post-run check passed");
