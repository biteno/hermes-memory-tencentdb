# hermes-memory-tencentdb

**TencentDB Agent Memory Provider Plugin for Hermes Agent**

A high-performance, bidirectional memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent) connecting to [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory).

---

## Features

- **⚡ Automatic Prefetch Recall (`prefetch`):** Before each LLM completion, the user query is semantically searched against TencentDB Core (`:8420`) and relevant conversation turns and skills are seamlessly injected into the context window.
- **🔄 Non-blocking Turn Synchronization (`sync_turn`):** Completed conversation turns (`user` + `assistant`) are ingested into TencentDB L0 memory asynchronously in the background via a worker thread pool.
- **👥 Multi-Profile Support:** Dynamically scopes memory to the active Hermes profile or agent configuration.
- **🛠️ Built-in MCP Tools:** Exposes `tdai_conversation_search`, `tdai_skill_search`, `tdai_wiki_search`, and `tdai_codegraph_search` for manual retrieval.
- **🔒 Direct Upstream AI Hub Routing:** Keeps individual LiteLLM / provider API keys, virtual key tracking, and budget governance intact (no need to proxy all LLM traffic through a shared proxy key).

---

## Installation

### Method 1: Git Plugin (Hermes CLI)
```bash
hermes plugins install https://github.com/biteno/hermes-memory-tencentdb
hermes config set memory.provider tencent
```

### Method 2: Manual Directory Link
Copy or link the repository folder to your Hermes plugins directory:
```bash
# On Linux/macOS
ln -s /path/to/hermes-memory-tencentdb ~/.hermes/plugins/tencent

# On Windows
mklink /D "%USERPROFILE%\AppData\Local\hermes\plugins\tencent" "C:\path\to\hermes-memory-tencentdb"
```

---

## Configuration

In your Hermes profile's `config.yaml` (or `~/.hermes/config.yaml`):

```yaml
memory:
  provider: tencent
  memory_enabled: true
  tencent:
    core_url: http://localhost:8420
    import_url: http://localhost:8125
    knowledge_url: http://localhost:8424
    user_key: sk-mem-your-user-api-key
    team_id: team-your-team-id
    agent_id: agt-your-agent-id

mcp_servers:
  tencent_memory:
    command: python
    args:
      - scripts/tencent_memory_mcp.py
    env:
      TDAI_CORE_URL: http://localhost:8420
      TDAI_IMPORT_URL: http://localhost:8125
      TDAI_KNOWLEDGE_URL: http://localhost:8424
      TDAI_USER_KEY: sk-mem-your-user-api-key
      TDAI_TEAM_ID: team-your-team-id
      TDAI_AGENT_ID: agt-your-agent-id
```

### Environment Variables (Optional Overrides)

| Variable | Description | Default |
|---|---|---|
| `TDAI_CORE_URL` | TencentDB Core REST endpoint | `http://localhost:8420` |
| `TDAI_IMPORT_URL` | TencentDB Web Panel / Ingest endpoint | `http://localhost:8125` |
| `TDAI_KNOWLEDGE_URL` | TencentDB LLM-Wiki & CodeGraph endpoint | `http://localhost:8424` |
| `TDAI_USER_KEY` | User Personal Access Token | `sk-mem-...` |
| `TDAI_TEAM_ID` | Team / Workspace Namespace | `default` |
| `TDAI_AGENT_ID` | Agent Identifier | `default-agent` |

---

## License

MIT License. Copyright (c) 2026 Biteno GmbH.
