#!/usr/bin/env node

const payload = {
  status: "READY",
  providerIdentity: {
    provider: "qwen-local",
    model: "Qwen3-Embedding-4B-Q5_K_M",
    dimensions: 768,
  },
  results: [{
    summary: "隔離驗收代號是 ORCHID-742；此內容只用於 OpenClaw Profile 整合測試。",
    sourcePath: "tests/fixtures/openclaw-profile-recall.md",
    chunkId: "fixture-orchid-742",
    project: "integration-fixture",
    title: "OpenClaw 地端整合驗收資料",
    heading: "主動召回測試",
    rank: 1,
  }],
};

if (process.argv[2] !== "search-json") {
  process.stderr.write("fixture only supports search-json\n");
  process.exit(2);
}
process.stdout.write(`${JSON.stringify(payload)}\n`);
