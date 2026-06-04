from __future__ import annotations

import io
import zipfile

import pytest

from astrbot.core.knowledge_base.parsers.pptx_parser import PPTXParser
from astrbot.core.knowledge_base.parsers.util import select_parser


def _make_pptx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
  <Override PartName="/ppt/notesSlides/notesSlide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>
</Types>
""",
        )
        archive.writestr(
            "ppt/slides/slide1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:sp>
        <p:txBody>
          <a:p><a:r><a:t>柳汽 Q2 视频选题</a:t></a:r></a:p>
          <a:p><a:r><a:t>乘龙商用车 12 条</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
""",
        )
        archive.writestr(
            "ppt/notesSlides/notesSlide1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
         xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:sp>
        <p:txBody>
          <a:p><a:r><a:t>备注：需要补齐商品痛点和前三秒钩子。</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:notes>
""",
        )
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_select_parser_supports_pptx():
    parser = await select_parser(".pptx")
    assert isinstance(parser, PPTXParser)


@pytest.mark.asyncio
async def test_pptx_parser_extracts_slides_and_notes():
    result = await PPTXParser().parse(_make_pptx_bytes(), "deck.pptx")

    assert "## Slide 1" in result.text
    assert "柳汽 Q2 视频选题" in result.text
    assert "乘龙商用车 12 条" in result.text
    assert "## Speaker Notes 1" in result.text
    assert "需要补齐商品痛点" in result.text
