import assert from "node:assert/strict";
import test from "node:test";
import { getToolPluginMetadata } from "openclaw/plugin-sdk/tool-plugin";
import entry from "../index.js";

test("declares OpenClaw tool-plugin metadata", () => {
  const metadata = getToolPluginMetadata(entry);
  assert.equal(metadata?.id, "openclaw-lancedb-knowledge-local");
  assert.deepEqual(metadata?.tools.map((tool) => tool.name), ["local_knowledge_search"]);
  assert.equal(metadata?.activation.onStartup, true);
});
