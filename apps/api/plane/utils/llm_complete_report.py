# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
from typing import Sequence
from xml.sax.saxutils import escape

from font_roboto import Roboto, RobotoBold
from PIL import Image, ImageOps, UnidentifiedImageError
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image as PdfImage
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer


PDF_PREFIX = b"%PDF-"
PDF_MIME_TYPE = "application/pdf"
_ALLOWED_SCREENSHOT_FORMATS = {"JPEG", "PNG", "WEBP"}
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
_MAX_SCREENSHOT_PIXELS = 50_000_000
_MAX_TEXT_LENGTH = 20_000
_FONT_FAMILY = "Roboto"
_FONTS_REGISTERED = False


class LlmCompleteReportError(ValueError):
    """Raised when the supplied report inputs cannot be rendered safely."""


@dataclass(frozen=True)
class PdfArtifact:
    data: bytes
    sha256: str
    length: int

    @classmethod
    def from_bytes(cls, data: bytes) -> PdfArtifact:
        if not data.startswith(PDF_PREFIX):
            raise LlmCompleteReportError("Generated data is not a PDF.")
        return cls(data=data, sha256=sha256(data).hexdigest(), length=len(data))

    def matches(self, candidate: bytes) -> bool:
        return (
            candidate.startswith(PDF_PREFIX)
            and len(candidate) == self.length
            and sha256(candidate).hexdigest() == self.sha256
        )


@dataclass(frozen=True)
class PreparedScreenshot:
    name: str
    data: bytes
    width: int
    height: int


def validate_russian_text(label: str, value: str) -> str:
    normalized = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise LlmCompleteReportError(f"{label} is required.")
    if len(normalized) > _MAX_TEXT_LENGTH:
        raise LlmCompleteReportError(f"{label} must be at most {_MAX_TEXT_LENGTH} characters.")
    if "\x00" in normalized:
        raise LlmCompleteReportError(f"{label} contains a null character.")
    if _CYRILLIC_RE.search(normalized) is None:
        raise LlmCompleteReportError(f"{label} must contain Russian text.")
    return normalized


def prepare_screenshot(path: str | Path) -> PreparedScreenshot:
    source_path = Path(path).expanduser()
    try:
        source_path = source_path.resolve(strict=True)
    except OSError as exc:
        raise LlmCompleteReportError(f"Screenshot does not exist: {source_path}") from exc
    if not source_path.is_file():
        raise LlmCompleteReportError(f"Screenshot is not a file: {source_path}")

    size = source_path.stat().st_size
    if size <= 0 or size > _MAX_SCREENSHOT_BYTES:
        raise LlmCompleteReportError(
            f"Screenshot must be between 1 byte and {_MAX_SCREENSHOT_BYTES} bytes: {source_path}"
        )

    try:
        with Image.open(source_path) as source:
            image_format = source.format
            width, height = source.size
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError) as exc:
        raise LlmCompleteReportError(f"Screenshot is not a readable image: {source_path}") from exc

    if image_format not in _ALLOWED_SCREENSHOT_FORMATS:
        raise LlmCompleteReportError(f"Screenshot must be PNG, JPEG, or WebP: {source_path}")
    if width <= 0 or height <= 0 or width * height > _MAX_SCREENSHOT_PIXELS:
        raise LlmCompleteReportError(f"Screenshot dimensions are not allowed: {source_path}")

    try:
        with Image.open(source_path) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            image.thumbnail((1800, 2600), Image.Resampling.LANCZOS)

            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")

            output = BytesIO()
            image.save(output, format="JPEG", quality=86, optimize=True)
            prepared_width, prepared_height = image.size
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError) as exc:
        raise LlmCompleteReportError(f"Screenshot could not be processed: {source_path}") from exc

    return PreparedScreenshot(
        name=source_path.name,
        data=output.getvalue(),
        width=prepared_width,
        height=prepared_height,
    )


def _register_fonts() -> None:
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return

    registered = set(pdfmetrics.getRegisteredFontNames())
    if _FONT_FAMILY not in registered:
        pdfmetrics.registerFont(TTFont(_FONT_FAMILY, Roboto))
    if "RobotoBold" not in registered:
        pdfmetrics.registerFont(TTFont("RobotoBold", RobotoBold))
    pdfmetrics.registerFontFamily(
        _FONT_FAMILY,
        normal=_FONT_FAMILY,
        bold="RobotoBold",
        italic=_FONT_FAMILY,
        boldItalic="RobotoBold",
    )
    _FONTS_REGISTERED = True


def _paragraph(value: str) -> str:
    return escape(value).replace("\n", "<br/>")


def _fit_image(screenshot: PreparedScreenshot, max_width: float, max_height: float) -> tuple[float, float]:
    scale = min(max_width / screenshot.width, max_height / screenshot.height)
    return screenshot.width * scale, screenshot.height * scale


def render_llm_complete_pdf(
    *,
    workspace_name: str,
    project_name: str,
    issue_identifier: str,
    issue_name: str,
    task_description: str,
    progress: str,
    screenshot_paths: Sequence[str | Path],
) -> PdfArtifact:
    """Render a deterministic Russian report with embedded screenshot evidence."""

    task_description = validate_russian_text("Task description", task_description)
    progress = validate_russian_text("Progress", progress)
    if not screenshot_paths:
        raise LlmCompleteReportError("At least one screenshot is required.")

    screenshots = [prepare_screenshot(path) for path in screenshot_paths]
    _register_fonts()

    title_style = ParagraphStyle(
        "LlmCompleteTitle",
        fontName=_FONT_FAMILY,
        fontSize=22,
        leading=27,
        textColor=colors.HexColor("#111827"),
        spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        "LlmCompleteSubtitle",
        fontName=_FONT_FAMILY,
        fontSize=11,
        leading=16,
        textColor=colors.HexColor("#4B5563"),
        spaceAfter=16,
    )
    heading_style = ParagraphStyle(
        "LlmCompleteHeading",
        fontName="RobotoBold",
        fontSize=14,
        leading=19,
        textColor=colors.HexColor("#111827"),
        spaceBefore=10,
        spaceAfter=7,
    )
    body_style = ParagraphStyle(
        "LlmCompleteBody",
        fontName=_FONT_FAMILY,
        fontSize=10.5,
        leading=16,
        textColor=colors.HexColor("#1F2937"),
        spaceAfter=8,
    )
    meta_style = ParagraphStyle(
        "LlmCompleteMeta",
        fontName=_FONT_FAMILY,
        fontSize=9.5,
        leading=14,
        textColor=colors.HexColor("#374151"),
        spaceAfter=4,
    )
    caption_style = ParagraphStyle(
        "LlmCompleteCaption",
        fontName=_FONT_FAMILY,
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#4B5563"),
        alignment=1,
        spaceBefore=6,
    )

    story = [
        Paragraph("LLM Complete", title_style),
        Paragraph("Итоговый отчёт для проверки человеком", subtitle_style),
        Paragraph("Рабочее пространство", heading_style),
        Paragraph(_paragraph(workspace_name), meta_style),
        Paragraph("Проект", heading_style),
        Paragraph(_paragraph(project_name), meta_style),
        Paragraph("Работа", heading_style),
        Paragraph(_paragraph(f"{issue_identifier} · {issue_name}"), meta_style),
        Paragraph("Состояние после публикации", heading_style),
        Paragraph(
            _paragraph("LLM Complete · группа STARTED · ожидает проверки человеком"),
            meta_style,
        ),
        Paragraph("Описание задачи", heading_style),
        Paragraph(_paragraph(task_description), body_style),
        Paragraph("Текущий прогресс", heading_style),
        Paragraph(_paragraph(progress), body_style),
    ]

    for index, screenshot in enumerate(screenshots, start=1):
        story.extend(
            [
                PageBreak(),
                Paragraph(f"Скриншот {index}", heading_style),
                Spacer(1, 4),
            ]
        )
        width, height = _fit_image(screenshot, 260 * mm, 150 * mm)
        story.extend(
            [
                PdfImage(BytesIO(screenshot.data), width=width, height=height),
                Paragraph(_paragraph(f"Файл: {screenshot.name}"), caption_style),
            ]
        )

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        invariant=1,
        title="LLM Complete",
        author="Plane",
    )
    document.build(story)
    return PdfArtifact.from_bytes(output.getvalue())
