import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createLocalKnowledgeSearchTool, validateToolParams } from "../src/search-tool.js";

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "local-knowledge-plugin-"));
  const projectRoot = path.join(root, "knowledge-lancedb-qwen-local");
  fs.mkdirSync(path.join(projectRoot, "src"), { recursive: true });
  fs.writeFileSync(path.join(projectRoot, "package.json"), "{}\n");
  fs.writeFileSync(path.join(projectRoot, "src", "cli.js"), `
const query = process.argv[process.argv.indexOf('--query') + 1];
if (query === 'bad-json') process.stdout.write('not-json');
else if (query === 'too-much') process.stdout.write('x'.repeat(600000));
else process.stdout.write(JSON.stringify({status:'READY',providerIdentity:{provider:'qwen-local',model:'Qwen3',dimensions:768},results:[{summary:'answer',sourcePath:'/safe/source.md',chunkId:'c1',project:'Demo',title:'Title',heading:'Heading',rank:1}]}));
`);
  return {
    root,
    config: { projectRoot, nodePath: process.execPath, allowedProjects: ["Demo"], timeoutMs: 5000, maxOutputBytes: 262144 },
  };
}

test("closed input contract rejects injection-shaped project and unsafe limits", () => {
  assert.throws(() => validateToolParams({ query: "hello", project: "Demo' OR 1=1", limit: 5 }, ["Demo"]));
  assert.throws(() => validateToolParams({ query: "hello", limit: 11 }, []));
  assert.throws(() => validateToolParams({ query: "x".repeat(2001) }, []));
  assert.deepEqual(validateToolParams({ query: "hello", project: "Demo", limit: 3 }, ["Demo"]), {
    query: "hello", project: "Demo", limit: 3,
  });
});

test("tool returns validated source-cited Qwen results", async () => {
  const item = fixture();
  try {
    const tool = createLocalKnowledgeSearchTool(item.config);
    const payload = JSON.parse((await tool.execute("call-1", { query: "decision", limit: 5, project: "Demo" })).content[0].text);
    assert.equal(payload.status, "READY");
    assert.equal(payload.providerIdentity.provider, "qwen-local");
    assert.equal(payload.results[0].sourcePath, "/safe/source.md");
  } finally { fs.rmSync(item.root, { recursive: true, force: true }); }
});

test("tool fails closed on invalid or oversized child output", async () => {
  const item = fixture();
  try {
    const tool = createLocalKnowledgeSearchTool(item.config);
    const invalid = JSON.parse((await tool.execute("call-1", { query: "bad-json" })).content[0].text);
    const oversized = JSON.parse((await tool.execute("call-2", { query: "too-much" })).content[0].text);
    assert.equal(invalid.status, "SEARCH_INVALID_JSON");
    assert.equal(oversized.status, "SEARCH_OUTPUT_LIMIT");
    assert.deepEqual(invalid.results, []);
  } finally { fs.rmSync(item.root, { recursive: true, force: true }); }
});

test("invalid config never exposes local paths or child errors", async () => {
  const tool = createLocalKnowledgeSearchTool({ projectRoot: "/tmp/not-managed", nodePath: process.execPath });
  const payload = JSON.parse((await tool.execute("call", { query: "hello" })).content[0].text);
  assert.deepEqual(payload, { status: "CONFIG_INVALID", results: [] });
});
