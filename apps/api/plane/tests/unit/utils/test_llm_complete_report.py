# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from hashlib import sha256

import pytest
from PIL import Image
from reportlab.pdfbase import pdfmetrics

from plane.utils.llm_complete_report import (
    LlmCompleteReportError,
    PdfArtifact,
    prepare_screenshot,
    render_llm_complete_pdf,
    validate_russian_text,
)


def _write_png(path):
    Image.new("RGB", (80, 40), (245, 247, 250)).save(path, format="PNG")
    return path


@pytest.mark.unit
def test_validate_russian_text_requires_non_empty_cyrillic():
    assert validate_russian_text("Progress", "  Готово: API и тесты\n") == "Готово: API и тесты"

    with pytest.raises(LlmCompleteReportError, match="required"):
        validate_russian_text("Progress", "   ")
    with pytest.raises(LlmCompleteReportError, match="Russian"):
        validate_russian_text("Progress", "Done and tested")


@pytest.mark.unit
def test_prepare_screenshot_rejects_non_image_and_unsupported_format(tmp_path):
    invalid = tmp_path / "invalid.png"
    invalid.write_bytes(b"not an image")
    with pytest.raises(LlmCompleteReportError, match="readable image"):
        prepare_screenshot(invalid)

    gif = tmp_path / "screenshot.gif"
    Image.new("RGB", (10, 10), "white").save(gif, format="GIF")
    with pytest.raises(LlmCompleteReportError, match="PNG, JPEG, or WebP"):
        prepare_screenshot(gif)


@pytest.mark.unit
def test_prepare_screenshot_uses_file_content_not_extension(tmp_path):
    screenshot = tmp_path / "screenshot.png"
    Image.new("RGB", (120, 60), "white").save(screenshot, format="JPEG")

    prepared = prepare_screenshot(screenshot)

    assert (prepared.width, prepared.height) == (120, 60)
    assert prepared.data.startswith(b"\xff\xd8")


@pytest.mark.unit
def test_render_llm_complete_pdf_is_deterministic_and_embeds_unicode_font(tmp_path):
    screenshot = _write_png(tmp_path / "screen.png")
    arguments = {
        "workspace_name": "Команда",
        "project_name": "Проверка LLM",
        "issue_identifier": "PLN-42",
        "issue_name": "Финальный отчёт",
        "task_description": "Нужно подготовить русский отчёт с проверкой.",
        "progress": "Отчёт сформирован. Скриншот приложен.",
        "screenshot_paths": [screenshot],
    }

    first = render_llm_complete_pdf(**arguments)
    second = render_llm_complete_pdf(**arguments)

    assert first.data.startswith(b"%PDF-")
    assert first.length == len(first.data) > 1_000
    assert first.sha256 == sha256(first.data).hexdigest()
    assert second.data == first.data
    assert 0x041F in pdfmetrics.getFont("Roboto").face.charToGlyph
    assert 0x044F in pdfmetrics.getFont("Roboto").face.charToGlyph


@pytest.mark.unit
def test_pdf_artifact_matches_checks_prefix_length_and_hash():
    artifact = PdfArtifact.from_bytes(b"%PDF-1.7\nreport")

    assert artifact.matches(b"%PDF-1.7\nreport")
    assert not artifact.matches(b"NOTPDF-1.7\nreport")
    assert not artifact.matches(b"%PDF-1.7\nrepor")
    assert not artifact.matches(b"%PDF-1.7\nother")
