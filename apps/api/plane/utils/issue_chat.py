# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import hashlib
import json
import re
from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from bs4 import BeautifulSoup
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.utils import timezone

from plane.db.models import Issue, IssueChatBinding, IssueLink


PROGRESS_BLOCK_TYPE = "chat-progress"
PROGRESS_BLOCK_ID_PREFIX = "plane-chat-progress-"
PROGRESS_KEYS = ("done", "now", "waiting")
CODEX_NATIVE_URL_PREFIX = "codex://threads/"
PROVIDER_HOSTS = {
    IssueChatBinding.Provider.CODEX: frozenset(
        {"chatgpt.com", "www.chatgpt.com", "chat.openai.com", "www.chat.openai.com"}
    ),
    IssueChatBinding.Provider.CHATGPT: frozenset(
        {"chatgpt.com", "www.chatgpt.com", "chat.openai.com", "www.chat.openai.com"}
    ),
}

_DIV_START_RE = re.compile(r"<div\b[^>]*>", re.IGNORECASE)
_DIV_END_RE = re.compile(r"</div\s*>", re.IGNORECASE)
_ATTRIBUTE_RE = re.compile(r"""([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_WHITESPACE_RE = re.compile(r"\s+")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_CODEX_NATIVE_URL_RE = re.compile(
    r"^codex://threads/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


class IssueChatError(Exception):
    pass


class IssueChatInputError(IssueChatError):
    def __init__(self, field, message):
        self.field = field
        self.message = message
        super().__init__(message)


class IssueChatNotFound(IssueChatError):
    pass


class IssueChatConflict(IssueChatError):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


class IssueChatProgressConflict(IssueChatConflict):
    pass


@dataclass(frozen=True)
class IssueChatSyncResult:
    binding: IssueChatBinding
    created: bool
    applied: bool


def progress_block_id(binding_id):
    return f"{PROGRESS_BLOCK_ID_PREFIX}{binding_id}"


def _normalize_item(item):
    return _WHITESPACE_RE.sub(" ", str(item)).strip()


def normalize_progress(progress):
    normalized = {}
    for key in PROGRESS_KEYS:
        items = []
        for item in (progress or {}).get(key, []):
            normalized_item = _normalize_item(item)
            if normalized_item:
                items.append(normalized_item)
        normalized[key] = items
    return normalized


def render_progress_block(binding_id, progress):
    marker_id = progress_block_id(binding_id)
    normalized_progress = normalize_progress(progress)
    rendered_items = []
    for key, label in (("done", "Done"), ("now", "Now"), ("waiting", "Waiting")):
        value = " · ".join(escape(item, quote=False) for item in normalized_progress[key]) or "—"
        rendered_items.append(f"<p><strong>{label}:</strong> {value}</p>")

    return f'<div data-node-type="{PROGRESS_BLOCK_TYPE}" id="{marker_id}">' + "".join(rendered_items) + "</div>"


def normalize_progress_block(block_html):
    """Return the semantic signature that survives editor HTML serialization."""

    soup = BeautifulSoup(block_html, "html.parser")
    root = soup.find("div")
    if root is None:
        return ""

    nodes = []
    for child in root.find_all(recursive=False):
        text = _WHITESPACE_RE.sub(" ", child.get_text(" ", strip=True).replace("\xa0", " ")).strip()
        nodes.append([child.name, text])

    return json.dumps(
        {
            "node_type": root.get("data-node-type"),
            "id": root.get("id"),
            "nodes": nodes,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def progress_block_hash(block_html):
    normalized = normalize_progress_block(block_html)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def payload_hash(payload):
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_provider_url(provider, value):
    """Validate and canonicalize a provider-hosted web link for storage."""

    normalized = (value or "").strip()
    if not normalized:
        raise IssueChatInputError("url", "A chat URL is required.")

    try:
        parts = urlsplit(normalized)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise IssueChatInputError("url", "Enter a valid chat URL.") from exc

    if parts.scheme not in ("http", "https") or not parts.netloc or not hostname:
        raise IssueChatInputError("url", "Enter a valid http:// or https:// chat URL.")
    if parts.username or parts.password:
        raise IssueChatInputError("url", "Chat URLs cannot contain credentials.")

    hostname = hostname.rstrip(".").lower()
    allowed_hosts = PROVIDER_HOSTS.get(provider, frozenset())
    is_local_debug = settings.DEBUG and hostname in {"localhost", "127.0.0.1", "::1"}
    if hostname not in allowed_hosts and not is_local_debug:
        raise IssueChatInputError("url", "The URL must belong to the selected chat provider.")
    if not is_local_debug and parts.scheme != "https":
        raise IssueChatInputError("url", "Provider chat URLs must use HTTPS.")
    if not is_local_debug and port not in (None, 443):
        raise IssueChatInputError("url", "Provider chat URLs must use the default HTTPS port.")

    netloc = f"[{hostname}]" if ":" in hostname else hostname
    normalized_url = urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, parts.fragment))
    try:
        URLValidator(schemes=["http", "https"])(normalized_url)
    except ValidationError as exc:
        raise IssueChatInputError("url", "Enter a valid chat URL.") from exc
    return normalized_url


def normalize_codex_thread_id(value):
    """Return a canonical UUID string only for an exact UUID thread identifier."""

    normalized = (value or "").strip()
    if not _UUID_RE.fullmatch(normalized):
        return None
    try:
        return str(UUID(normalized))
    except ValueError:
        return None


def codex_thread_deep_link(thread_id):
    canonical_thread_id = normalize_codex_thread_id(thread_id)
    if canonical_thread_id is None:
        return None
    return f"{CODEX_NATIVE_URL_PREFIX}{canonical_thread_id}"


def normalize_chat_binding_url(provider, thread_id, value):
    """Resolve a provider web link or an exact native Codex thread deep link."""

    normalized = (value or "").strip()
    native_url = codex_thread_deep_link(thread_id) if provider == IssueChatBinding.Provider.CODEX else None

    if provider == IssueChatBinding.Provider.CODEX and normalized.lower().startswith("codex:"):
        if native_url is None:
            raise IssueChatInputError("url", "Native Codex links require a UUID thread ID.")
        match = _CODEX_NATIVE_URL_RE.fullmatch(normalized)
        if match is None or match.group(1).lower() != native_url.removeprefix(CODEX_NATIVE_URL_PREFIX):
            raise IssueChatInputError("url", "Native Codex links must match codex://threads/{thread_id}.")
        return native_url

    if not normalized:
        if native_url is not None:
            return native_url
        raise IssueChatInputError(
            "url",
            "A chat URL is required. Use codex://threads/<UUID> for a native Codex thread.",
        )

    return normalize_provider_url(provider, normalized)


def _tag_attributes(tag_html):
    return {
        name.lower(): double_quoted or single_quoted
        for name, double_quoted, single_quoted in _ATTRIBUTE_RE.findall(tag_html)
    }


def _generated_block_bounds(description_html):
    matches = []
    for match in _DIV_START_RE.finditer(description_html):
        attributes = _tag_attributes(match.group(0))
        marker_id = attributes.get("id", "")
        is_progress_type = attributes.get("data-node-type") == PROGRESS_BLOCK_TYPE
        has_progress_id = marker_id.startswith(PROGRESS_BLOCK_ID_PREFIX)
        if not is_progress_type and not has_progress_id:
            continue
        if not is_progress_type or not has_progress_id:
            raise IssueChatProgressConflict(
                "malformed_block",
                "The generated progress block is malformed.",
            )
        end_match = _DIV_END_RE.search(description_html, match.end())
        if end_match is None or _DIV_START_RE.search(description_html, match.end(), end_match.start()):
            raise IssueChatProgressConflict("malformed_block", "The generated progress block is malformed.")
        matches.append((match.start(), end_match.end(), marker_id))
    return matches


def _matching_block_bounds(description_html, marker_id):
    return [match for match in _generated_block_bounds(description_html) if match[2] == marker_id]


def merge_progress_block(description_html, binding_id, progress, expected_block_hash=""):
    """Insert or replace one generated block without rewriting surrounding manual HTML."""

    description_html = description_html or "<p></p>"
    marker_id = progress_block_id(binding_id)
    matches = _matching_block_bounds(description_html, marker_id)
    rendered_block = render_progress_block(binding_id, progress)

    if len(matches) > 1:
        raise IssueChatProgressConflict("duplicate_block", "The generated progress block appears more than once.")

    if not matches:
        if expected_block_hash:
            raise IssueChatProgressConflict(
                "missing_block",
                "The generated progress block was removed or changed; refusing to recreate it.",
            )
        return f"{description_html}{rendered_block}", progress_block_hash(rendered_block)

    start, end, _ = matches[0]
    current_block = description_html[start:end]
    if not expected_block_hash:
        raise IssueChatProgressConflict(
            "unexpected_block",
            "A generated progress block already exists without a recorded sync state.",
        )
    if progress_block_hash(current_block) != expected_block_hash:
        raise IssueChatProgressConflict(
            "manual_edit",
            "The generated progress block was edited manually; refusing to overwrite it.",
        )

    updated_description = f"{description_html[:start]}{rendered_block}{description_html[end:]}"
    return updated_description, progress_block_hash(rendered_block)


def merge_latest_chat_blocks(issue, submitted_description_html):
    """Keep current server-generated blocks while applying a manual description edit.

    Callers must hold a row lock on ``issue``. Chat sync acquires the same lock
    before it reads or changes generated blocks, so this keeps both a stale
    editor save and a concurrent sync from losing the other's changes.
    """

    bindings = list(
        IssueChatBinding.objects.filter(
            workspace_id=issue.workspace_id,
            project_id=issue.project_id,
            issue_id=issue.id,
        ).order_by("created_at", "id")
    )
    current_description = issue.description_html or "<p></p>"
    current_blocks = {}
    for start, end, marker_id in _generated_block_bounds(current_description):
        if marker_id in current_blocks:
            raise IssueChatProgressConflict(
                "duplicate_block",
                "The generated progress block appears more than once.",
            )
        current_blocks[marker_id] = current_description[start:end]
    latest_blocks = {}

    for binding in bindings:
        marker_id = progress_block_id(binding.id)
        try:
            current_block = current_blocks[marker_id]
        except KeyError as exc:
            if binding.generated_block_hash:
                raise IssueChatProgressConflict(
                    "missing_block",
                    "The generated progress block was removed or changed; refusing to update the description.",
                ) from exc
            continue

        if not binding.generated_block_hash:
            raise IssueChatProgressConflict(
                "unexpected_block",
                "A generated progress block exists without a recorded sync state.",
            )
        if progress_block_hash(current_block) != binding.generated_block_hash:
            raise IssueChatProgressConflict(
                "manual_edit",
                "The generated progress block was edited manually; refusing to update the description.",
            )
        latest_blocks[marker_id] = current_block

    submitted_description = submitted_description_html or "<p></p>"
    submitted_blocks = _generated_block_bounds(submitted_description)
    seen_markers = set()
    for _, _, marker_id in submitted_blocks:
        if marker_id in seen_markers:
            raise IssueChatProgressConflict(
                "duplicate_block",
                "The generated progress block appears more than once.",
            )
        if marker_id not in latest_blocks:
            raise IssueChatProgressConflict(
                "unknown_block",
                "The description contains a generated progress block that does not belong to this work item.",
            )
        seen_markers.add(marker_id)

    merged_description = submitted_description
    for start, end, marker_id in sorted(submitted_blocks, key=lambda match: match[0], reverse=True):
        merged_description = (
            f"{merged_description[:start]}{latest_blocks[marker_id]}{merged_description[end:]}"
        )

    for marker_id in latest_blocks:
        if marker_id not in seen_markers:
            merged_description = f"{merged_description}{latest_blocks[marker_id]}"

    return merged_description


def _issue_queryset(workspace_slug, project_id):
    return Issue.objects.filter(
        workspace__slug=workspace_slug,
        project_id=project_id,
        project__archived_at__isnull=True,
    )


def list_issue_chat_bindings(workspace_slug, project_id, issue_id):
    if not _issue_queryset(workspace_slug, project_id).filter(pk=issue_id).exists():
        raise IssueChatNotFound

    return (
        IssueChatBinding.objects.filter(
            workspace__slug=workspace_slug,
            project_id=project_id,
            issue_id=issue_id,
        )
        .select_related("link")
        .order_by("-created_at")
    )


def _create_binding(issue, actor, provider, thread_id):
    try:
        with transaction.atomic():
            return IssueChatBinding.objects.create(
                workspace=issue.workspace,
                project=issue.project,
                issue=issue,
                provider=provider,
                thread_id=thread_id,
                created_by_id=actor.id,
                progress={},
            )
    except IntegrityError as exc:
        raise IssueChatConflict(
            "thread_already_bound",
            "This provider thread is already bound to another work item.",
        ) from exc


def sync_issue_chat_binding(
    *,
    workspace_slug,
    project_id,
    issue_id,
    actor,
    provider,
    thread_id,
    cursor,
    progress,
    url=None,
    title=None,
):
    """Idempotently apply one provider cursor to a work item's progress block."""

    normalized_progress = normalize_progress(progress)
    normalized_thread_id = (thread_id or "").strip()
    requested_url = normalize_chat_binding_url(provider, normalized_thread_id, url)
    requested_title = _normalize_item(title) if title else None
    request_payload = {
        "provider": provider,
        "thread_id": normalized_thread_id,
        "cursor": cursor,
        "progress": normalized_progress,
        "url": requested_url,
        "title": requested_title,
    }
    request_hash = payload_hash(request_payload)

    try:
        with transaction.atomic():
            issue = (
                _issue_queryset(workspace_slug, project_id)
                .select_for_update(of=("self",))
                .select_related("project")
                .filter(pk=issue_id)
                .first()
            )
            if issue is None:
                raise IssueChatNotFound

            binding = (
                IssueChatBinding.objects.select_for_update(of=("self",))
                .filter(
                    workspace_id=issue.workspace_id,
                    provider=provider,
                    thread_id=normalized_thread_id,
                )
                .select_related("link")
                .first()
            )
            created = binding is None
            if created:
                if requested_url is None:
                    raise IssueChatInputError("url", "url is required when creating a chat binding.")
                binding = _create_binding(issue, actor, provider, normalized_thread_id)
            elif binding.issue_id != issue.id:
                raise IssueChatConflict(
                    "thread_already_bound",
                    "This provider thread is already bound to another work item.",
                )
            elif cursor < binding.sync_cursor:
                raise IssueChatConflict(
                    "stale_cursor",
                    "The sync cursor is older than the last applied update.",
                )
            elif cursor == binding.sync_cursor:
                if request_hash != binding.payload_hash:
                    raise IssueChatConflict(
                        "cursor_reused",
                        "This sync cursor was already used with different chat data.",
                    )
                return IssueChatSyncResult(binding=binding, created=False, applied=False)

            link = binding.link
            if link is not None and link.issue_id != issue.id:
                raise IssueChatConflict(
                    "invalid_link",
                    "The existing visible chat link does not belong to this work item.",
                )
            if link is None or link.deleted_at is not None:
                if requested_url is None:
                    raise IssueChatInputError("url", "url is required when creating a chat binding.")
                provider_label = dict(IssueChatBinding.Provider.choices)[provider]
                link = IssueLink.objects.create(
                    workspace=issue.workspace,
                    project=issue.project,
                    issue=issue,
                    url=requested_url,
                    title=requested_title or f"{provider_label} chat",
                    metadata={
                        "kind": "chat",
                        "chat_binding_id": str(binding.id),
                        "provider": provider,
                        "thread_id": normalized_thread_id,
                    },
                    created_by_id=actor.id,
                )
                binding.link = link
            else:
                link_updates = {}
                if requested_url and requested_url != link.url:
                    link_updates["url"] = requested_url
                if requested_title is not None and requested_title != link.title:
                    link_updates["title"] = requested_title
                if link_updates:
                    link_updates["updated_at"] = timezone.now()
                    IssueLink.objects.filter(pk=link.pk).update(**link_updates)
                    link.refresh_from_db(fields=list(link_updates))

            updated_description, block_hash = merge_progress_block(
                issue.description_html,
                binding.id,
                normalized_progress,
                expected_block_hash=binding.generated_block_hash,
            )
            synced_at = timezone.now()
            if updated_description != issue.description_html:
                issue.description_html = updated_description
                issue.updated_at = synced_at
                issue.save(update_fields=["description_html", "description_stripped", "updated_at"])

            binding.sync_cursor = cursor
            binding.progress = normalized_progress
            binding.payload_hash = request_hash
            binding.generated_block_hash = block_hash
            binding.last_synced_at = synced_at
            binding.link = link
            binding.save(
                update_fields=[
                    "sync_cursor",
                    "progress",
                    "payload_hash",
                    "generated_block_hash",
                    "last_synced_at",
                    "link",
                    "updated_at",
                ]
            )
    except IntegrityError as exc:
        raise IssueChatConflict(
            "thread_already_bound",
            "This provider thread is already bound to another work item.",
        ) from exc

    return IssueChatSyncResult(binding=binding, created=created, applied=True)
