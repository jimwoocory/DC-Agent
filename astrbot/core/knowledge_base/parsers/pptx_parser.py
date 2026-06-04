"""PPTX document parser.

Extracts text from PowerPoint OOXML packages without requiring external
conversion tools. This covers slide text, tables, and speaker notes.
"""

from __future__ import annotations

import io
import re
import zipfile
from html import unescape
from xml.etree import ElementTree

from astrbot.core.knowledge_base.parsers.base import BaseParser, ParseResult

_TEXT_TAG_RE = re.compile(r"\{[^}]+}t$")
_SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml$")
_NOTES_RE = re.compile(r"ppt/notesSlides/notesSlide(\d+)\.xml$")


class PPTXParser(BaseParser):
    """Parse a PPTX file into plain text."""

    async def parse(self, file_content: bytes, file_name: str) -> ParseResult:
        try:
            archive = zipfile.ZipFile(io.BytesIO(file_content))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Invalid PPTX file: {file_name}") from exc

        with archive:
            names = archive.namelist()
            if "[Content_Types].xml" not in names or not any(
                name.startswith("ppt/slides/slide") for name in names
            ):
                raise ValueError(f"Invalid PPTX file: {file_name}")

            parts: list[str] = []
            for slide_name in _sorted_pptx_parts(names, _SLIDE_RE):
                slide_text = _extract_xml_text(archive.read(slide_name))
                slide_no = _part_number(slide_name, _SLIDE_RE)
                if slide_text:
                    parts.append(f"## Slide {slide_no}\n{slide_text}")

            for notes_name in _sorted_pptx_parts(names, _NOTES_RE):
                notes_text = _extract_xml_text(archive.read(notes_name))
                slide_no = _part_number(notes_name, _NOTES_RE)
                if notes_text:
                    parts.append(f"## Speaker Notes {slide_no}\n{notes_text}")

        return ParseResult(text="\n\n".join(parts), media=[])


def _sorted_pptx_parts(names: list[str], pattern: re.Pattern[str]) -> list[str]:
    return sorted(
        (name for name in names if pattern.match(name)),
        key=lambda name: _part_number(name, pattern),
    )


def _part_number(name: str, pattern: re.Pattern[str]) -> int:
    match = pattern.match(name)
    if not match:
        return 0
    return int(match.group(1))


def _extract_xml_text(xml_bytes: bytes) -> str:
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return ""

    lines: list[str] = []
    current: list[str] = []
    for elem in root.iter():
        if not _TEXT_TAG_RE.search(elem.tag):
            continue
        text = unescape((elem.text or "").strip())
        if not text:
            continue
        current.append(text)
        if text.endswith((".", "。", "!", "！", "?", "？", ":", "：")):
            lines.append(" ".join(current).strip())
            current = []
    if current:
        lines.append(" ".join(current).strip())

    return "\n".join(line for line in lines if line)
