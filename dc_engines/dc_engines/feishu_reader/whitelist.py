"""白名单 + 凭证加载。

期望 yaml 格式：

```yaml
feishu:
  app_id: cli_a1b2c3...
  app_secret: ABCdef...
  enable: true

documents:
  - doc_token: doxcXXXXX
    name: 营销策划模板库
    domain: marketing
    description: 历史营销方案模板

tables:
  - app_token: bascnXXXXX
    table_id: tblXXXXX
    name: 客户应标资料
    domain: client
    primary_key: client_name

folders:
  - kb_id: client_archives
    name: 客户档案
    domain: client
```

向后兼容：
- 旧版用 ``id`` 而非 ``doc_token`` / 旧表格只有 ``id`` 没 ``app_token`` —— 仍会解析（但 v1
  内容检索会跳过它们，因为缺 token 没法调 API）
- ``feishu`` 段缺失 → 返回 None 凭证，plugin 退到 v0 元信息匹配
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .contracts import (
    FeishuCredentials,
    Whitelist,
    WhitelistDocument,
    WhitelistFolder,
    WhitelistTable,
)


def _credentials(raw: dict) -> FeishuCredentials | None:
    fs = raw.get("feishu") or {}
    if not isinstance(fs, dict):
        return None
    app_id = str(fs.get("app_id", "")).strip()
    app_secret = str(fs.get("app_secret", "")).strip()
    enable = bool(fs.get("enable", True))
    if not app_id or not app_secret:
        return None
    return FeishuCredentials(app_id=app_id, app_secret=app_secret, enable=enable)


def load_whitelist(path: Path | str) -> tuple[Whitelist, FeishuCredentials | None]:
    """加载 yaml 白名单 + 凭证。

    返回 ``(Whitelist, FeishuCredentials | None)``；凭证为 None 时 plugin 走 v0。
    文件不存在 / 解析失败：返回空 Whitelist + None（不抛错）。
    """
    p = Path(path)
    if not p.exists():
        return Whitelist(), None

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return Whitelist(), None

    if not isinstance(raw, dict):
        return Whitelist(), None

    creds = _credentials(raw)

    docs = []
    for d in raw.get("documents") or []:
        if not isinstance(d, dict):
            continue
        token = str(d.get("doc_token") or d.get("id") or "").strip()
        if not token:
            continue
        docs.append(
            WhitelistDocument(
                doc_token=token,
                name=str(d.get("name", "")).strip() or "未命名文档",
                domain=str(d.get("domain", "general")),
                description=str(d.get("description", "")),
            )
        )

    tables = []
    for t in raw.get("tables") or []:
        if not isinstance(t, dict):
            continue
        app_token = str(t.get("app_token") or "").strip()
        table_id = str(t.get("table_id") or t.get("id") or "").strip()
        if not table_id:
            continue
        tables.append(
            WhitelistTable(
                app_token=app_token,
                table_id=table_id,
                name=str(t.get("name", "")).strip() or "未命名表格",
                domain=str(t.get("domain", "general")),
                description=str(t.get("description", "")),
                primary_key=t.get("primary_key"),
            )
        )

    folders = []
    for f in raw.get("folders") or []:
        if not isinstance(f, dict):
            continue
        kb_id = str(f.get("kb_id") or "").strip()
        if not kb_id:
            continue
        folders.append(
            WhitelistFolder(
                kb_id=kb_id,
                name=str(f.get("name", "")).strip() or "未命名 KB",
                domain=str(f.get("domain", "general")),
                description=str(f.get("description", "")),
            )
        )

    return Whitelist(documents=docs, tables=tables, folders=folders), creds
