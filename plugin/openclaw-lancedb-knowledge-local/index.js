import { createLocalKnowledgeSearchTool } from "./src/search-tool.js";

export default {
  id: "openclaw-lancedb-knowledge-local",
  name: "OpenClaw Local Knowledge Search",
  register(api) {
    api.registerTool(() => createLocalKnowledgeSearchTool(api.pluginConfig || {}), {
      name: "local_knowledge_search",
    });
  },
};
