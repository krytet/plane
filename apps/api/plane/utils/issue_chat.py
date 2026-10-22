# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from bs4 import BeautifulSoup
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.db.models import CharField, Prefetch, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Coalesce
from django.utils import timezone

from plane.db.models import (
    Issue,
    IssueChatBinding,
    IssueChatBindingSeen,
    IssueLink,
    ProjectMember,
    WorkspaceMember,
)
from plane.db.models.project import ROLE


PROGRESS_BLOCK_TYPE = "chat-progress"
CHAT_PROGRESS_JSON_TYPE = "chatProgress"
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
OBSERVATION_ATTENTION_STATES = frozenset(
    {
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        IssueChatBinding.ObservationState.NEEDS_ACTION,
        IssueChatBinding.ObservationState.FAILED,
    }
)

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


@dataclass(frozen=True)
class IssueChatBindingCreateResult:
    binding: IssueChatBinding
    created: bool


@dataclass(frozen=True)
class IssueChatObservationResult:
    binding: IssueChatBinding
    applied: bool


@dataclass(frozen=True)
class IssueChatSeenResult:
    binding: IssueChatBinding
    acknowledgement: IssueChatBindingSeen
    applied: bool


_DROP_CHAT_PROGRESS = object()


def _attribute_text(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return value.strip() if isinstance(value, str) else ""


def _is_chat_progress_identity(node_type, marker_id):
    normalized_type = _WHITESPACE_RE.sub("-", str(node_type or "").strip()).lower()
    if normalized_type.replace("_", "-").replace("-", "") == "chatprogress":
        return True
    return _attribute_text(marker_id).lower().startswith(PROGRESS_BLOCK_ID_PREFIX)


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


def _chat_progress_json_node_id(node):
    attrs = node.get("attrs")
    attrs = attrs if isinstance(attrs, dict) else {}
    node_type = node.get("type") or node.get("name") or attrs.get("data-node-type") or attrs.get("dataNodeType")
    marker_id = node.get("id") or attrs.get("id")
    if not _is_chat_progress_identity(node_type, marker_id):
        return None
    return _attribute_text(marker_id)


def _collect_chat_progress_json_nodes(value, nodes):
    if isinstance(value, list):
        for item in value:
            _collect_chat_progress_json_nodes(item, nodes)
        return
    if not isinstance(value, dict):
        return

    marker_id = _chat_progress_json_node_id(value)
    if marker_id is not None:
        if not marker_id:
            raise IssueChatProgressConflict(
                "malformed_block",
                "The generated progress block is malformed.",
            )
        if marker_id in nodes:
            raise IssueChatProgressConflict(
                "duplicate_block",
                "The generated progress block appears more than once.",
            )
        nodes[marker_id] = deepcopy(value)
        return

    for item in value.values():
        _collect_chat_progress_json_nodes(item, nodes)


def reject_managed_chat_progress(description_html=None, description_json=None):
    """Fail closed when a create payload tries to forge a generated progress block."""

    if description_html and _generated_block_bounds(description_html):
        raise IssueChatProgressConflict(
            "unexpected_block",
            "Generated chat progress can only be created through chat binding sync.",
        )

    if description_json is not None:
        nodes = {}
        _collect_chat_progress_json_nodes(description_json, nodes)
        if nodes:
            raise IssueChatProgressConflict(
                "unexpected_block",
                "Generated chat progress can only be created through chat binding sync.",
            )


def _latest_chat_progress_json_node(marker_id, binding, current_nodes):
    node = deepcopy(current_nodes.get(marker_id, {}))
    attrs = node.get("attrs")
    attrs = deepcopy(attrs) if isinstance(attrs, dict) else {}
    attrs["id"] = marker_id
    attrs["progress"] = normalize_progress(binding.progress)
    node.setdefault("type", CHAT_PROGRESS_JSON_TYPE)
    node["attrs"] = attrs
    return node


def _merge_chat_progress_json_value(value, latest_blocks, seen_markers):
    if isinstance(value, list):
        merged = []
        for item in value:
            replaced = _merge_chat_progress_json_value(item, latest_blocks, seen_markers)
            if replaced is not _DROP_CHAT_PROGRESS:
                merged.append(replaced)
        return merged
    if not isinstance(value, dict):
        return value

    marker_id = _chat_progress_json_node_id(value)
    if marker_id is not None:
        if not marker_id:
            raise IssueChatProgressConflict(
                "malformed_block",
                "The generated progress block is malformed.",
            )
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
        return deepcopy(latest_blocks[marker_id])

    return {
        key: merged
        for key, item in value.items()
        for merged in [_merge_chat_progress_json_value(item, latest_blocks, seen_markers)]
        if merged is not _DROP_CHAT_PROGRESS
    }


def _append_chat_progress_json_nodes(description_json, nodes):
    if not nodes:
        return description_json
    if isinstance(description_json, list):
        return [*description_json, *[deepcopy(node) for node in nodes]]
    if not isinstance(description_json, dict):
        return {"type": "doc", "content": [deepcopy(node) for node in nodes]}

    description_json = deepcopy(description_json)
    content = description_json.get("content")
    content = content if isinstance(content, list) else []
    content.extend(deepcopy(node) for node in nodes)
    description_json["content"] = content
    return description_json


def merge_latest_chat_description(issue, submitted_description_html, submitted_description_json=None):
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
    latest_json_blocks = {}
    current_json_blocks = {}
    if submitted_description_json is not None:
        if not isinstance(submitted_description_json, (dict, list)):
            raise IssueChatProgressConflict(
                "malformed_json",
                "The work item description JSON is malformed.",
            )
        _collect_chat_progress_json_nodes(issue.description_json, current_json_blocks)

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
        if submitted_description_json is not None:
            latest_json_blocks[marker_id] = _latest_chat_progress_json_node(
                marker_id,
                binding,
                current_json_blocks,
            )

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
        merged_description = f"{merged_description[:start]}{latest_blocks[marker_id]}{merged_description[end:]}"

    for marker_id in latest_blocks:
        if marker_id not in seen_markers:
            merged_description = f"{merged_description}{latest_blocks[marker_id]}"

    merged_description_json = None
    if submitted_description_json is not None:
        seen_json_markers = set()
        merged_description_json = _merge_chat_progress_json_value(
            submitted_description_json,
            latest_json_blocks,
            seen_json_markers,
        )
        merged_description_json = _append_chat_progress_json_nodes(
            merged_description_json,
            [
                latest_json_blocks[marker_id]
                for marker_id in latest_json_blocks
                if marker_id not in seen_json_markers
            ],
        )

    return merged_description, merged_description_json


def merge_latest_chat_blocks(issue, submitted_description_html):
    """HTML-only compatibility wrapper for description preservation."""

    return merge_latest_chat_description(issue, submitted_description_html)[0]


def can_view_issue_chat_progress(user, workspace_slug, project_id):
    """Return whether a project member may see generated chat progress.

    Guests are denied even when ``guest_view_all_features`` exposes the rest of
    the work item. A workspace admin who is a project guest follows the same
    rule as chat binding management and remains allowed.
    """

    if user is None or not getattr(user, "is_authenticated", False):
        return False

    role = (
        ProjectMember.objects.filter(
            workspace__slug=workspace_slug,
            project_id=project_id,
            member=user,
            is_active=True,
        )
        .values_list("role", flat=True)
        .first()
    )
    if role is None:
        return False
    if role >= ROLE.MEMBER.value:
        return True
    return WorkspaceMember.objects.filter(
        workspace__slug=workspace_slug,
        member=user,
        role=ROLE.ADMIN.value,
        is_active=True,
    ).exists()


def issue_chat_presence_summaries(user, workspace_slug, project_id, issue_ids):
    """Return compact board counts only for users allowed to see chat presence."""

    if not can_view_issue_chat_progress(user, workspace_slug, project_id):
        return {}

    normalized_issue_ids = {str(issue_id): issue_id for issue_id in issue_ids if issue_id}
    summaries = {
        issue_id: {
            "count": 0,
            "attention": 0,
            "unread": 0,
            "manual_attention": 0,
        }
        for issue_id in normalized_issue_ids
    }
    if not summaries:
        return summaries

    bindings = list(
        IssueChatBinding.objects.filter(
            workspace__slug=workspace_slug,
            project_id=project_id,
            issue_id__in=normalized_issue_ids.values(),
        ).only(
            "id",
            "issue_id",
            "observation_state",
            "observation_source",
            "observation_observed_at",
        )
    )
    seen_by_binding = {
        seen.binding_id: seen
        for seen in IssueChatBindingSeen.objects.filter(
            member=user,
            binding_id__in=[binding.id for binding in bindings],
        )
    }
    for binding in bindings:
        summary = summaries[str(binding.issue_id)]
        summary["count"] += 1
        if binding.has_attention:
            summary["attention"] += 1
        if binding.is_manual_attention:
            summary["manual_attention"] += 1
        if binding_is_unread(binding, seen_by_binding.get(binding.id)):
            summary["unread"] += 1
    return summaries


def attach_issue_chat_presence(issues, user, workspace_slug, project_id):
    """Attach ``chat_presence`` to internal board cards for authorized roles."""

    if not can_view_issue_chat_progress(user, workspace_slug, project_id):
        return issues

    rows = list(issues)
    summaries = issue_chat_presence_summaries(
        user,
        workspace_slug,
        project_id,
        [row.get("id") for row in rows],
    )
    for row in rows:
        row["chat_presence"] = summaries.get(
            str(row.get("id")),
            {
                "count": 0,
                "attention": 0,
                "unread": 0,
                "manual_attention": 0,
            },
        )
    return rows


def attach_group_chat_presence(grouped_results):
    """Add a card-count/attention summary to each grouped response level."""

    if not isinstance(grouped_results, dict):
        return grouped_results

    def summarize(value):
        totals = {
            "count": 0,
            "attention": 0,
            "unread": 0,
            "manual_attention": 0,
        }

        def walk(node):
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if not isinstance(node, dict):
                return
            presence = node.get("chat_presence")
            if isinstance(presence, dict):
                for key in totals:
                    totals[key] += int(presence.get(key, 0) or 0)
            if "results" in node:
                walk(node["results"])

        walk(value)
        return totals

    for group in grouped_results.values():
        if isinstance(group, dict) and "results" in group:
            group["chat_presence"] = summarize(group["results"])
            if isinstance(group["results"], dict):
                for subgroup in group["results"].values():
                    if isinstance(subgroup, dict) and "results" in subgroup:
                        subgroup["chat_presence"] = summarize(subgroup["results"])
    return grouped_results


def scope_issue_links_for_user(queryset, user, workspace_slug, project_id):
    """Hide managed chat links from users who cannot see generated progress."""

    if can_view_issue_chat_progress(user, workspace_slug, project_id):
        return queryset
    managed_link_ids = list(
        IssueChatBinding.objects.filter(
            workspace__slug=workspace_slug,
            project_id=project_id,
            link_id__isnull=False,
        ).values_list("link_id", flat=True)
    )
    return (
        queryset.annotate(
            _chat_link_kind=Coalesce(
                KeyTextTransform("kind", "metadata"),
                Value(""),
                output_field=CharField(),
            )
        )
        .exclude(_chat_link_kind="chat")
        .exclude(metadata__has_key="chat_binding_id")
        .exclude(pk__in=managed_link_ids)
    )


def is_issue_chat_link(link):
    metadata = getattr(link, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    return metadata.get("kind") == "chat" or bool(metadata.get("chat_binding_id"))


def redact_chat_progress_html(description_html):
    """Remove generated progress blocks while preserving manual HTML."""

    if not description_html:
        return description_html
    soup = BeautifulSoup(description_html, "html.parser")
    removed = False
    for tag in list(soup.find_all("div")):
        attributes = getattr(tag, "attrs", {})
        if _is_chat_progress_identity(attributes.get("data-node-type"), attributes.get("id")):
            tag.decompose()
            removed = True
    return str(soup) if removed else description_html


def _redact_chat_progress_json_value(value):
    if isinstance(value, list):
        return [
            redacted
            for item in value
            for redacted in [_redact_chat_progress_json_value(item)]
            if redacted is not _DROP_CHAT_PROGRESS
        ]
    if not isinstance(value, dict):
        return value

    attrs = value.get("attrs")
    attrs = attrs if isinstance(attrs, dict) else {}
    if _is_chat_progress_identity(
        value.get("type") or value.get("name") or attrs.get("data-node-type") or attrs.get("dataNodeType"),
        value.get("id") or attrs.get("id"),
    ):
        return _DROP_CHAT_PROGRESS

    return {
        key: redacted
        for key, item in value.items()
        for redacted in [_redact_chat_progress_json_value(item)]
        if redacted is not _DROP_CHAT_PROGRESS
    }


def redact_chat_progress_json(description_json):
    if description_json is None:
        return None
    redacted = _redact_chat_progress_json_value(description_json)
    return {} if redacted is _DROP_CHAT_PROGRESS else redacted


def redact_issue_description_fields(data):
    """Strip protected progress from every description representation."""

    description_html = data.get("description_html")
    redacted_html = redact_chat_progress_html(description_html)
    if "description_html" in data:
        data["description_html"] = redacted_html
    if "description_stripped" in data:
        data["description_stripped"] = redacted_issue_description_stripped(
            description_html,
            data.get("description_stripped"),
        )
    if "description_json" in data:
        data["description_json"] = redact_chat_progress_json(data.get("description_json"))
    if "description_binary" in data:
        data["description_binary"] = None
    return data


def redacted_issue_description_stripped(description_html, description_stripped):
    """Return plain text derived from redacted HTML when the HTML is available."""

    redacted_html = redact_chat_progress_html(description_html)
    if isinstance(redacted_html, str) and redacted_html != description_html:
        return _WHITESPACE_RE.sub(" ", BeautifulSoup(redacted_html, "html.parser").get_text(" ", strip=True)).strip()
    return description_stripped


def redact_issue_activity_fields(data):
    """Redact generated progress from description changes and nested issue detail."""

    for key in ("old_value", "new_value"):
        value = data.get(key)
        if isinstance(value, str):
            redacted = redact_chat_progress_html(value)
            if redacted == value:
                stripped = value.strip()
                if stripped.startswith(("{", "[")):
                    try:
                        parsed = json.loads(stripped)
                    except (TypeError, ValueError):
                        pass
                    else:
                        redacted_json = redact_chat_progress_json(parsed)
                        redacted = json.dumps(redacted_json, ensure_ascii=False)
            data[key] = redacted
        elif isinstance(value, (dict, list)):
            data[key] = redact_chat_progress_json(value)

    for key in ("issue", "issue_detail"):
        nested = data.get(key)
        if isinstance(nested, dict):
            redact_issue_description_fields(nested)
    return data


def get_issue_chat_progress_redacted_project_ids(user, workspace_slug, project_ids):
    """Bulk scope progress redaction for exports without a query per work item."""

    project_ids = {str(project_id) for project_id in project_ids}
    if not project_ids:
        return set()
    if user is None or not getattr(user, "is_authenticated", False):
        return project_ids

    roles = {
        str(project_id): role
        for project_id, role in ProjectMember.objects.filter(
            workspace__slug=workspace_slug,
            project_id__in=project_ids,
            member=user,
            is_active=True,
        ).values_list("project_id", "role")
    }
    is_workspace_admin = WorkspaceMember.objects.filter(
        workspace__slug=workspace_slug,
        member=user,
        role=ROLE.ADMIN.value,
        is_active=True,
    ).exists()
    return {
        project_id
        for project_id in project_ids
        if not (
            roles.get(project_id, -1) >= ROLE.MEMBER.value
            or (is_workspace_admin and project_id in roles)
        )
    }


def get_issue_chat_progress_redacted_activity_project_ids(user, activities):
    """Return redacted project IDs for activities from one or more workspaces."""

    scopes = {}
    for activity in activities:
        project_id = getattr(activity, "project_id", None)
        workspace = getattr(activity, "workspace", None)
        workspace_slug = getattr(workspace, "slug", None)
        if project_id is not None and workspace_slug:
            scopes.setdefault(workspace_slug, set()).add(str(project_id))

    redacted_project_ids = set()
    for workspace_slug, project_ids in scopes.items():
        redacted_project_ids.update(
            get_issue_chat_progress_redacted_project_ids(
                user,
                workspace_slug,
                project_ids,
            )
        )
    return redacted_project_ids


def issue_activity_chat_progress_context(user, activities):
    return {
        "redact_chat_progress_project_ids": get_issue_chat_progress_redacted_activity_project_ids(
            user,
            activities,
        )
    }


def _issue_queryset(workspace_slug, project_id):
    return Issue.objects.filter(
        workspace__slug=workspace_slug,
        project_id=project_id,
        project__archived_at__isnull=True,
    )


def list_issue_chat_bindings(workspace_slug, project_id, issue_id, user=None):
    if not _issue_queryset(workspace_slug, project_id).filter(pk=issue_id).exists():
        raise IssueChatNotFound

    queryset = (
        IssueChatBinding.objects.filter(
            workspace__slug=workspace_slug,
            project_id=project_id,
            issue_id=issue_id,
        )
        .select_related("link")
        .order_by("-created_at")
    )
    if user is not None and getattr(user, "is_authenticated", False):
        queryset = queryset.prefetch_related(
            Prefetch(
                "seen_acknowledgements",
                queryset=IssueChatBindingSeen.objects.filter(member=user),
                to_attr="current_user_seen_acknowledgements",
            )
        )
    return queryset


def get_current_user_seen(binding, user=None):
    acknowledgements = getattr(binding, "current_user_seen_acknowledgements", None)
    if acknowledgements is not None:
        return acknowledgements[0] if acknowledgements else None
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return IssueChatBindingSeen.objects.filter(binding=binding, member=user).first()


def binding_is_unread(binding, acknowledgement=None):
    """Return Plane-local unread attention, never a provider read receipt."""

    if binding.observation_state not in OBSERVATION_ATTENTION_STATES:
        return False
    if binding.observation_observed_at is None:
        return False
    if acknowledgement is None or acknowledgement.observed_at is None:
        return True
    return binding.observation_observed_at > acknowledgement.observed_at


def _normalize_binding_identity(provider, thread_id, url):
    normalized_thread_id = (thread_id or "").strip()
    if provider == IssueChatBinding.Provider.CODEX:
        normalized_thread_id = normalize_codex_thread_id(normalized_thread_id) or normalized_thread_id
    return normalized_thread_id, normalize_chat_binding_url(provider, normalized_thread_id, url)


def _ensure_chat_link(binding, issue, actor, provider, thread_id, requested_url, requested_title):
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
                "thread_id": thread_id,
            },
            created_by_id=actor.id,
        )
        return link

    link_updates = {}
    if requested_url and requested_url != link.url:
        link_updates["url"] = requested_url
    if requested_title is not None and requested_title != link.title:
        link_updates["title"] = requested_title
    if link_updates:
        link_updates["updated_at"] = timezone.now()
        IssueLink.objects.filter(pk=link.pk).update(**link_updates)
        link.refresh_from_db(fields=list(link_updates))
    return link


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


def create_issue_chat_binding(
    *,
    workspace_slug,
    project_id,
    issue_id,
    actor,
    provider,
    thread_id,
    url=None,
    title=None,
):
    """Create or return a manual binding without inventing sync state."""

    normalized_thread_id, requested_url = _normalize_binding_identity(provider, thread_id, url)
    requested_title = _normalize_item(title) if title else None

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

            link = _ensure_chat_link(
                binding,
                issue,
                actor,
                provider,
                normalized_thread_id,
                requested_url,
                requested_title,
            )
            if binding.link_id != link.id:
                binding.link = link
                binding.save(update_fields=["link", "updated_at"])
    except IntegrityError as exc:
        raise IssueChatConflict(
            "thread_already_bound",
            "This provider thread is already bound to another work item.",
        ) from exc

    return IssueChatBindingCreateResult(binding=binding, created=created)


def update_issue_chat_observation(
    *,
    workspace_slug,
    project_id,
    issue_id,
    binding_id,
    state,
    observed_at,
    turn_id,
    resume_after_stop=False,
):
    """Apply an idempotent typed Codex observation to an existing binding."""

    normalized_turn_id = (turn_id or "").strip()
    if not normalized_turn_id:
        raise IssueChatInputError("turn_id", "turn_id is required.")
    if state not in IssueChatBinding.ObservationState.values:
        raise IssueChatInputError("state", "A valid observation state is required.")
    if resume_after_stop and state != IssueChatBinding.ObservationState.RUNNING:
        raise IssueChatInputError(
            "resume_after_stop",
            "This flag is only valid for a running observation.",
        )

    with transaction.atomic():
        issue = (
            _issue_queryset(workspace_slug, project_id)
            .select_for_update(of=("self",))
            .select_related("project", "state")
            .filter(pk=issue_id)
            .first()
        )
        if issue is None:
            raise IssueChatNotFound

        binding = (
            IssueChatBinding.objects.select_for_update(of=("self",))
            .select_related("link")
            .filter(
                pk=binding_id,
                workspace_id=issue.workspace_id,
                project_id=issue.project_id,
                issue_id=issue.id,
            )
            .first()
        )
        if binding is None:
            raise IssueChatNotFound
        if binding.provider != IssueChatBinding.Provider.CODEX:
            raise IssueChatConflict(
                "provider_observation_unsupported",
                "Automatic observations are supported for explicitly bound Codex chats only.",
            )

        current_observed_at = binding.observation_observed_at
        if current_observed_at is not None:
            if observed_at < current_observed_at:
                raise IssueChatConflict(
                    "stale_observation",
                    "The observation is older than the last applied observation.",
                )
            if observed_at == current_observed_at:
                if (
                    binding.observation_source == IssueChatBinding.ObservationSource.CODEX
                    and binding.observation_state == state
                    and binding.observation_turn_id == normalized_turn_id
                ):
                    return IssueChatObservationResult(binding=binding, applied=False)
                raise IssueChatConflict(
                    "observation_reused",
                    "This observation time was already used with different data.",
                )
            if (
                binding.observation_turn_id == normalized_turn_id
                and state == IssueChatBinding.ObservationState.RUNNING
            ):
                if binding.observation_state == IssueChatBinding.ObservationState.FAILED:
                    raise IssueChatConflict(
                        "invalid_transition",
                        "A failed turn cannot return to running.",
                    )
                if (
                    binding.observation_state
                    == IssueChatBinding.ObservationState.ANSWER_AVAILABLE
                    and not resume_after_stop
                ):
                    raise IssueChatConflict(
                        "invalid_transition",
                        "A completed turn cannot return to running without resume_after_stop.",
                    )

        binding.observation_state = state
        binding.observation_source = IssueChatBinding.ObservationSource.CODEX
        binding.observation_observed_at = observed_at
        binding.observation_turn_id = normalized_turn_id
        binding.save(
            update_fields=[
                "observation_state",
                "observation_source",
                "observation_observed_at",
                "observation_turn_id",
                "updated_at",
            ]
        )

        from plane.utils.issue_chat_workflow import evaluate_issue_chat_workflow

        evaluate_issue_chat_workflow(
            workspace_slug=workspace_slug,
            project_id=project_id,
            issue_id=issue_id,
            binding_id=binding_id,
            observation_state=state,
            observed_at=observed_at,
            turn_id=normalized_turn_id,
        )

    return IssueChatObservationResult(binding=binding, applied=True)


def set_issue_chat_manual_attention(
    *,
    workspace_slug,
    project_id,
    issue_id,
    binding_id,
    needs_action,
):
    """Set or clear an explicitly manual ChatGPT attention flag."""

    with transaction.atomic():
        binding = (
            IssueChatBinding.objects.select_for_update(of=("self",))
            .select_related("link")
            .filter(
                pk=binding_id,
                workspace__slug=workspace_slug,
                project_id=project_id,
                issue_id=issue_id,
            )
            .first()
        )
        if binding is None:
            raise IssueChatNotFound
        if binding.provider != IssueChatBinding.Provider.CHATGPT:
            raise IssueChatConflict(
                "manual_attention_unsupported",
                "Manual attention is only available for ChatGPT bindings; Codex presence comes from observations.",
            )

        if needs_action:
            next_values = {
                "observation_state": IssueChatBinding.ObservationState.NEEDS_ACTION,
                "observation_source": IssueChatBinding.ObservationSource.MANUAL,
                "observation_observed_at": timezone.now(),
                "observation_turn_id": "",
            }
        else:
            next_values = {
                "observation_state": IssueChatBinding.ObservationState.UNKNOWN,
                "observation_source": None,
                "observation_observed_at": None,
                "observation_turn_id": "",
            }

        applied = any(getattr(binding, field) != value for field, value in next_values.items())
        if applied:
            for field, value in next_values.items():
                setattr(binding, field, value)
            binding.save(update_fields=[*next_values, "updated_at"])

    return IssueChatObservationResult(binding=binding, applied=applied)


def acknowledge_issue_chat_binding(
    *,
    workspace_slug,
    project_id,
    issue_id,
    binding_id,
    actor,
):
    """Record that one Plane user has seen the binding's current observation."""

    with transaction.atomic():
        binding = (
            IssueChatBinding.objects.select_for_update(of=("self",))
            .filter(
                pk=binding_id,
                workspace__slug=workspace_slug,
                project_id=project_id,
                issue_id=issue_id,
            )
            .first()
        )
        if binding is None:
            raise IssueChatNotFound

        acknowledgement, created = IssueChatBindingSeen.objects.get_or_create(
            binding=binding,
            member=actor,
            defaults={
                "workspace": binding.workspace,
                "project": binding.project,
                "observed_at": binding.observation_observed_at,
                "turn_id": binding.observation_turn_id,
                "created_by_id": actor.id,
            },
        )
        applied = created
        if not created and (
            acknowledgement.observed_at != binding.observation_observed_at
            or acknowledgement.turn_id != binding.observation_turn_id
        ):
            acknowledgement.observed_at = binding.observation_observed_at
            acknowledgement.turn_id = binding.observation_turn_id
            acknowledgement.save(update_fields=["observed_at", "turn_id", "updated_at"])
            applied = True

    return IssueChatSeenResult(
        binding=binding,
        acknowledgement=acknowledgement,
        applied=applied,
    )


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
    normalized_thread_id, requested_url = _normalize_binding_identity(provider, thread_id, url)
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

            link = _ensure_chat_link(
                binding,
                issue,
                actor,
                provider,
                normalized_thread_id,
                requested_url,
                requested_title,
            )

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
