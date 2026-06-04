#!/usr/bin/env python3
"""把 JSON 白名单 merge 进 data/feishu_whitelist.yaml。

用法：
    python scripts-tools/import_feishu_whitelist.py
    python scripts-tools/import_feishu_whitelist.py path/to/my.json
    python scripts-tools/import_feishu_whitelist.py --dry-run

策略：
- 不动 feishu: 凭证段
- documents / tables / folders 三段，按主键去重：
    - documents 主键 = doc_token
    - tables 主键 = (app_token, table_id)
    - folders 主键 = kb_id
- 同主键的条目 **覆盖**（用 JSON 里的新值），不同主键的 **追加**
- 占位符（含"换成真的"或全 X 的）会被识别并跳过（避免污染白名单）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

DC_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_YAML = DC_ROOT / "data" / "feishu_whitelist.yaml"
DEFAULT_JSON = DC_ROOT / "scripts-tools" / "feishu_whitelist_template.json"

PLACEHOLDER_PATTERNS = [
    re.compile(r"^[xXyYzZ]+$"),
    re.compile(r"换成真的"),
    re.compile(r"^docX{8,}$", re.IGNORECASE),
    re.compile(r"^doxc[XY]{8,}$", re.IGNORECASE),
    re.compile(r"^bascn[XY]{8,}$", re.IGNORECASE),
    re.compile(r"^tbl[XY]{8,}$", re.IGNORECASE),
]


def is_placeholder(value: str) -> bool:
    if not value:
        return True
    s = str(value).strip()
    for p in PLACEHOLDER_PATTERNS:
        if p.search(s):
            return True
    # 全是同一字符也跳
    if len(set(s.upper())) <= 2 and len(s) >= 8:
        return True
    return False


def filter_real(items: list[dict], key_fields: list[str]) -> list[dict]:
    """过滤掉主键还在 placeholder 状态的条目。"""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if any(is_placeholder(it.get(k, "")) for k in key_fields):
            continue
        out.append(it)
    return out


def merge_by_key(
    existing: list[dict],
    incoming: list[dict],
    key_fields: list[str],
) -> tuple[list[dict], int, int]:
    """existing 跟 incoming merge：同主键替换，新主键追加。返 (merged, updated, added)。"""

    def key_of(item: dict) -> tuple:
        return tuple(str(item.get(k, "")).strip() for k in key_fields)

    by_key = {key_of(it): it for it in existing or []}
    updated = added = 0
    for it in incoming:
        k = key_of(it)
        if k in by_key:
            by_key[k] = it
            updated += 1
        else:
            by_key[k] = it
            added += 1
    return list(by_key.values()), updated, added


def main() -> int:
    parser = argparse.ArgumentParser(
        description="JSON → feishu_whitelist.yaml 批量导入"
    )
    parser.add_argument("json_path", nargs="?", default=str(DEFAULT_JSON))
    parser.add_argument("--yaml-path", default=str(DEFAULT_YAML))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    json_path = Path(args.json_path)
    yaml_path = Path(args.yaml_path)

    if not json_path.exists():
        print(f"✗ JSON 不存在: {json_path}", file=sys.stderr)
        return 1
    if not yaml_path.exists():
        print(f"✗ yaml 不存在: {yaml_path}", file=sys.stderr)
        return 1

    data = json.loads(json_path.read_text(encoding="utf-8"))
    current = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}

    # 过滤占位符 → 只保留真实条目
    docs_new = filter_real(data.get("documents", []), ["doc_token"])
    tables_new = filter_real(data.get("tables", []), ["app_token", "table_id"])
    folders_new = filter_real(data.get("folders", []), ["kb_id"])

    # 也过滤当前 yaml 里的占位（这一步清理之前 example 留下的 doxcXXXX）
    docs_cur = filter_real(current.get("documents", []), ["doc_token"])
    tables_cur = filter_real(current.get("tables", []), ["app_token", "table_id"])
    folders_cur = filter_real(current.get("folders", []), ["kb_id"])

    docs_merged, du, da = merge_by_key(docs_cur, docs_new, ["doc_token"])
    tables_merged, tu, ta = merge_by_key(
        tables_cur, tables_new, ["app_token", "table_id"]
    )
    folders_merged, fu, fa = merge_by_key(folders_cur, folders_new, ["kb_id"])

    print(f"  documents:  +{da} 新增  ~{du} 更新  ({len(docs_merged)} 总)")
    print(f"  tables:     +{ta} 新增  ~{tu} 更新  ({len(tables_merged)} 总)")
    print(f"  folders:    +{fa} 新增  ~{fu} 更新  ({len(folders_merged)} 总)")

    if not (da + du + ta + tu + fa + fu):
        print()
        print('⚠️ JSON 里没有任何"非占位符"条目可导。')
        print("   提示：把模板里的 doxcXXXX / bascnXXXX 之类占位符换成真 token 再跑。")
        return 0

    # 装回 yaml，保留 feishu: 凭证段不动
    current["documents"] = docs_merged
    current["tables"] = tables_merged
    current["folders"] = folders_merged

    if args.dry_run:
        print()
        print("--- dry-run: 不写文件（feishu: 凭证段已 redact）---")
        preview = {
            k: ("<REDACTED>" if k == "feishu" else v) for k, v in current.items()
        }
        print(yaml.safe_dump(preview, allow_unicode=True, sort_keys=False))
        return 0

    # 备份现有 yaml
    backup = yaml_path.with_suffix(".yaml.bak")
    backup.write_text(yaml_path.read_text(encoding="utf-8"), encoding="utf-8")

    # 写新 yaml
    yaml_path.write_text(
        yaml.safe_dump(current, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print()
    print(f"✓ 写入 {yaml_path}")
    print(f"  备份 → {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
