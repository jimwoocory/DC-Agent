import json
import sqlite3

from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.runtime_context.assembler import RuntimeContextAssembler
from astrbot.core.runtime_context.models import (
    RUNTIME_CONTEXT_SECTIONS_EXTRA_KEY,
    RuntimeContextSection,
)
from data.plugins.dc_router import memory_injection


class _Event:
    def __init__(self, text: str) -> None:
        self.message_str = text
        self.extras: dict[str, object] = {}

    def get_platform_id(self) -> str:
        return memory_injection.BUSINESS_PLATFORM_ID

    def get_extra(self, key: str):
        return self.extras.get(key)

    def set_extra(self, key: str, value: object) -> None:
        self.extras[key] = value


def test_new_creative_work_does_not_trigger_historical_memory() -> None:
    assert memory_injection._should_retrieve("帮我搭建一个新的活动方案框架") is False
    assert memory_injection._should_retrieve("帮我生成一张五菱夏至海报") is False
    assert memory_injection._should_retrieve("查一下之前五菱活动方案") is True


def test_new_quotation_work_triggers_historical_memory() -> None:
    assert memory_injection._should_retrieve("做一个柳州100人活动三档报价") is True
    assert memory_injection._should_retrieve("请按项目物料清单测算成本") is True
    assert memory_injection.QUOTATION_REQUEST_RE.search("查聚美广告历史报价")
    assert memory_injection.PRICE_QUERY_RE.search("聚美UV报价是多少")


def test_quotation_retrieval_combines_governed_and_nas_memory(
    monkeypatch, tmp_path
) -> None:
    db_path = tmp_path / "nas_memory.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE project_items (
                project_name TEXT,
                project_type TEXT,
                project_status TEXT,
                owner TEXT,
                owner_department TEXT,
                source_rel_path TEXT,
                evidence_json TEXT,
                updated_at TEXT
            )
            """
        )
    monkeypatch.setattr(memory_injection, "NAS_MEMORY_DB", db_path)
    monkeypatch.setattr(
        memory_injection,
        "retrieve_governed_memory_context",
        lambda _text, limit: {
            "governed_memories": [{"title": "柳州历史活动复盘"}],
            "documents": [],
            "project_items": [],
        },
    )
    monkeypatch.setattr(memory_injection, "query_terms", lambda _text: ["柳州"])
    monkeypatch.setattr(
        memory_injection,
        "fetch_fts_rows",
        lambda _conn, _text, _limit: [{"title": "聚美广告"}],
    )
    monkeypatch.setattr(
        memory_injection, "fetch_like_rows", lambda _conn, _terms, _limit: []
    )
    monkeypatch.setattr(
        memory_injection,
        "dedupe_query_rows",
        lambda rows, _terms, _text, limit: rows[:limit],
    )

    context = memory_injection.retrieve_memory_context("做一个柳州100人活动三档报价")

    assert context["quotation_mode"] is True
    assert context["governed_memories"] == [{"title": "柳州历史活动复盘"}]
    assert context["documents"] == [{"title": "聚美广告"}]


def test_company_fact_retrieval_combines_governed_and_nas_evidence(
    monkeypatch, tmp_path
) -> None:
    db_path = tmp_path / "nas_memory.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE project_items (
                project_name TEXT,
                project_type TEXT,
                project_status TEXT,
                owner TEXT,
                owner_department TEXT,
                source_rel_path TEXT,
                evidence_json TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO project_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "五菱中台项目",
                "客户运营",
                "进行中",
                "罗经理",
                "中台",
                "项目台账/五菱中台项目.md",
                "{}",
                "2026-07-20T00:00:00Z",
            ),
        )
    monkeypatch.setattr(memory_injection, "NAS_MEMORY_DB", db_path)
    monkeypatch.setattr(
        memory_injection,
        "retrieve_governed_memory_context",
        lambda _text, limit: {
            "governed_memories": [
                {
                    "title": "五菱客户治理结论",
                    "review_status": "approved",
                }
            ],
            "documents": [],
            "project_items": [],
        },
    )
    monkeypatch.setattr(memory_injection, "query_terms", lambda _text: ["五菱"])
    monkeypatch.setattr(
        memory_injection,
        "retrieve_obsidian_bridge_context",
        lambda _text, limit: [
            {
                "kind": "entity",
                "name": "五菱",
                "path": "20_Bridges/Entities/实体-五菱.md",
                "content": "# 五菱\n类型：产品线",
            }
        ],
    )
    monkeypatch.setattr(
        memory_injection,
        "fetch_fts_rows",
        lambda _conn, _text, _limit: [
            {
                "title": "五菱项目执行方案",
                "review_status": "need_review",
                "rel_path": "项目资料/五菱项目执行方案.md",
            }
        ],
    )
    monkeypatch.setattr(
        memory_injection, "fetch_like_rows", lambda _conn, _terms, _limit: []
    )
    monkeypatch.setattr(
        memory_injection,
        "dedupe_query_rows",
        lambda rows, _terms, _text, limit: rows[:limit],
    )

    context = memory_injection.retrieve_memory_context("查一下五菱中台项目负责人")

    assert context["governed_memories"] == [
        {"title": "五菱客户治理结论", "review_status": "approved"}
    ]
    assert context["project_items"][0]["project_name"] == "五菱中台项目"
    assert context["documents"] == [
        {
            "title": "五菱项目执行方案",
            "review_status": "need_review",
            "rel_path": "项目资料/五菱项目执行方案.md",
        }
    ]
    assert context["obsidian_bridges"][0]["name"] == "五菱"


def test_obsidian_bridge_context_resolves_entity_department_and_executive_alias(
    monkeypatch, tmp_path
) -> None:
    vault = tmp_path / "ObsidianVault"
    entity_dir = vault / "20_Bridges" / "Entities"
    department_dir = vault / "20_Bridges" / "Departments"
    people_dir = vault / "20_Bridges" / "People"
    entity_dir.mkdir(parents=True)
    department_dir.mkdir(parents=True)
    people_dir.mkdir(parents=True)
    (entity_dir / "实体-五菱.md").write_text(
        "# 五菱\n\n类型：产品线\n\n## 项目 Top\n- [[五菱用户运营]]：12\n",
        encoding="utf-8",
    )
    (department_dir / "中台部门.md").write_text(
        "# 中台部门\n\n类型：部门\n\n## 关联文档\n- [[项目管理总表]]\n",
        encoding="utf-8",
    )
    (people_dir / "罗鸣.md").write_text(
        "# 罗鸣\n\n类型：人员\n\n## 关联文档\n"
        "- [[五菱项目]]\n- [[五菱项目]]\n\n"
        "## 关联 RawRefs\n- [[不应注入的明细]]\n",
        encoding="utf-8",
    )
    org_config = tmp_path / "company_org_structure.json"
    org_config.write_text(
        json.dumps(
            {
                "people": {
                    "罗鸣": {
                        "department": "客户部",
                        "role": "客户部总监",
                        "office": "职能部",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_injection, "OBSIDIAN_VAULT_ROOT", vault)
    monkeypatch.setattr(memory_injection, "COMPANY_ORG_CONFIG", org_config)

    bridges = memory_injection.retrieve_obsidian_bridge_context(
        "罗总想了解五菱和中台",
        limit=5,
    )

    assert [(item["kind"], item["name"]) for item in bridges] == [
        ("person", "罗鸣"),
        ("entity", "五菱"),
        ("department", "中台部门"),
    ]
    person = bridges[0]
    assert person["metadata"] == {
        "department": "客户部",
        "role": "客户部总监",
        "office": "职能部",
    }
    assert person["content"].count("[[五菱项目]]") == 1
    assert "不应注入的明细" not in person["content"]


def test_obsidian_bridge_context_prioritizes_canonical_company_pages(
    monkeypatch, tmp_path
) -> None:
    vault = tmp_path / "ObsidianVault"
    for relative in (
        "20_Bridges/Canonical",
        "20_Bridges/Entities",
        "20_Bridges/Departments",
        "20_Bridges/People",
    ):
        (vault / relative).mkdir(parents=True)
    (vault / "20_Bridges" / "Canonical" / "公司-总览.md").write_text(
        "---\n"
        "page_kind: canonical_company_context\n"
        "canonical_type: company\n"
        "review_status: need_review\n"
        "---\n\n"
        "# 公司总览\n\n## 已知结构化事实\n- 已配置部门：16\n",
        encoding="utf-8",
    )
    (vault / "20_Bridges" / "Canonical" / "客户-五菱.md").write_text(
        "---\n"
        "page_kind: canonical_company_context\n"
        "canonical_type: client\n"
        "review_status: need_review\n"
        "---\n\n"
        "# 客户：五菱\n\n## 证据入口\n- [[实体-五菱]]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_injection, "OBSIDIAN_VAULT_ROOT", vault)
    monkeypatch.setattr(
        memory_injection,
        "COMPANY_ORG_CONFIG",
        tmp_path / "missing-company-org.json",
    )

    bridges = memory_injection.retrieve_obsidian_bridge_context(
        "了解公司和五菱客户",
        limit=5,
    )

    assert [(item["kind"], item["name"]) for item in bridges] == [
        ("canonical", "五菱"),
        ("canonical", "公司总览"),
    ]
    assert all(item["metadata"]["review_status"] == "need_review" for item in bridges)
    assert bridges[0]["path"] == "20_Bridges/Canonical/客户-五菱.md"


def test_memory_context_formats_obsidian_relationship_bridges() -> None:
    block = memory_injection.format_memory_context(
        {
            "governed_memories": [],
            "documents": [],
            "project_items": [],
            "obsidian_bridges": [
                {
                    "kind": "person",
                    "name": "罗鸣",
                    "path": "20_Bridges/People/罗鸣.md",
                    "metadata": {"department": "客户部", "role": "客户部总监"},
                    "content": "# 罗鸣\n类型：人员\n- [[五菱项目]]",
                }
            ],
        }
    )

    assert "Obsidian 关系桥接页" in block
    assert "关系索引，不等同于人工确认事实" in block
    assert "人员=罗鸣" in block
    assert "部门=客户部" in block
    assert "角色=客户部总监" in block
    assert "来源=ObsidianVault/20_Bridges/People/罗鸣.md" in block


def test_governed_memory_recall_uses_bounded_index_queries(
    monkeypatch, tmp_path
) -> None:
    db_path = tmp_path / "governed_memory.db"
    db_path.touch()
    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(memory_injection, "GOVERNED_MEMORY_DB", db_path)
    monkeypatch.setattr(
        memory_injection, "MemoryGovernanceStore", lambda _path: object()
    )

    def fake_recall(*, store, query, limit, **_kwargs):
        _ = store
        calls.append((query, limit))
        return []

    monkeypatch.setattr(memory_injection, "list_recall_memories", fake_recall)

    result = memory_injection.retrieve_governed_memory_context(
        "查一下之前五菱活动方案",
        limit=5,
    )

    assert result["governed_memories"] == []
    assert 0 < len(calls) <= 8
    assert all(query for query, _limit in calls)
    assert all(limit <= 20 for _query, limit in calls)


def test_memory_injection_publishes_typed_runtime_context_section(monkeypatch) -> None:
    event = _Event("查一下之前五菱活动方案")
    context = {
        "governed_memories": [
            {
                "memory_id": "memory-1",
                "title": "五菱活动方案",
                "canonical_text": "负责人是市场部。",
                "source_path": "vault/五菱活动方案.md",
                "review_status": "approved",
            }
        ],
        "documents": [],
        "project_items": [],
    }
    monkeypatch.setattr(
        memory_injection,
        "retrieve_memory_context",
        lambda _text: context,
    )

    injected = memory_injection.inject_memory_context_into_event(event)

    assert injected is True
    assert event.message_str == "查一下之前五菱活动方案"
    assert event.get_extra("dc_agent_memory_context") == context
    sections = event.get_extra(RUNTIME_CONTEXT_SECTIONS_EXTRA_KEY)
    assert isinstance(sections, list)
    assert len(sections) == 1
    assert isinstance(sections[0], RuntimeContextSection)
    assert sections[0].source_id == "dc_memory_context"
    assert "五菱活动方案" in sections[0].text

    request = ProviderRequest(prompt=event.message_str)
    RuntimeContextAssembler().normalize(request, event=event)

    assert request.prompt == "查一下之前五菱活动方案"
    assert len(request.extra_user_content_parts) == 1
    assert "五菱活动方案" in request.extra_user_content_parts[0].text
    assert getattr(request.extra_user_content_parts[0], "_no_save") is True


def test_quotation_context_includes_matched_price_evidence() -> None:
    block = memory_injection.format_memory_context(
        {
            "quotation_mode": True,
            "governed_memories": [],
            "project_items": [],
            "documents": [
                {
                    "doc_key": "supplier-1",
                    "title": "聚美广告",
                    "project_name": "活动部供应商库",
                    "doc_type": "预算报价",
                    "review_status": "need_review",
                    "rel_path": "活动部供应商库/聚美广告.md",
                    "summary": "聚美广告历史报价。",
                    "text": "UV 超透贴 35 元/㎡；UV 3M 布 120 元/㎡。",
                }
            ],
        }
    )

    assert "报价草案" in block
    assert "待询价" in block
    assert "税费、运输、安装、加急、损耗和利润" in block
    assert "来源=活动部供应商库/聚美广告.md" in block
    assert "状态=need_review" in block
    assert "UV 超透贴 35 元/㎡" in block
