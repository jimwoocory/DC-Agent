"""Generate real Office artifacts from research and file-processing results."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont as PdfTTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_DELIVERY_FORMATS = {
    "result_card",
    "docx",
    "xlsx",
    "pdf",
    "txt",
    "feishu_doc",
    "office_bundle",
}
_TASK_LABELS = {"research": "研究分析", "file": "文件处理"}
_DOCX_FONT_NAME = "Heiti SC"
_FIELD_LABELS = {
    "research_question": "研究问题",
    "research_scope": "范围 / 时间",
    "source_requirement": "来源要求",
    "source_material": "参考资料",
    "output_format": "内容结构 / 输出格式",
    "delivery_format": "交付文件",
    "file_goal": "处理目标",
    "file_source": "文件来源",
    "operation": "处理方式",
    "output_requirement": "其他要求",
    "model_choice": "模型选择",
}


def _clean_inline(value: str) -> str:
    """Remove lightweight Markdown markers while preserving visible text.

    Args:
        value: Markdown-like source text.

    Returns:
        Plain text suitable for Word, Excel, and PDF cells.
    """
    text = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", str(value or ""))
    text = re.sub(r"\[([^]]+)\]\(([^)]+)\)", r"\1（\2）", text)
    text = re.sub(r"(`{1,3}|\*\*|__|~~)", "", text)
    return text.replace("<br>", "\n").replace("<br/>", "\n").strip()


def _content_blocks(text: str) -> list[tuple[str, Any]]:
    """Parse a compact Markdown subset shared by all output renderers.

    Args:
        text: Final model result.

    Returns:
        Ordered paragraph, heading, bullet, and table blocks.
    """
    source = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.I).strip()
    lines = source.splitlines()
    blocks: list[tuple[str, Any]] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(("paragraph", _clean_inline(" ".join(paragraph))))
            paragraph.clear()

    index = 0
    while index < len(lines):
        raw = lines[index].strip()
        if not raw:
            flush_paragraph()
            index += 1
            continue
        if raw.startswith("|") and index + 1 < len(lines):
            separator = lines[index + 1].strip()
            if separator.startswith("|") and re.fullmatch(r"[|:\-\s]+", separator):
                flush_paragraph()
                rows = [[_clean_inline(cell) for cell in raw.strip("|").split("|")]]
                index += 2
                while index < len(lines) and lines[index].strip().startswith("|"):
                    rows.append(
                        [
                            _clean_inline(cell)
                            for cell in lines[index].strip().strip("|").split("|")
                        ]
                    )
                    index += 1
                width = max(len(row) for row in rows)
                rows = [row + [""] * (width - len(row)) for row in rows]
                blocks.append(("table", rows))
                continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", raw)
        if heading:
            flush_paragraph()
            blocks.append(
                ("heading", (len(heading.group(1)), _clean_inline(heading.group(2))))
            )
            index += 1
            continue
        bullet = re.match(r"^(?:[-*+]\s+|\d+[.)、]\s*)(.+)$", raw)
        if bullet:
            flush_paragraph()
            blocks.append(("bullet", _clean_inline(bullet.group(1))))
            index += 1
            continue
        paragraph.append(raw)
        index += 1
    flush_paragraph()
    return blocks or [("paragraph", _clean_inline(source))]


def _set_docx_font(
    run, *, size: float, bold: bool = False, color: str = "27323A"
) -> None:
    """Apply one cross-platform Chinese-capable Word run style.

    Args:
        run: python-docx run instance.
        size: Point size.
        bold: Whether the run is bold.
        color: Six-digit RGB color.

    Returns:
        None.
    """
    run.font.name = _DOCX_FONT_NAME
    run._element.rPr.rFonts.set(qn("w:eastAsia"), _DOCX_FONT_NAME)
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)


def _build_docx(
    path: Path,
    *,
    title: str,
    task_label: str,
    generated_at: str,
    task_data: dict[str, Any],
    blocks: list[tuple[str, Any]],
) -> None:
    """Write the standard business brief as a Word document.

    Args:
        path: Destination DOCX path.
        title: Visible document title.
        task_label: Research or file-processing label.
        generated_at: Local generation timestamp.
        task_data: Sanitized task settings.
        blocks: Parsed final-result blocks.

    Returns:
        None.
    """
    document = Document()
    section = document.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(2.4)
    section.right_margin = Cm(2.4)

    normal = document.styles["Normal"]
    normal.font.name = _DOCX_FONT_NAME
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), _DOCX_FONT_NAME)
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.line_spacing = 1.35

    masthead = document.add_table(rows=1, cols=2)
    masthead.autofit = False
    masthead.columns[0].width = Cm(10.8)
    masthead.columns[1].width = Cm(5.4)
    left = masthead.cell(0, 0).paragraphs[0]
    _set_docx_font(
        left.add_run("巅池 · 办公智能交付"), size=9, bold=True, color="147D70"
    )
    right = masthead.cell(0, 1).paragraphs[0]
    right.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _set_docx_font(right.add_run(generated_at), size=8, color="65736F")
    for cell in masthead.rows[0].cells:
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        borders = OxmlElement("w:tcBorders")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "8")
        bottom.set(qn("w:color"), "148D7D")
        borders.append(bottom)
        cell._tc.get_or_add_tcPr().append(borders)

    heading = document.add_paragraph()
    heading.paragraph_format.space_before = Pt(22)
    heading.paragraph_format.space_after = Pt(7)
    _set_docx_font(heading.add_run(title), size=22, bold=True, color="15332F")
    subtitle = document.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(18)
    _set_docx_font(
        subtitle.add_run(f"{task_label}｜自动生成，可继续补充要求后重新生成"),
        size=9,
        color="65736F",
    )

    for kind, payload in blocks:
        if kind == "heading":
            level, text = payload
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_before = Pt(14 if level <= 2 else 9)
            paragraph.paragraph_format.space_after = Pt(5)
            _set_docx_font(
                paragraph.add_run(text),
                size=15 if level == 1 else 12,
                bold=True,
                color="0B5E54",
            )
        elif kind == "bullet":
            paragraph = document.add_paragraph(style="List Bullet")
            paragraph.paragraph_format.left_indent = Cm(0.55)
            _set_docx_font(paragraph.add_run(payload), size=10.5)
        elif kind == "table":
            rows = payload
            table = document.add_table(rows=len(rows), cols=len(rows[0]))
            table.style = "Table Grid"
            table.autofit = True
            for row_index, row in enumerate(rows):
                for column_index, value in enumerate(row):
                    cell = table.cell(row_index, column_index)
                    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                    cell.text = ""
                    run = cell.paragraphs[0].add_run(value)
                    _set_docx_font(
                        run,
                        size=8.5,
                        bold=row_index == 0,
                        color="FFFFFF" if row_index == 0 else "27323A",
                    )
                    if row_index == 0:
                        shading = OxmlElement("w:shd")
                        shading.set(qn("w:fill"), "176E64")
                        cell._tc.get_or_add_tcPr().append(shading)
            document.add_paragraph().paragraph_format.space_after = Pt(2)
        else:
            paragraph = document.add_paragraph()
            _set_docx_font(paragraph.add_run(payload), size=10.5)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_docx_font(
        footer.add_run("巅池办公工作台 · 自动交付结果"), size=8, color="7B8884"
    )
    document.core_properties.title = title
    document.core_properties.subject = task_label
    document.core_properties.keywords = ", ".join(
        str(value)[:60] for value in task_data.values() if str(value or "").strip()
    )[:255]
    document.save(path)


def _build_xlsx(
    path: Path,
    *,
    title: str,
    task_label: str,
    generated_at: str,
    task_data: dict[str, Any],
    blocks: list[tuple[str, Any]],
) -> None:
    """Write structured results and task provenance to an Excel workbook.

    Args:
        path: Destination XLSX path.
        title: Visible workbook title.
        task_label: Research or file-processing label.
        generated_at: Local generation timestamp.
        task_data: Sanitized task settings.
        blocks: Parsed final-result blocks.

    Returns:
        None.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "分析结果" if task_label == "研究分析" else "处理结果"
    sheet.sheet_view.showGridLines = False
    sheet.merge_cells("A1:F1")
    sheet["A1"] = title
    sheet["A1"].font = Font(name="Arial Unicode MS", size=18, bold=True, color="FFFFFF")
    sheet["A1"].fill = PatternFill("solid", fgColor="153F3A")
    sheet["A1"].alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 34
    sheet.merge_cells("A2:F2")
    sheet["A2"] = f"{task_label}｜生成时间：{generated_at}｜可继续补充要求后重新生成"
    sheet["A2"].font = Font(name="Arial Unicode MS", size=9, color="536965")
    sheet["A2"].fill = PatternFill("solid", fgColor="EAF5F2")
    sheet["A2"].alignment = Alignment(vertical="center")

    header_fill = PatternFill("solid", fgColor="176E64")
    header_font = Font(name="Arial Unicode MS", size=10, bold=True, color="FFFFFF")
    body_font = Font(name="Arial Unicode MS", size=10, color="27323A")
    thin = Side(style="thin", color="D8E3DF")
    row_index = 4
    table_found = False
    for kind, payload in blocks:
        if kind == "table":
            table_found = True
            for source_row_index, row in enumerate(payload):
                for column_index, value in enumerate(row, start=1):
                    cell = sheet.cell(row=row_index, column=column_index, value=value)
                    cell.font = header_font if source_row_index == 0 else body_font
                    if source_row_index == 0:
                        cell.fill = header_fill
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                    cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
                row_index += 1
            row_index += 1
    if not table_found:
        headers = ("序号", "类型", "结果内容")
        for column_index, value in enumerate(headers, start=1):
            cell = sheet.cell(row=row_index, column=column_index, value=value)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        row_index += 1
        result_index = 1
        for kind, payload in blocks:
            if kind == "heading":
                _level, value = payload
            elif kind == "table":
                continue
            else:
                value = payload
            sheet.cell(row=row_index, column=1, value=result_index)
            sheet.cell(
                row=row_index,
                column=2,
                value={"heading": "标题", "bullet": "要点"}.get(kind, "正文"),
            )
            sheet.cell(row=row_index, column=3, value=value)
            for column_index in range(1, 4):
                cell = sheet.cell(row=row_index, column=column_index)
                cell.font = body_font
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
            result_index += 1
            row_index += 1

    for column_index in range(1, max(6, sheet.max_column) + 1):
        values = [
            str(sheet.cell(row=row, column=column_index).value or "")
            for row in range(1, sheet.max_row + 1)
        ]
        sheet.column_dimensions[get_column_letter(column_index)].width = min(
            48, max(10, max((len(value) for value in values), default=10) + 2)
        )
    sheet.freeze_panes = "A4"
    sheet.auto_filter.ref = f"A4:{get_column_letter(sheet.max_column)}{sheet.max_row}"

    metadata = workbook.create_sheet("任务信息")
    metadata.sheet_view.showGridLines = False
    metadata.append(["字段", "内容"])
    for cell in metadata[1]:
        cell.font = header_font
        cell.fill = header_fill
    for key, value in task_data.items():
        if str(value or "").strip():
            metadata.append([_FIELD_LABELS.get(str(key), str(key)), str(value)])
    metadata.column_dimensions["A"].width = 24
    metadata.column_dimensions["B"].width = 78
    for row in metadata.iter_rows(min_row=2):
        for cell in row:
            cell.font = body_font
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    workbook.save(path)


def _build_pdf(
    path: Path,
    *,
    title: str,
    task_label: str,
    generated_at: str,
    blocks: list[tuple[str, Any]],
) -> None:
    """Write a Unicode-capable A4 PDF business brief.

    Args:
        path: Destination PDF path.
        title: Visible PDF title.
        task_label: Research or file-processing label.
        generated_at: Local generation timestamp.
        blocks: Parsed final-result blocks.

    Returns:
        None.
    """
    font_name = "OfficeCJK"
    try:
        pdfmetrics.getFont(font_name)
    except KeyError:
        font_paths = (
            Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
            Path("/Library/Fonts/Arial Unicode.ttf"),
            Path("/usr/share/fonts/truetype/arphic/ukai.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        )
        font_registered = False
        for font_path in font_paths:
            if not font_path.is_file():
                continue
            try:
                pdfmetrics.registerFont(PdfTTFont(font_name, str(font_path)))
                font_registered = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not font_registered:
            font_name = "STSong-Light"
            try:
                pdfmetrics.getFont(font_name)
            except KeyError:
                pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    styles = getSampleStyleSheet()
    brand = ParagraphStyle(
        "OfficeBrand",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=8.5,
        textColor=colors.HexColor("#147D70"),
        leading=12,
        spaceAfter=8,
    )
    title_style = ParagraphStyle(
        "OfficeTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=20,
        textColor=colors.HexColor("#15332F"),
        leading=28,
        alignment=TA_LEFT,
        spaceAfter=7,
    )
    meta_style = ParagraphStyle(
        "OfficeMeta",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=8.5,
        textColor=colors.HexColor("#65736F"),
        leading=13,
        spaceAfter=14,
    )
    body_style = ParagraphStyle(
        "OfficeBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10,
        textColor=colors.HexColor("#27323A"),
        leading=16,
        spaceAfter=7,
    )
    heading_style = ParagraphStyle(
        "OfficeHeading",
        parent=body_style,
        fontSize=13,
        textColor=colors.HexColor("#0B5E54"),
        leading=19,
        spaceBefore=10,
        spaceAfter=5,
    )
    bullet_style = ParagraphStyle(
        "OfficeBullet",
        parent=body_style,
        leftIndent=12,
        firstLineIndent=-7,
    )
    table_header_style = ParagraphStyle(
        "OfficeTableHeader",
        parent=body_style,
        fontSize=8.5,
        textColor=colors.white,
        leading=12,
    )
    story = [
        Paragraph("巅池 · 办公智能交付", brand),
        Paragraph(escape(title), title_style),
        Paragraph(
            escape(f"{task_label}｜生成时间：{generated_at}｜可继续补充要求后重新生成"),
            meta_style,
        ),
    ]
    for kind, payload in blocks:
        if kind == "heading":
            _level, value = payload
            story.append(Paragraph(escape(value), heading_style))
        elif kind == "bullet":
            story.append(Paragraph(f"•&nbsp;&nbsp;{escape(payload)}", bullet_style))
        elif kind == "table":
            rows = [
                [
                    Paragraph(
                        escape(value),
                        table_header_style if row_index == 0 else body_style,
                    )
                    for value in row
                ]
                for row_index, row in enumerate(payload)
            ]
            widths = [(A4[0] - 40 * mm) / len(rows[0])] * len(rows[0])
            table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#176E64")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, -1), font_name),
                        ("FONTSIZE", (0, 0), (-1, -1), 8),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D8E3DF")),
                        (
                            "ROWBACKGROUNDS",
                            (0, 1),
                            (-1, -1),
                            [colors.white, colors.HexColor("#F5F9F7")],
                        ),
                        ("LEFTPADDING", (0, 0), (-1, -1), 5),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            story.extend([table, Spacer(1, 7)])
        else:
            story.append(Paragraph(escape(payload).replace("\n", "<br/>"), body_style))

    pdf = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="巅池办公工作台",
    )
    pdf.build(story)


def generate_office_deliverables(
    task_type: str,
    task_data: dict[str, Any],
    final_text: str,
    *,
    output_dir: Path,
) -> dict[str, Path]:
    """Generate the exact files selected in an Office workbench task.

    Result-card and Feishu-document delivery are handled by the messaging
    plugin, while Word, Excel, PDF, and text files are created here.

    Args:
        task_type: ``research`` or ``file``.
        task_data: Sanitized Office task fields.
        final_text: Final model result to package.
        output_dir: Runtime directory for generated files.

    Returns:
        Generated local paths keyed by stable delivery-format names.

    Raises:
        ValueError: If the task, output format, or final result is invalid.
        OSError: If the output directory or artifact cannot be written.
    """
    if task_type not in _TASK_LABELS:
        raise ValueError("仅支持研究分析和文件处理交付")
    clean_text = str(final_text or "").strip()
    if not clean_text:
        raise ValueError("模型未返回可交付内容")
    delivery_format = str(
        (
            task_data.get("delivery_format")
            if task_type == "research"
            else task_data.get("output_format")
        )
        or ""
    ).strip()
    if not delivery_format:
        delivery_format = "result_card" if task_type == "research" else "docx"
    if delivery_format not in _DELIVERY_FORMATS:
        raise ValueError("交付格式不受支持")
    if delivery_format in {"result_card", "feishu_doc"}:
        return {}

    output_dir.mkdir(parents=True, exist_ok=True)
    question = str(
        task_data.get("research_question")
        or task_data.get("file_goal")
        or "办公任务结果"
    ).strip()
    safe_name = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", question).strip("-_")
    safe_name = safe_name[:48] or "office-result"
    task_label = _TASK_LABELS[task_type]
    generated_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    blocks = _content_blocks(clean_text)
    title = question[:120]
    formats = (
        {"docx", "xlsx", "pdf"}
        if delivery_format == "office_bundle"
        else {delivery_format}
    )
    artifacts: dict[str, Path] = {}
    for item in formats:
        path = output_dir / f"{safe_name}-{task_label}.{item}"
        if item == "docx":
            _build_docx(
                path,
                title=title,
                task_label=task_label,
                generated_at=generated_at,
                task_data=task_data,
                blocks=blocks,
            )
        elif item == "xlsx":
            _build_xlsx(
                path,
                title=title,
                task_label=task_label,
                generated_at=generated_at,
                task_data=task_data,
                blocks=blocks,
            )
        elif item == "pdf":
            _build_pdf(
                path,
                title=title,
                task_label=task_label,
                generated_at=generated_at,
                blocks=blocks,
            )
        else:
            path.write_text(
                f"{title}\n{task_label}｜生成时间：{generated_at}\n\n{clean_text}\n",
                encoding="utf-8",
            )
        artifacts[item] = path
    return artifacts
