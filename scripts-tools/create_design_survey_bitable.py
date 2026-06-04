#!/usr/bin/env python3
"""一次性脚本：在飞书云空间创建「设计部 AI 工具需求调研」多维表格 + 表单视图。

用法：
    python scripts-tools/create_design_survey_bitable.py \\
        --folder-token <飞书云空间文件夹 token>

    # 只打印计划不真实调用：
    python scripts-tools/create_design_survey_bitable.py \\
        --folder-token xxx --dry-run

    # 改表名：
    python scripts-tools/create_design_survey_bitable.py \\
        --folder-token xxx --name "设计部需求调研 v2"

环境变量：
    必须先 source ~/.dc-agent.env（提供 FEISHU_APP_SECRET）

权限要求（飞书开放平台 → 应用后台勾选 → 发版 → 等几分钟生效）：
    - bitable:app                多维表格完整管理
    - drive:drive  或  drive:file:create   在文件夹下创建文件

如果文件夹是普通文件夹（非 app 自己空间），还需要在飞书云空间
把那个文件夹的协作者加上当前 app（cli_aa8cc8***）+ 给"可编辑"权限。

失败时看 errcode：
    - 99991663 / 1254030     权限缺失 → 去后台勾 scope
    - 1254003                bad request → 字段定义有问题
    - 1254303                folder_token 无效
    - 90002 / 1254301        文件夹没把 app 加协作者
    - 99991672               表单字段不可设为必填（默认表字段限制）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DC_ROOT / "dc_engines"))

from dc_engines.feishu_hub import get_client, get_credentials, is_enabled  # noqa: E402
from lark_oapi.api.bitable.v1 import (  # noqa: E402
    App,
    AppTableField,
    AppTableFieldProperty,
    AppTableFieldPropertyOption,
    AppTableForm,
    AppTableFormPatchedField,
    CreateAppRequest,
    CreateAppTableFieldRequest,
    CreateAppTableViewRequest,
    PatchAppTableFormFieldRequest,
    PatchAppTableFormRequest,
    ReqView,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("survey")


# ──────────────────────── 飞书 bitable 字段类型常量 ────────────────────────
# 详见 https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/...
TYPE_TEXT = 1  # 多行文本
TYPE_NUMBER = 2  # 数字
TYPE_SINGLE_SELECT = 3  # 单选
TYPE_MULTI_SELECT = 4  # 多选


# ──────────────────────── 17 题字段定义 ────────────────────────
# 第 1 题（姓名）必须是 TEXT，因为飞书 bitable 的 primary 字段只能是文本。
@dataclass(frozen=True)
class FieldSpec:
    title: str  # 表单上显示的题目（也是表格列名）
    type: int  # 字段类型
    required: bool = False
    description: str = ""  # 表单字段的辅助说明（题目下方小灰字）
    options: tuple[str, ...] = ()  # 单选/多选的候选项


FIELDS: list[FieldSpec] = [
    FieldSpec(
        title="Q1 你的姓名",
        type=TYPE_TEXT,
        required=True,
        description="方便后续找种子用户做访谈",
    ),
    FieldSpec(
        title="Q2 你在设计部主要负责的方向",
        type=TYPE_SINGLE_SELECT,
        required=True,
        options=(
            "平面设计(海报/KV/物料)",
            "视频剪辑/动效",
            "UI/网页/H5",
            "3D/建模",
            "综合岗(什么都做)",
            "其他",
        ),
    ),
    FieldSpec(
        title="Q3 每周大概处理多少张图",
        type=TYPE_SINGLE_SELECT,
        required=True,
        options=("少于 50 张", "50–200 张", "200–500 张", "500 张以上"),
    ),
    FieldSpec(
        title="Q4 一次性批量处理过最多多少张图",
        type=TYPE_SINGLE_SELECT,
        required=True,
        description="比如一个项目交付时统一导出",
        options=("少于 10 张", "10–50 张", "50–200 张", "200 张以上"),
    ),
    FieldSpec(
        title="Q5 80% 的时间花在哪几类操作上(最多 5 项)",
        type=TYPE_MULTI_SELECT,
        required=True,
        options=(
            "抠图/去背景",
            "修图/磨皮/调色",
            "排版/文字处理",
            "合成/图层叠加",
            "批量改尺寸/改格式",
            "加水印/加 Logo",
            "套模板/替换素材",
            "切图/导出多版本",
            "找素材/翻历史文件",
            "与甲方沟通改稿",
            "其他",
        ),
    ),
    FieldSpec(
        title="Q6 最不想再做第二次的 3 个操作",
        type=TYPE_TEXT,
        required=True,
        description="越具体越好。例:每次活动结束要把 200 张照片统一加水印 + 改成 1080×1080 + 导出 jpg,要折腾 2 小时",
    ),
    FieldSpec(
        title="Q7 上一次加班/赶工,时间主要被什么吃掉了",
        type=TYPE_TEXT,
        required=True,
        description="例:不是设计本身慢,是导出 30 个尺寸 + 命名 + 上传飞书 + 通知甲方,这一套流程每次 1 小时",
    ),
    FieldSpec(
        title="Q8 找一张上次某项目用过的素材,平均花多久",
        type=TYPE_SINGLE_SELECT,
        required=True,
        options=(
            "1 分钟内(命名规范+路径清晰)",
            "1–5 分钟(要翻几个文件夹)",
            "5–15 分钟(要靠记忆+关键词搜)",
            "15 分钟以上(基本得问同事)",
            "经常找不到,直接重做",
        ),
    ),
    FieldSpec(
        title="Q9 素材库/历史文件主要存在哪",
        type=TYPE_MULTI_SELECT,
        required=True,
        options=(
            "公司 NAS",
            "飞书云文档/飞书云盘",
            "自己电脑本地硬盘",
            "移动硬盘/U 盘",
            "百度网盘/阿里云盘等",
            "没有系统的素材库,每次重找",
            "其他",
        ),
    ),
    FieldSpec(
        title="Q10 用过/在用哪些 AI 设计工具",
        type=TYPE_MULTI_SELECT,
        required=True,
        options=(
            "即梦/Dreamina",
            "Midjourney",
            "Stable Diffusion/ComfyUI",
            "Photoshop 创成式填充(Firefly)",
            "通义万相/文心一格/智谱清言",
            "Krea/Magnific(放大、增强)",
            "Runway/Pika(视频 AI)",
            "剪映的 AI 功能",
            "没怎么用过",
            "其他",
        ),
    ),
    FieldSpec(
        title="Q11 哪些场景你愿意让 AI 出第一版,自己再调",
        type=TYPE_TEXT,
        required=True,
        description="例:初稿配色/海报概念图/背景图,我接受 AI 出,自己再改",
    ),
    FieldSpec(
        title="Q12 哪些场景你坚决不接受 AI,必须 100% 人工",
        type=TYPE_TEXT,
        required=True,
        description="例:主视觉 KV/客户 logo 相关/人物精修",
    ),
    FieldSpec(
        title="Q13 如果飞书里有「@设计助手」机器人,最希望它干的第一件事",
        type=TYPE_TEXT,
        required=True,
        description="例:我发一张图给它,它自动抠图+换 3 种背景色返回给我",
    ),
    FieldSpec(
        title="Q14 接受「飞书发回处理好的图,我下载再继续 PS」这种异步工作流吗",
        type=TYPE_SINGLE_SELECT,
        required=True,
        options=(
            "完全接受,反正现在素材也是在飞书传来传去",
            "视场景接受(简单的可以,复杂的还是在 PS 里搞)",
            "不接受,希望直接在 PS 里有插件",
        ),
    ),
    FieldSpec(
        title="Q15 一个工具能省 30% 时间,你愿意付出多少学习成本",
        type=TYPE_SINGLE_SELECT,
        required=True,
        options=(
            "10 分钟内(看个短视频就要能上手)",
            "10–30 分钟(有简单文档就行)",
            "30 分钟–2 小时(愿意参加一次培训)",
            "2 小时以上(如果真的有用,深度学习也行)",
        ),
    ),
    FieldSpec(
        title="Q16 愿意当第一批种子用户陪 DC-Agent 项目组迭代吗",
        type=TYPE_SINGLE_SELECT,
        required=True,
        options=(
            "愿意,可以每周抽 30 分钟反馈",
            "视具体功能而定",
            "暂时不愿意",
        ),
    ),
    FieldSpec(
        title="Q17 还有什么想对项目组说的(选填)",
        type=TYPE_TEXT,
        required=False,
        description="吐槽、建议、灵感都欢迎",
    ),
]


FORM_DESCRIPTION = """Hi 设计师们:

我们正在调研——能不能在飞书里搞一个"设计助手",把你们最烦的那部分活儿
(批量处理、抠图、找素材、加水印…)自动化掉。

这份问卷会决定我们先做哪个功能,所以你的真实想法非常重要。

⚠️ 请如实填写:你写得越具体("做某某项目时一次要导 200 张图,每张手工存"),
我们做出来的工具才越能戳中你。

填完有什么不清楚的,群里 @ 蔡挺 即可。

预计填写时间:8 分钟。"""


# ──────────────────────── 工具函数 ────────────────────────


def build_field_property(spec: FieldSpec) -> AppTableFieldProperty | None:
    """单选/多选要传 options;文本字段不需要 property。"""
    if spec.type in (TYPE_SINGLE_SELECT, TYPE_MULTI_SELECT):
        opts = [
            AppTableFieldPropertyOption.builder().name(name).build()
            for name in spec.options
        ]
        return AppTableFieldProperty.builder().options(opts).build()
    return None


def check_response(label: str, resp) -> None:
    """飞书 SDK 调用统一错误检查。失败时打印 errcode + msg + log_id 后退出。"""
    if not resp.success():
        logger.error(
            "❌ %s 失败: code=%s msg=%s log_id=%s",
            label,
            resp.code,
            resp.msg,
            getattr(resp, "log_id", "?"),
        )
        # 把 raw 也打出来,方便诊断
        try:
            raw = json.loads(resp.raw.content) if resp.raw and resp.raw.content else {}
            logger.error("   raw: %s", json.dumps(raw, ensure_ascii=False)[:300])
        except Exception:
            pass
        sys.exit(2)
    logger.info("✅ %s", label)


# ──────────────────────── 主流程 ────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="创建设计部需求调研 bitable + 表单视图"
    )
    parser.add_argument(
        "--folder-token",
        default="",
        help="飞书云空间文件夹 token(可选)。不传时建在 app 自己空间,"
        "通过 share_url 访问。从文件夹 url 末尾取:"
        "https://o0ain5w98jh.feishu.cn/drive/folder/<folder_token>。"
        "注意:飞书云空间协作者不能加 app,要让 app 写入指定文件夹必须在"
        "开放平台后台配「数据权限」白名单,否则报 1254701。",
    )
    parser.add_argument(
        "--name",
        default="设计部 AI 工具需求调研",
        help="多维表格名称",
    )
    parser.add_argument(
        "--form-name",
        default="设计部 AI 工具需求调研问卷",
        help="表单视图名称",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划,不调用飞书 API",
    )
    args = parser.parse_args()

    # ===== 1. 凭证检查 =====
    if not is_enabled():
        logger.error(
            "飞书 hub 未启用。检查:1) DC_AGENT_ROOT 是否指向 /Users/dianchi/DC-Agent  "
            "2) 是否 source 了 ~/.dc-agent.env (FEISHU_APP_SECRET)"
        )
        return 1
    cred = get_credentials()
    logger.info(
        "凭证 OK: app_id=%s***, source=%s",
        cred.app_id[:10],
        Path(cred.source).name,
    )

    # ===== 2. dry-run 模式:打印计划就退出 =====
    logger.info("将创建多维表格: %r", args.name)
    logger.info("  → 字段数: %d", len(FIELDS))
    logger.info("  → 表单视图: %r", args.form_name)
    logger.info("  → folder_token: %s", args.folder_token)
    logger.info("字段清单:")
    for i, f in enumerate(FIELDS, 1):
        t_label = {1: "文本", 2: "数字", 3: "单选", 4: "多选"}[f.type]
        req = "★必填" if f.required else "选填"
        opts = f" [{len(f.options)} 项]" if f.options else ""
        logger.info("  %02d. [%s/%s]%s %s", i, t_label, req, opts, f.title)

    if args.dry_run:
        logger.info("--dry-run 模式,跳过 API 调用")
        return 0

    client = get_client()
    assert client is not None

    # ===== 3. 创建多维表格 app =====
    app_body = App.builder().name(args.name)
    if args.folder_token:
        app_body = app_body.folder_token(args.folder_token)
        logger.info("  → 建到指定文件夹: %s", args.folder_token)
    else:
        logger.info("  → 建在 app 自己空间(无 folder_token)")
    create_app_req = CreateAppRequest.builder().request_body(app_body.build()).build()
    resp = client.bitable.v1.app.create(create_app_req)
    check_response("创建多维表格", resp)
    app_token = resp.data.app.app_token
    default_table_id = resp.data.app.default_table_id
    app_url = resp.data.app.url
    logger.info(
        "  app_token=%s, default_table_id=%s, url=%s",
        app_token,
        default_table_id,
        app_url,
    )

    # ===== 4. 在默认表里挨个创建字段 =====
    # 默认表已经有一个 primary 字段(文本类型,名字"多行文本"或类似)。
    # 第一个题 Q1 也是文本必填,所以先 update primary 字段名字,后面字段挨个 create。
    # 但 update primary 的 API 比较绕(改 field_name 需要先 list 拿到 field_id),
    # 简化做法:把 Q1 也走 create 流程,然后接受表格里多一个"未使用的默认字段"。
    # 用户回收数据时只看 Q1–Q17,默认字段无所谓。
    # 反正主键字段在 bitable 里默认就是空的,表单视图也不会显示它。

    field_id_map: dict[int, str] = {}  # idx (0-based) → field_id
    for i, spec in enumerate(FIELDS):
        field_builder = AppTableField.builder().field_name(spec.title).type(spec.type)
        prop = build_field_property(spec)
        if prop is not None:
            field_builder = field_builder.property(prop)

        req = (
            CreateAppTableFieldRequest.builder()
            .app_token(app_token)
            .table_id(default_table_id)
            .request_body(field_builder.build())
            .build()
        )
        resp = client.bitable.v1.app_table_field.create(req)
        check_response(f"  字段 {i + 1:02d}/{len(FIELDS)} {spec.title[:30]}", resp)
        field_id_map[i] = resp.data.field.field_id
        # 飞书 bitable 字段创建有 QPS 限制,串行 + 小间隔
        time.sleep(0.15)

    # ===== 5. 创建表单视图 =====
    view_req = (
        CreateAppTableViewRequest.builder()
        .app_token(app_token)
        .table_id(default_table_id)
        .request_body(
            ReqView.builder().view_name(args.form_name).view_type("form").build()
        )
        .build()
    )
    resp = client.bitable.v1.app_table_view.create(view_req)
    check_response("创建表单视图", resp)
    form_id = resp.data.view.view_id
    logger.info("  form_id=%s", form_id)

    # ===== 6. 配置表单元信息:开放分享 + 设置描述 =====
    patch_form_req = (
        PatchAppTableFormRequest.builder()
        .app_token(app_token)
        .table_id(default_table_id)
        .form_id(form_id)
        .request_body(
            AppTableForm.builder()
            .name(args.form_name)
            .description(FORM_DESCRIPTION)
            .shared(True)
            .shared_limit("tenant_editable")  # 公司内可填
            .build()
        )
        .build()
    )
    resp = client.bitable.v1.app_table_form.patch(patch_form_req)
    check_response("开启表单分享 + 设置描述", resp)
    shared_url = resp.data.form.shared_url
    logger.info("  shared_url=%s", shared_url)

    # ===== 7. 配置每个表单字段的题目/描述/必填 =====
    # 飞书表单视图默认会把所有字段都 visible,但 description 和 required 要单独 patch。
    for i, spec in enumerate(FIELDS):
        field_id = field_id_map[i]
        patched = (
            AppTableFormPatchedField.builder()
            .title(spec.title)
            .description(spec.description)
            .required(spec.required)
            .visible(True)
            .build()
        )
        req = (
            PatchAppTableFormFieldRequest.builder()
            .app_token(app_token)
            .table_id(default_table_id)
            .form_id(form_id)
            .field_id(field_id)
            .request_body(patched)
            .build()
        )
        resp = client.bitable.v1.app_table_form_field.patch(req)
        check_response(f"  表单字段 {i + 1:02d}/{len(FIELDS)} 配置", resp)
        time.sleep(0.15)

    # ===== 8. 总结输出 =====
    print()
    print("=" * 60)
    print("🎉 全部完成")
    print("=" * 60)
    print(f"多维表格 URL: {app_url}")
    print(f"表单分享 URL: {shared_url}")
    print("  → 把表单 URL 发给设计部群即可")
    print("  → 数据会落到多维表格,后续可做透视/分析")
    print(f"app_token:    {app_token}")
    print(f"table_id:     {default_table_id}")
    print(f"form_id:      {form_id}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
