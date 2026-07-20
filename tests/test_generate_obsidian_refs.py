from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts-company" / "generate_obsidian_refs.py"


def _load_generate_obsidian_refs() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "generate_obsidian_refs_test",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample_docs() -> list[dict[str, str]]:
    return [
        {"parser": "docx", "doc_type": "SOP"},
        {"parser": "pdf", "doc_type": "报告"},
        {"parser": "md", "doc_type": "记录"},
    ]


def _sample_canvas(module: ModuleType) -> dict:
    return module.build_company_canvas(
        _sample_docs(),
        {
            "报告": [{}, {}],
            "SOP": [{}],
            "记录": [{}],
        },
        "2026-06-22T14:00:00+08:00",
    )


def test_canvas_helpers_build_expected_shapes() -> None:
    module = _load_generate_obsidian_refs()

    assert module.canvas_text_node(
        "note",
        "hello",
        0,
        20,
        200,
        100,
        color="1",
    ) == {
        "id": "note",
        "type": "text",
        "text": "hello",
        "x": 0,
        "y": 20,
        "width": 200,
        "height": 100,
        "color": "1",
    }
    assert module.canvas_file_node(
        "file",
        "10_Index/Example.md",
        300,
        20,
        240,
        120,
    ) == {
        "id": "file",
        "type": "file",
        "file": "10_Index/Example.md",
        "x": 300,
        "y": 20,
        "width": 240,
        "height": 120,
    }
    assert module.canvas_edge(
        "e-note-file",
        "note",
        "right",
        "file",
        "left",
        label="supports",
    ) == {
        "id": "e-note-file",
        "fromNode": "note",
        "fromSide": "right",
        "toNode": "file",
        "toSide": "left",
        "label": "supports",
    }


def test_company_canvas_place_keeps_zone_relative_coordinates() -> None:
    module = _load_generate_obsidian_refs()

    assert module.company_canvas_place("center") == {"zone": "center", "x": 0, "y": 0}
    assert module.company_canvas_place("product", dx=-420, dy=-170) == {
        "zone": "product",
        "x": -1040,
        "y": -290,
    }
    assert module.company_canvas_place("review", dx=260, dy=220) == {
        "zone": "review",
        "x": 0,
        "y": 920,
    }


def test_company_canvas_specs_are_declarative_and_complete() -> None:
    module = _load_generate_obsidian_refs()

    node_ids = {spec["id"] for spec in module.COMPANY_CANVAS_NODE_SPECS}
    edge_ids = {spec["id"] for spec in module.COMPANY_CANVAS_EDGE_SPECS}

    assert {"root", "metrics", "taxonomy", "rawrefs"} <= node_ids
    assert {"e-root-metrics", "e-root-taxonomy", "e-pending-rawrefs"} <= edge_ids
    assert len(node_ids) == len(module.COMPANY_CANVAS_NODE_SPECS)
    assert len(edge_ids) == len(module.COMPANY_CANVAS_EDGE_SPECS)
    assert all(
        spec["zone"] in module.COMPANY_CANVAS_ZONES
        for spec in module.COMPANY_CANVAS_NODE_SPECS
    )
    for edge in module.COMPANY_CANVAS_EDGE_SPECS:
        assert edge["from_node"] in node_ids
        assert edge["to_node"] in node_ids


def test_build_company_canvas_returns_valid_canvas_graph() -> None:
    module = _load_generate_obsidian_refs()

    canvas = _sample_canvas(module)

    assert set(canvas) == {"nodes", "edges"}
    assert module.validate_json_canvas(canvas) == []
    node_ids = {node["id"] for node in canvas["nodes"]}
    assert len(node_ids) == len(canvas["nodes"])

    edge_ids = {edge["id"] for edge in canvas["edges"]}
    assert len(edge_ids) == len(canvas["edges"])
    for edge in canvas["edges"]:
        assert edge["fromNode"] in node_ids
        assert edge["toNode"] in node_ids

    metrics = next(node for node in canvas["nodes"] if node["id"] == "metrics")
    product_line = next(
        node for node in canvas["nodes"] if node["id"] == "product-line"
    )
    rawrefs = next(node for node in canvas["nodes"] if node["id"] == "rawrefs")
    assert "RawRefs：3" in metrics["text"]
    assert "P0 候选：2" in metrics["text"]
    assert "类型 Top5：报告 2, SOP 1, 记录 1" in metrics["text"]
    assert (product_line["x"], product_line["y"]) == (-1040, -290)
    assert (rawrefs["x"], rawrefs["y"]) == (0, 920)


def test_company_canvas_keeps_core_nodes_and_main_paths() -> None:
    module = _load_generate_obsidian_refs()

    canvas = _sample_canvas(module)
    nodes_by_id = {node["id"]: node for node in canvas["nodes"]}
    edges_by_id = {edge["id"]: edge for edge in canvas["edges"]}

    assert {
        "root",
        "metrics",
        "taxonomy",
        "product-customer",
        "business",
        "people-map",
        "pending",
        "review-workbench",
        "rawrefs",
    } <= set(nodes_by_id)
    assert {
        "e-root-metrics": ("metrics", "root"),
        "e-root-taxonomy": ("root", "taxonomy"),
        "e-root-product": ("root", "product-customer"),
        "e-root-business": ("root", "business"),
        "e-root-people": ("root", "people-map"),
        "e-root-pending": ("root", "pending"),
        "e-pending-review": ("pending", "review-workbench"),
        "e-pending-rawrefs": ("review-workbench", "rawrefs"),
    } == {
        edge_id: (edges_by_id[edge_id]["fromNode"], edges_by_id[edge_id]["toNode"])
        for edge_id in (
            "e-root-metrics",
            "e-root-taxonomy",
            "e-root-product",
            "e-root-business",
            "e-root-people",
            "e-root-pending",
            "e-pending-review",
            "e-pending-rawrefs",
        )
    }
    specs_by_id = {spec["id"]: spec for spec in module.COMPANY_CANVAS_NODE_SPECS}
    assert specs_by_id["product-customer"]["zone"] == "product"
    assert specs_by_id["business"]["zone"] == "business"
    assert specs_by_id["people-map"]["zone"] == "people"
    assert specs_by_id["review-workbench"]["zone"] == "review"


def test_write_company_canvas_writes_parseable_json(tmp_path: Path) -> None:
    module = _load_generate_obsidian_refs()
    vault_path = tmp_path / "ObsidianVault"
    (vault_path / "10_Index").mkdir(parents=True)

    module.write_company_canvas(
        vault_path,
        _sample_docs(),
        {"SOP": [{}]},
        "2026-06-22T14:00:00+08:00",
    )

    output_path = vault_path / "10_Index" / "公司知识地图.canvas"
    canvas = json.loads(output_path.read_text(encoding="utf-8"))

    assert canvas["nodes"][0]["id"] == "root"
    assert canvas["edges"][0]["id"] == "e-root-metrics"


def test_validate_json_canvas_reports_structural_errors() -> None:
    module = _load_generate_obsidian_refs()

    errors = module.validate_json_canvas(
        {
            "nodes": [
                {
                    "id": "root",
                    "type": "text",
                    "x": 0,
                    "y": 0,
                    "width": 200,
                    "height": 100,
                },
                {
                    "id": "root",
                    "type": "file",
                    "x": 300,
                    "y": 0,
                    "width": 200,
                    "height": 100,
                    "file": "10_Index/重复.md",
                },
            ],
            "edges": [
                {
                    "id": "root",
                    "fromNode": "root",
                    "toNode": "missing",
                    "fromSide": "middle",
                    "toEnd": "triangle",
                }
            ],
        }
    )

    assert "duplicate canvas id: root" in errors
    assert "text node root missing text" in errors
    assert "edge root references missing toNode: missing" in errors
    assert "edge root has invalid fromSide: middle" in errors
    assert "edge root has invalid toEnd: triangle" in errors


def test_company_knowledge_taxonomy_is_valid_and_complete() -> None:
    module = _load_generate_obsidian_refs()
    taxonomy = module.load_knowledge_taxonomy(
        PROJECT_ROOT / "scripts-company" / "company_knowledge_taxonomy.json"
    )

    assert module.validate_knowledge_taxonomy(taxonomy) == []
    assert {node["label"] for node in taxonomy["levels"]} == {
        "产品",
        "项目",
        "客户",
        "合同",
        "交付",
        "售后",
        "财务",
        "行政",
        "人事",
        "技术",
        "市场",
        "待人工确认",
    }
    paths = module.iter_taxonomy_paths(taxonomy)
    assert paths
    assert all(len(path) == 3 for path in paths)
    assert all(not path[2].get("children") for path in paths)


def test_classify_document_returns_auditable_paths_and_safe_fallbacks() -> None:
    module = _load_generate_obsidian_refs()
    taxonomy = module.load_knowledge_taxonomy(
        PROJECT_ROOT / "scripts-company" / "company_knowledge_taxonomy.json"
    )

    classified = module.classify_document(
        {
            "title": "五菱用户传播策略",
            "summary": "品牌传播规划",
            "rel_path": "策划部日常工作方案进度记录/附件/传播策略.pptx",
            "project_name": "五菱用户传播",
            "doc_type": "传播策略",
        },
        taxonomy,
    )
    unmatched = module.classify_document(
        {
            "title": "附件001",
            "summary": "",
            "rel_path": "附件001.pdf",
            "project_name": "",
            "doc_type": "资料文档",
        },
        taxonomy,
    )

    conflicting_taxonomy = copy.deepcopy(taxonomy)
    conflicting_taxonomy["levels"][0]["children"][0]["children"][0]["keywords"] = [
        "同分样本"
    ]
    conflicting_taxonomy["levels"][2]["children"][0]["children"][0]["keywords"] = [
        "同分样本"
    ]
    conflict = module.classify_document(
        {
            "title": "同分样本",
            "summary": "",
            "rel_path": "同分样本.pdf",
            "project_name": "",
            "doc_type": "资料文档",
        },
        conflicting_taxonomy,
    )

    assert classified["path"] == "市场 / 品牌传播 / 传播与营销策略"
    assert classified["status"] == "rule_classified"
    assert classified["confidence"] > 0
    assert "doc_type:传播策略" in classified["evidence"]
    assert unmatched["path"] == "待人工确认 / 待复核 / 未命中规则"
    assert unmatched["status"] == "needs_review"
    assert unmatched["evidence"] == ["fallback:no_rule_matched"]
    assert conflict["path"] == "待人工确认 / 待复核 / 分类规则冲突"
    assert conflict["status"] == "needs_review"
    assert conflict["evidence"][0] == "fallback:top_score_tie"


def test_classify_document_applies_user_confirmed_business_ownership_rules() -> None:
    module = _load_generate_obsidian_refs()
    taxonomy = module.load_knowledge_taxonomy(
        PROJECT_ROOT / "scripts-company" / "company_knowledge_taxonomy.json"
    )
    entity_taxonomy = module.load_entity_taxonomy(
        PROJECT_ROOT / "scripts-company" / "company_entity_taxonomy.json"
    )
    product_lines = {
        entity["name"] for entity in entity_taxonomy["entity_types"]["产品线"]
    }
    product_assets = {
        entity["name"] for entity in entity_taxonomy["entity_types"]["车型与品牌资产"]
    }

    assert "宝骏" not in product_lines
    assert {"宝骏", "乘龙", "风行"} <= product_assets
    assert "归属产品族：[[实体-五菱]]" in module.render_entity_taxonomy_bridge(
        "车型与品牌资产", "宝骏", [], "2026-07-13T00:00:00Z"
    )
    assert "归属产品族：[[实体-柳汽]]" in module.render_entity_taxonomy_bridge(
        "车型与品牌资产", "乘龙", [], "2026-07-13T00:00:00Z"
    )

    cases = [
        ("缤果，8", "资料文档", "缤果，8.xlsx", "产品 / 五菱产品族 / 缤果车型资料"),
        ("星光7", "资料文档", "星光7.xlsx", "产品 / 五菱产品族 / 星光车型资料"),
        (
            "马卡龙7",
            "资料文档",
            "马卡龙7.xlsx",
            "产品 / 五菱产品族 / 宏光MINIEV与马卡龙",
        ),
        ("宝骏资料", "资料文档", "宝骏资料.pdf", "产品 / 五菱产品族 / 宝骏品牌资料"),
        ("乘龙资料", "资料文档", "乘龙资料.pdf", "产品 / 柳汽产品族 / 乘龙车型资料"),
        ("风行资料", "资料文档", "风行资料.pdf", "产品 / 柳汽产品族 / 风行车型资料"),
        (
            "新年礼品方案",
            "资料文档",
            "新年礼品方案.pdf",
            "交付 / 执行部门筹备组 / 礼品与物料",
        ),
        (
            "未知提案",
            "资料文档",
            "策划部工作记录__attachments/策划方案/未知提案.pdf",
            "交付 / 执行部门筹备组 / 策划与提案资料",
        ),
        (
            "服务器维护日报",
            "资料文档",
            "日常任务报告/服务器维护日报.md",
            "技术 / AI应用组 / 服务器维护与技术日报",
        ),
        (
            "供应商公司简介",
            "资料文档",
            "供应商库整理/企业资料/供应商公司简介.pdf",
            "合同 / 采购管理 / 采购与供应商",
        ),
    ]

    for title, doc_type, rel_path, expected_path in cases:
        classified = module.classify_document(
            {
                "title": title,
                "summary": "",
                "rel_path": rel_path,
                "project_name": title,
                "doc_type": doc_type,
            },
            taxonomy,
        )

        assert classified["path"] == expected_path
        assert classified["status"] == "rule_classified"
        assert classified["evidence"]
        if "执行部门筹备组" in expected_path:
            assert classified["department"] == "筹备组"
        elif "AI应用组" in expected_path:
            assert classified["department"] == "AI应用组"
        else:
            assert classified["department"] == ""


def test_write_three_level_taxonomy_builds_all_levels_and_leaf_links(
    tmp_path: Path,
) -> None:
    module = _load_generate_obsidian_refs()
    taxonomy = module.load_knowledge_taxonomy(
        PROJECT_ROOT / "scripts-company" / "company_knowledge_taxonomy.json"
    )
    vault = tmp_path / "ObsidianVault"
    (vault / "10_Index").mkdir(parents=True)
    classification = module.classify_document(
        {
            "title": "年度传播策略",
            "summary": "品牌传播规划",
            "rel_path": "市场/年度传播策略.pptx",
            "project_name": "年度传播",
            "doc_type": "传播策略",
        },
        taxonomy,
    )
    docs = [
        {
            "link_title": "年度传播策略-rawref",
            "doc_type": "传播策略",
            "rel_path": "市场/年度传播策略.pptx",
            "taxonomy": classification,
        }
    ]

    stats = module.write_three_level_taxonomy(
        vault, docs, taxonomy, "2026-07-13T00:00:00Z"
    )

    root = (vault / "10_Index" / "三级分类.md").read_text(encoding="utf-8")
    level_1 = (vault / "20_Bridges" / "Taxonomy" / "L1" / "分类-1-市场.md").read_text(
        encoding="utf-8"
    )
    level_2 = (
        vault / "20_Bridges" / "Taxonomy" / "L2" / "分类-2-市场-品牌传播.md"
    ).read_text(encoding="utf-8")
    level_3 = (
        vault
        / "20_Bridges"
        / "Taxonomy"
        / "L3"
        / "分类-3-市场-品牌传播-传播与营销策略.md"
    ).read_text(encoding="utf-8")

    assert "[[分类-1-市场]]：1" in root
    assert "[[分类-2-市场-品牌传播]]：1" in level_1
    assert "[[分类-3-市场-品牌传播-传播与营销策略]]：1" in level_2
    assert "[[年度传播策略-rawref]]" in level_3
    assert stats["classified_docs"] == 1
    assert stats["needs_review_docs"] == 0


def test_render_raw_ref_includes_three_level_taxonomy() -> None:
    module = _load_generate_obsidian_refs()
    taxonomy = module.load_knowledge_taxonomy(
        PROJECT_ROOT / "scripts-company" / "company_knowledge_taxonomy.json"
    )
    row = {
        "doc_key": "doc-1",
        "source_path": "/nas/市场/传播策略.pptx",
        "rel_path": "市场/传播策略.pptx",
        "sha256": "abc123",
        "parser": "pptx",
        "doc_type": "传播策略",
        "project_id": "project-1",
        "project_name": "年度传播",
        "owner": "",
        "review_status": "need_review",
        "confidence": 0.8,
        "indexed_at": "2026-07-13T00:00:00Z",
        "file_size": 1024,
        "tags_json": "[]",
        "participants_json": "[]",
        "departments_json": "[]",
        "metadata_json": "{}",
        "summary": "年度品牌传播规划",
        "title": "年度传播策略",
    }
    classification = module.classify_document(row, taxonomy)

    rendered = module.render_raw_ref(
        row,
        "年度传播策略",
        1,
        "正文预览",
        {"departments": {}, "people": {}},
        {},
        classification,
    )

    assert 'category_level_1: "市场"' in rendered
    assert 'category_level_2: "品牌传播"' in rendered
    assert 'category_level_3: "传播与营销策略"' in rendered
    assert 'category_path: "市场 / 品牌传播 / 传播与营销策略"' in rendered
    assert 'category_department: ""' in rendered
    assert "[[分类-1-市场]] → [[分类-2-市场-品牌传播]]" in rendered


def test_write_canonical_company_context_builds_reviewable_standard_pages(
    tmp_path: Path,
) -> None:
    module = _load_generate_obsidian_refs()
    vault = tmp_path / "ObsidianVault"
    org_model = {
        "roots": [
            {
                "name": "总经办",
                "children": [
                    {
                        "name": "中台部门",
                        "aliases": ["中台"],
                        "children": [
                            {"name": "客户部"},
                            {"name": "策略部"},
                        ],
                    }
                ],
            }
        ],
        "departments": {
            "总经办": {
                "parent": "",
                "lead": "杨国民",
                "aliases": [],
                "children": ["中台部门"],
            },
            "中台部门": {
                "parent": "总经办",
                "lead": "罗鸣",
                "aliases": ["中台"],
                "children": ["客户部", "策略部"],
            },
        },
        "people": {
            "杨国民": {
                "department": "总经办",
                "role": "总经理",
                "office": "职能部",
            },
            "罗鸣": {
                "department": "客户部",
                "role": "客户部总监",
                "office": "职能部",
            },
        },
    }
    entity_taxonomy = {
        "entity_types": {
            "产品线": [
                {
                    "name": "五菱",
                    "aliases": ["SGMW"],
                    "keywords": ["五菱", "星光", "缤果"],
                },
                {
                    "name": "柳汽",
                    "aliases": ["东风柳汽"],
                    "keywords": ["柳汽", "东风柳汽"],
                },
            ]
        }
    }

    count = module.write_canonical_company_context(
        vault,
        org_model,
        entity_taxonomy,
        "2026-07-20T00:00:00Z",
    )

    canonical = vault / "20_Bridges" / "Canonical"
    assert count == 6
    assert (vault / "10_Index" / "公司认知入口.md").exists()
    assert (canonical / "公司-总览.md").exists()
    assert (canonical / "管理层-杨国民.md").exists()
    assert (canonical / "管理层-罗鸣.md").exists()
    assert (canonical / "部门-中台部门.md").exists()
    assert (canonical / "客户-五菱.md").exists()
    assert (canonical / "客户-柳汽.md").exists()

    manager = (canonical / "管理层-杨国民.md").read_text(encoding="utf-8")
    assert 'page_kind: "canonical_company_context"' in manager
    assert 'canonical_type: "management"' in manager
    assert 'review_status: "need_review"' in manager
    assert "- 部门：总经办" in manager
    assert "- 角色：总经理" in manager
    assert "## 待人工确认" in manager
    assert "当前关注重点" in manager
    assert "不得从关联文件数量推断管理层观点" in manager

    client = (canonical / "客户-五菱.md").read_text(encoding="utf-8")
    assert "- 已配置别名：SGMW" in client
    assert "- [[实体-五菱]]" in client
    assert "当前关键联系人" in client
    assert "客户偏好与禁区" in client

    overview = (canonical / "公司-总览.md").read_text(encoding="utf-8")
    assert "- 已配置人员：2" in overview
    assert "- 已配置部门：2" in overview
    assert "- [[管理层-杨国民]]" in overview
    assert "- [[客户-五菱]]" in overview

    knowledge_map = module.render_clean_knowledge_map(
        [],
        "2026-07-20T00:00:00Z",
    )
    assert "- [[公司认知入口]]" in knowledge_map
