"""TencentDB Agent Memory Provider Plugin for Hermes Agent.

Provides bidirectional integration with TencentDB Agent Memory:
- prefetch(): Semantic recall before LLM completion
- sync_turn(): Non-blocking background ingest to L0 raw memory
- Built-in MCP tool handlers for conversation, skill, and wiki search
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

try:
    from agent.memory_provider import MemoryProvider, is_trivial_prompt
except ImportError:
    # Fallback for standalone / testing outside Hermes runtime
    class MemoryProvider:  # type: ignore
        pass

    def is_trivial_prompt(query: Optional[str]) -> bool:
        if not query:
            return True
        return query.strip().lower() in {"ok", "danke", "ja", "nein", "thanks", "hello", "hi"}


logger = logging.getLogger("hermes.plugins.memory.tencent")

DEFAULT_CORE_URL = "http://localhost:8420"
DEFAULT_IMPORT_URL = "http://localhost:8125"
DEFAULT_KNOWLEDGE_URL = "http://localhost:8424"
DEFAULT_USER_KEY = ""
DEFAULT_TEAM_ID = "default"


def _http_post(url: str, headers: dict, payload: dict, timeout: float = 3.0) -> tuple[int, Any]:
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body)
            except Exception:
                return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body
    except Exception as e:
        return 0, str(e)


class TencentMemoryProvider(MemoryProvider):
    """MemoryProvider implementation for TencentDB Agent Memory."""

    def __init__(self) -> None:
        self._core_url = DEFAULT_CORE_URL
        self._import_url = DEFAULT_IMPORT_URL
        self._knowledge_url = DEFAULT_KNOWLEDGE_URL
        self._user_key = DEFAULT_USER_KEY
        self._team_id = DEFAULT_TEAM_ID
        self._agent_id = "default-agent"
        self._session_id = ""
        self._worker: Optional[ThreadPoolExecutor] = None
        self._worker_lock = threading.Lock()
        self._warning_callback = None

    @property
    def name(self) -> str:
        return "tencent"

    def is_available(self) -> bool:
        cfg_key = self._user_key or os.environ.get("TDAI_USER_KEY", "")
        cfg_core = self._core_url or os.environ.get("TDAI_CORE_URL", "")
        return bool(cfg_key and cfg_core)

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "core_url", "description": "TencentDB Core REST URL (:8420)", "default": DEFAULT_CORE_URL},
            {"key": "import_url", "description": "TencentDB Web Panel / Import URL (:8125)", "default": DEFAULT_IMPORT_URL},
            {"key": "knowledge_url", "description": "TencentDB Knowledge/Wiki URL (:8424)", "default": DEFAULT_KNOWLEDGE_URL},
            {"key": "user_key", "description": "TencentDB User API Key (sk-mem-...)", "secret": True},
            {"key": "team_id", "description": "TencentDB Team / Namespace ID", "default": DEFAULT_TEAM_ID},
            {"key": "agent_id", "description": "TencentDB Agent ID (e.g. agt-xxx)"},
        ]

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._warning_callback = kwargs.get("warning_callback")

        hermes_home = kwargs.get("hermes_home") or ""
        profile_name = os.path.basename(str(hermes_home)) if "profiles" in str(hermes_home) else ""

        resolved_agent = profile_name or "default-agent"

        try:
            from hermes_cli.config import load_config
            cfg = load_config() or {}
            mcp_env = (cfg.get("mcp_servers") or {}).get("tencent_memory", {}).get("env") or {}
            mem_cfg = (cfg.get("memory") or {}).get("tencent", {}) or {}

            self._core_url = (
                mem_cfg.get("core_url")
                or mcp_env.get("TDAI_CORE_URL")
                or os.environ.get("TDAI_CORE_URL")
                or DEFAULT_CORE_URL
            ).rstrip("/")
            self._import_url = (
                mem_cfg.get("import_url")
                or os.environ.get("TDAI_IMPORT_URL")
                or DEFAULT_IMPORT_URL
            ).rstrip("/")
            self._knowledge_url = (
                mem_cfg.get("knowledge_url")
                or mcp_env.get("TDAI_KNOWLEDGE_URL")
                or os.environ.get("TDAI_KNOWLEDGE_URL")
                or DEFAULT_KNOWLEDGE_URL
            ).rstrip("/")
            self._user_key = (
                mem_cfg.get("user_key")
                or mcp_env.get("TDAI_USER_KEY")
                or os.environ.get("TDAI_USER_KEY")
                or DEFAULT_USER_KEY
            )
            self._team_id = (
                mem_cfg.get("team_id")
                or mcp_env.get("TDAI_TEAM_ID")
                or os.environ.get("TDAI_TEAM_ID")
                or DEFAULT_TEAM_ID
            )
            resolved_agent = (
                mem_cfg.get("agent_id")
                or mcp_env.get("TDAI_AGENT_ID")
                or os.environ.get("TDAI_AGENT_ID")
                or resolved_agent
            )
        except Exception:
            pass

        self._agent_id = resolved_agent

        with self._worker_lock:
            if self._worker is None:
                self._worker = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tdai-sync")

        logger.info(
            "TencentMemoryProvider initialized (agent_id=%s, team_id=%s, session_id=%s)",
            self._agent_id, self._team_id, self._session_id,
        )

    def system_prompt_block(self) -> str:
        return (
            "# TencentDB Agent Memory\n"
            "Active. Relevant conversation memories and skills from TencentDB are prefetched before each turn."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if is_trivial_prompt(query) or not self.is_available():
            return ""

        headers = {
            "Authorization": f"Bearer {self._user_key}",
            "x-tdai-service-id": self._team_id,
            "x-agent-id": self._agent_id,
            "Content-Type": "application/json",
        }

        recalled_sections: List[str] = []

        # 1. Search conversation memory (L0/L1)
        status, res = _http_post(
            f"{self._core_url}/v2/conversation/search",
            headers,
            {"query": query[:500], "limit": 3},
            timeout=2.0,
        )
        if status == 200 and isinstance(res, dict):
            messages = res.get("data", {}).get("messages", [])
            if messages:
                mem_lines = []
                for msg in messages:
                    content = str(msg.get("content", "")).strip()
                    score = msg.get("score", 0)
                    if content and score > 0.5:
                        short_content = content if len(content) <= 300 else content[:297] + "..."
                        mem_lines.append(f"- {short_content}")
                if mem_lines:
                    recalled_sections.append("### Relevante Gesprächserinnerungen (TencentDB):\n" + "\n".join(mem_lines))

        # 2. Search skills / knowledge
        s_status, s_res = _http_post(
            f"{self._core_url}/v3/skill/search",
            headers,
            {"query": query[:500], "limit": 2},
            timeout=2.0,
        )
        if s_status == 200 and isinstance(s_res, dict):
            items = s_res.get("data", {}).get("items", [])
            if items:
                skill_lines = []
                for item in items:
                    name = item.get("name", "")
                    desc = item.get("description", "")
                    snippet = item.get("snippet", "")
                    clean_snippet = re.sub(r"<[^>]+>", "", snippet).strip()
                    skill_lines.append(f"- **{name}**: {desc} ({clean_snippet[:200]})")
                if skill_lines:
                    recalled_sections.append("### Hinterlegte Skills & Dokumente (TencentDB):\n" + "\n".join(skill_lines))

        if not recalled_sections:
            return ""

        return (
            "<tencentdb-memory>\n"
            + "\n\n".join(recalled_sections)
            + "\n</tencentdb-memory>"
        )

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if not user_content.strip() or not assistant_content.strip() or not self.is_available():
            return

        target_session = session_id or self._session_id
        cleaned_user = re.sub(r"<tencentdb-memory>[\s\S]*?</tencentdb-memory>", "", user_content).strip()
        if not cleaned_user:
            cleaned_user = user_content.strip()

        payload = {
            "team_id": self._team_id,
            "agent_id": self._agent_id,
            "session_id": f"hermes-{target_session}" if target_session else "hermes-default",
            "messages": [
                {"role": "user", "content": cleaned_user},
                {"role": "assistant", "content": assistant_content.strip()},
            ],
        }

        headers = {
            "X-Tdai-User-Key": self._user_key,
            "X-Tdai-Service-Id": "default",
            "Content-Type": "application/json",
        }

        def _do_sync() -> None:
            try:
                status, res = _http_post(
                    f"{self._import_url}/api/v1/chat-memory/import",
                    headers,
                    payload,
                    timeout=5.0,
                )
                if status not in (200, 201):
                    logger.warning("TencentDB turn sync returned HTTP %s: %s", status, res)
                else:
                    logger.debug("TencentDB turn sync succeeded for session %s", target_session)
            except Exception as exc:
                logger.warning("TencentDB turn sync failed: %s", exc)

        with self._worker_lock:
            if self._worker:
                self._worker.submit(_do_sync)
            else:
                threading.Thread(target=_do_sync, daemon=True).start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "tdai_conversation_search",
                "description": "Durchsucht die Konversationshistorie und das Gesprächsgedächtnis des Agenten in TencentDB nach Begriffen oder Themen.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Suchbegriff oder Frage"},
                        "limit": {"type": "integer", "description": "Maximale Anzahl Ergebnisse", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "tdai_skill_search",
                "description": "Durchsucht im TencentDB Memory Hub hinterlegte Skills, Workflows und Best Practices.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Suchbegriff oder Name des Skills"},
                        "limit": {"type": "integer", "description": "Maximale Anzahl Ergebnisse", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "tdai_wiki_search",
                "description": "Durchsucht das Team-Wiki und Knowledge-Dokumente im TencentDB Memory Hub auf Port 8424.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Suchbegriff"},
                        "wiki_id": {"type": "string", "description": "Optionale Wiki-ID", "default": ""},
                        "limit": {"type": "integer", "description": "Maximale Anzahl", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "tdai_codegraph_search",
                "description": "Durchsucht den Code-Graph nach Symbolen, Funktionen und Abhängigkeiten (Impact Analysis).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Funktions- oder Symbolname"},
                        "limit": {"type": "integer", "description": "Maximale Anzahl", "default": 5},
                    },
                    "required": ["query"],
                },
            },
        ]

    def handle_tool_call(self, name: str, args: Dict[str, Any]) -> str:
        query = args.get("query", "")
        limit = args.get("limit", 5)

        headers_core = {
            "Authorization": f"Bearer {self._user_key}",
            "x-tdai-service-id": self._team_id,
            "x-agent-id": self._agent_id,
            "Content-Type": "application/json",
        }
        headers_ks = {
            "Authorization": f"Bearer {self._user_key}",
            "x-tdai-service-id": self._team_id,
            "Content-Type": "application/json",
        }

        if name == "tdai_conversation_search":
            status, res = _http_post(
                f"{self._core_url}/v2/conversation/search",
                headers_core,
                {"query": query, "limit": limit},
            )
            if status == 200 and isinstance(res, dict):
                messages = res.get("data", {}).get("messages", [])
                if not messages:
                    return f"Keine Konversationen zu '{query}' gefunden."
                return json.dumps(messages, ensure_ascii=False, indent=2)
            return f"Fehler bei Konversationssuche (HTTP {status}): {res}"

        elif name == "tdai_skill_search":
            status, res = _http_post(
                f"{self._core_url}/v3/skill/search",
                headers_core,
                {"query": query, "limit": limit},
            )
            if status == 200 and isinstance(res, dict):
                items = res.get("data", {}).get("items", []) if isinstance(res, dict) else []
                if not items:
                    return f"Keine Skills zu '{query}' gefunden."
                return json.dumps(items, ensure_ascii=False, indent=2)
            return f"Fehler bei Skill-Suche (HTTP {status}): {res}"

        elif name == "tdai_wiki_search":
            payload = {"team_id": self._team_id, "query": query, "limit": limit}
            if args.get("wiki_id"):
                payload["wiki_id"] = args["wiki_id"]
            status, res = _http_post(
                f"{self._knowledge_url}/v3/wiki/search",
                headers_ks,
                payload,
            )
            return json.dumps(res, ensure_ascii=False, indent=2) if isinstance(res, dict) else str(res)

        elif name == "tdai_codegraph_search":
            status, res = _http_post(
                f"{self._knowledge_url}/v3/code-graph/search",
                headers_ks,
                {"team_id": self._team_id, "query": query, "limit": limit},
            )
            return json.dumps(res, ensure_ascii=False, indent=2) if isinstance(res, dict) else str(res)

        return f"Unbekanntes Werkzeug: {name}"

    def shutdown(self) -> None:
        with self._worker_lock:
            if self._worker:
                self._worker.shutdown(wait=False)
                self._worker = None


def register(ctx: Any) -> None:
    ctx.register_memory_provider(TencentMemoryProvider())
