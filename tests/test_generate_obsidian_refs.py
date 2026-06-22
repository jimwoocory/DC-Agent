from __future__ import annotations

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

    assert {"root", "metrics", "rawrefs"} <= node_ids
    assert {"e-root-metrics", "e-pending-rawrefs"} <= edge_ids
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
        "product-customer",
        "business",
        "people-map",
        "pending",
        "review-workbench",
        "rawrefs",
    } <= set(nodes_by_id)
    assert {
        "e-root-metrics": ("metrics", "root"),
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
