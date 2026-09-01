# Source Map Patterns

Use `config/source-map.json` to keep indexing explicit and client-safe.

## Project labels and overlapping sources

- A source's `project` value is authoritative: every file the source claims gets that label. Filename heuristics run only for sources without a `project`, and they match the file's relative path only, so prefer setting an explicit `project` on every source.
- When two sources overlap (same or nested roots), the source whose include pattern is most specific claims the file: an exact relative path beats a single-level wildcard (`*.md`), which beats a recursive wildcard (`**/*.md`). Ties go to the source listed earlier in the config. Each file is indexed exactly once.
- Use this to carve special files out of a broad source: list the broad `**/*.md` source, then add a narrower source with explicit include paths and a different `project`/`sourceType` for the files that need distinct attribution.

## Minimum OpenClaw setup

```json
{
  "id": "workspace-memory",
  "project": "OpenClawOps",
  "sourceType": "memory",
  "root": "/path/to/.openclaw/workspace/memory",
  "include": ["**/*.md"],
  "exclude": ["**/*.bak*", "**/*secret*", "**/*token*"],
  "priority": 0
}
```

## Discord / channel backup summaries

Prefer summaries before raw messages.

```json
{
  "id": "discord-backup-summary",
  "project": "DiscordBackups",
  "sourceType": "backup_summary",
  "root": "/path/to/頻道紀錄",
  "include": ["**/summary/**/*.md", "**/*摘要*.md", "**/*summary*.md"],
  "exclude": ["**/raw/**", "**/legacy/**", "**/legacy_docs/**"],
  "priority": 0
}
```

## Discord raw history (explicit opt-in)

Use this only when users need exact historical statements, examples, or message IDs that summaries cannot preserve. Confirm privacy and storage impact first. The bootstrap helper adds this source with `--include-discord-raw`.

```json
{
  "id": "discord-backup-raw",
  "project": "DiscordBackups",
  "sourceType": "discord_raw",
  "root": "/path/to/頻道紀錄",
  "include": ["**/raw/**/*.md"],
  "exclude": ["**/summary/**", "**/legacy/**", "**/legacy_docs/**", "**/.env*", "**/*secret*", "**/*token*"],
  "priority": 1
}
```

After indexing, run `npm run audit` and require `sourceTypes.discord_raw > 0` when raw history is expected. This validates that current raw chunks and deterministic metadata exactly match the LanceDB rows.

## Project docs / handoff files

```json
{
  "id": "client-project-docs",
  "project": "ClientProject",
  "sourceType": "project_doc",
  "root": "/path/to/client/project",
  "include": ["*.md", "session_summaries/**/*.md", "docs/**/*.md", "handoff/**/*.md"],
  "exclude": ["**/node_modules/**", "**/build/**", "**/.git/**", "**/dist/**"],
  "priority": 1
}
```

## Obsidian-style vault

```json
{
  "id": "obsidian-vault",
  "project": "ClientVault",
  "sourceType": "obsidian_note",
  "root": "/path/to/vault",
  "include": ["**/*.md"],
  "exclude": [".obsidian/**", "**/.trash/**", "**/templates/**", "**/*secret*", "**/*token*"],
  "priority": 1
}
```

## Code documentation only

Do not index whole code repositories by default. Start with README, schema, architecture, and handoff docs.

```json
{
  "id": "repo-docs",
  "project": "ClientApp",
  "sourceType": "code_doc",
  "root": "/path/to/repo",
  "include": ["README.md", "docs/**/*.md", "supabase/**/*.md", "architecture/**/*.md", "**/*handoff*.md"],
  "exclude": ["**/node_modules/**", "**/.git/**", "**/build/**", "**/dist/**"],
  "priority": 1
}
```

## Division of responsibilities with built-in OpenClaw memory search

If the platform already ships a built-in memory/vector search over agent memory files, avoid competing indexes over the same content:

- Let the built-in memory search own short-term session recall over the agent's own memory files; keep this knowledge base focused on curated summaries, project docs, handoff files, and vault content with explicit project labels and source-cited answers.
- Do not double-index the same raw files in both systems by default; if the built-in index already covers a root well, exclude it here or index only its summary layer.
- Route "what happened recently in this session/agent" questions to the built-in search, and "historical/cross-project/client documentation" questions to this knowledge base.
- If the built-in index is broken or paused, repair it (see the skill's memory index health section) instead of silently widening this knowledge base to compensate.

## Quality checklist

- Each source has stable `id`, useful `project`, and clear `sourceType`.
- `include` is narrow enough to avoid junk.
- `exclude` blocks credentials, generated files, dependencies, and media-heavy paths.
- Large raw chat/code sources are added only after summary/project docs prove insufficient.
