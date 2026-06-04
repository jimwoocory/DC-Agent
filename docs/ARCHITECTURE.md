# Architecture

DC-Agent is based on AstrBot and extends it with company-local agent, knowledge-base, NAS, Feishu, dashboard, and harness capabilities.

## Primary Areas

| Area | Paths | Notes |
|---|---|---|
| Core lifecycle | `main.py`, `astrbot/core/core_lifecycle.py` | Starts services and initializes managers |
| Knowledge base | `astrbot/core/knowledge_base/` | Parses, chunks, stores, and retrieves documents |
| KB dashboard API | `astrbot/dashboard/routes/knowledge_base.py` | Upload, import, progress, retrieve endpoints |
| Agent KB tools | `astrbot/core/tools/knowledge_base_tools.py` | Runtime retrieval surface for agents |
| Harness | `harness/`, `tests/harness/` | Agent workflow support and evaluators |
| NAS sync | `nas_sync/` | Local document discovery and sync utilities |
| Dashboard | `dashboard/` | Vue-based management UI |

## Knowledge-Base Flow

```text
source file
  -> parser
  -> chunks
  -> document metadata
  -> sparse retriever and vector store
  -> retrieval result
  -> agent answer with source context
```

## Runtime Data Rule

Runtime data under `data/knowledge_base`, `data/temp`, `data/output`, logs, and local configs must not be committed.
