#!/usr/bin/env python3
"""把当前 cmd_config.json 里被 linter 替换的 ${REDACTED_*} 占位符
还原成 DC-Agent-Old/data/cmd_config.json 里的真 secret。

用法：
    .venv/bin/python scripts-tools/restore_redacted_secrets.py [--dry-run]

逻辑：
- 按 app_id 匹配 lark 平台的 app_secret
- 按 provider_source id 匹配 aihubmix key
- 按 embedding provider id 匹配 embedding_api_key
- 只替换占位符（REDACTED / ${REDACTED_*}），不动其他字段
- 自动备份当前 cmd_config 到 data/_backup_cmd_config_pre_unredact_<TS>.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

OLD = Path("/Users/dianchi/DC-Agent-Old/data/cmd_config.json")
CUR = Path("/Users/dianchi/DC-Agent/data/cmd_config.json")
BACKUP_DIR = Path("/Users/dianchi/DC-Agent/data")


def is_redacted(s: str) -> bool:
    return not s or "REDACTED" in s or s.startswith("${")


def load(path: Path) -> dict:
    with path.open(encoding="utf-8-sig") as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印改动，不写文件")
    args = ap.parse_args()

    old = load(OLD)
    cur = load(CUR)

    # 索引 old 里的所有真 secret
    lark_secrets: dict[str, str] = {}
    for p in old.get("platform", []):
        if p.get("type") == "lark":
            app_id = p.get("app_id", "")
            secret = p.get("app_secret", "")
            if app_id and not is_redacted(secret):
                lark_secrets[app_id] = secret

    aihubmix_key = None
    for ps in old.get("provider_sources", []):
        if ps.get("id") == "aihubmix":
            keys = ps.get("key", [])
            if keys and not is_redacted(keys[0]):
                aihubmix_key = keys[0]
                break

    embedding_key = None
    for prov in old.get("provider", []):
        if prov.get("id") == "openai_embedding":
            k = prov.get("embedding_api_key", "")
            if not is_redacted(k):
                embedding_key = k
                break

    print("=== 从 OLD 提取到 ===")
    print(
        f"  lark secrets: {len(lark_secrets)} 个 (app_ids: {list(lark_secrets.keys())})"
    )
    print(f"  aihubmix key: {'✅ 有' if aihubmix_key else '❌ 没找到'}")
    print(f"  embedding key: {'✅ 有' if embedding_key else '❌ 没找到'}")
    print()

    # 替换 current 里的占位符
    changes: list[str] = []

    for p in cur.get("platform", []):
        if p.get("type") == "lark":
            app_id = p.get("app_id", "")
            if is_redacted(p.get("app_secret", "")) and app_id in lark_secrets:
                p["app_secret"] = lark_secrets[app_id]
                changes.append(f"lark[{app_id}] app_secret 恢复")

    if aihubmix_key:
        for ps in cur.get("provider_sources", []):
            if ps.get("id") == "aihubmix":
                keys = ps.get("key", [])
                if keys and is_redacted(keys[0]):
                    ps["key"] = [aihubmix_key]
                    changes.append("provider_sources/aihubmix key 恢复")

    if embedding_key:
        for prov in cur.get("provider", []):
            if prov.get("id") == "openai_embedding" and is_redacted(
                prov.get("embedding_api_key", "")
            ):
                prov["embedding_api_key"] = embedding_key
                changes.append("openai_embedding key 恢复")

    if not changes:
        print("=== 没有需要恢复的占位符（或者 OLD 里也找不到对应的真 secret）===")
        return 1

    print("=== 将要做的改动 ===")
    for c in changes:
        print(f"  · {c}")
    print()

    if args.dry_run:
        print("--dry-run：不写入。")
        return 0

    # 备份当前
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"_backup_cmd_config_pre_unredact_{ts}.json"
    shutil.copy2(CUR, backup_path)
    print(f"✅ 当前 cmd_config 已备份: {backup_path}")

    # 写回
    with CUR.open("w", encoding="utf-8") as f:
        json.dump(cur, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"✅ {CUR} 已恢复 {len(changes)} 处 secret")
    print()
    print("=== 下一步 ===")
    print("  1. launchctl kickstart -k gui/$(id -u)/io.astrbot.bot")
    print("  2. 等 6 秒")
    print("  3. tail astrbot.log，应该看不到 'app_id or app_secret is invalid' 错误")
    print("  4. 飞书发条消息测试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
