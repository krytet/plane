# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import hashlib
import re
from dataclasses import dataclass, field
from html import unescape


CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
TAG_RE = re.compile(r"<[^>]+>")
MULTISPACE_RE = re.compile(r"[ \t]+")


@dataclass(frozen=True)
class JackrabbitDraft:
    summary: str
    description_markdown: str
    validation: list = field(default_factory=list)
    source_revision: str = ""

    @property
    def is_valid(self):
        return not self.validation

    @property
    def approval_hash(self):
        value = f"{self.summary}\n\n{self.description_markdown}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()


def html_to_text(value):
    if not value:
        return ""
    text = TAG_RE.sub("\n", str(value))
    text = unescape(text)
    lines = [MULTISPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def build_english_draft(*, summary, description_html, source_revision, extra_context=""):
    clean_summary = " ".join((summary or "").split()).strip()
    clean_description = html_to_text(description_html)
    if extra_context:
        clean_description = f"{clean_description}\n\n{html_to_text(extra_context)}".strip()
    validation = []
    if not clean_summary:
        validation.append({"code": "summary_required", "field": "summary"})
    if CYRILLIC_RE.search(clean_summary) or CYRILLIC_RE.search(clean_description):
        validation.append(
            {
                "code": "english_required",
                "field": "description",
                "message": "Review and translate the draft before submitting it to Jira.",
            }
        )
    if not clean_description:
        clean_description = "No source description was provided in Plane."
    markdown = (
        "# Short summary\n\n"
        f"{clean_summary or 'Missing English summary.'}\n\n"
        "# Context\n\n"
        "**Actor and workflow or system:** Confirm the affected actor and workflow.\n\n"
        "**Purpose:** Confirm the intended outcome before creating this Jira issue.\n\n"
        "**Concepts in dependency order:** None recorded.\n\n"
        "**Confirmed baseline and boundary:** See the source Plane work item.\n\n"
        "**Discovery starting point:** Review the linked Plane work item.\n\n"
        "# Full description\n\n"
        f"{clean_description}\n"
    )
    return JackrabbitDraft(
        summary=clean_summary,
        description_markdown=markdown,
        validation=validation,
        source_revision=source_revision,
    )


def _paragraph(text):
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text}] if text else [],
    }


def markdown_to_adf(markdown):
    """Convert the controlled Jackrabbit subset used by the draft editor."""
    content = []
    paragraph_lines = []

    def flush_paragraph():
        if paragraph_lines:
            content.append(_paragraph(" ".join(paragraph_lines).strip()))
            paragraph_lines.clear()

    for raw_line in (markdown or "").splitlines():
        line = raw_line.rstrip()
        if not line:
            flush_paragraph()
            continue
        if line.startswith("# "):
            flush_paragraph()
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": 1},
                    "content": [{"type": "text", "text": line[2:]}],
                }
            )
            continue
        if line.startswith("## "):
            flush_paragraph()
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": line[3:]}],
                }
            )
            continue
        paragraph_lines.append(line)
    flush_paragraph()
    return {"type": "doc", "version": 1, "content": content}


def validate_approval(*, draft, submitted_hash):
    if not draft:
        raise ValueError("Draft not found")
    expected = draft.approval_hash or draft.approved_by_id and draft.approval_hash
    if not expected:
        raise ValueError("Draft has not been approved")
    if submitted_hash != expected:
        raise ValueError("Draft changed after approval")
    return expected
