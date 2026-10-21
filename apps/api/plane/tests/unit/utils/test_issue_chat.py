# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

import pytest
from django.test import override_settings

from plane.utils.issue_chat import (
    IssueChatInputError,
    IssueChatProgressConflict,
    codex_thread_deep_link,
    merge_progress_block,
    normalize_chat_binding_url,
    normalize_provider_url,
    progress_block_hash,
    progress_block_id,
    redact_issue_activity_fields,
    redact_issue_description_fields,
    render_progress_block,
)


@pytest.mark.unit
def test_render_progress_block_is_compact_safe_and_stable():
    binding_id = uuid4()
    progress = {
        "done": ["Ship API", "Fixed   <script>alert(1)</script>"],
        "now": ["Write tests"],
        "waiting": [],
    }

    block = render_progress_block(binding_id, progress)

    assert f'id="{progress_block_id(binding_id)}"' in block
    assert 'data-node-type="chat-progress"' in block
    assert "<strong>Done:</strong> Ship API · Fixed &lt;script&gt;alert(1)&lt;/script&gt;" in block
    assert "<strong>Now:</strong> Write tests" in block
    assert "<strong>Waiting:</strong> —" in block
    assert "<script>" not in block
    assert block == render_progress_block(binding_id, progress)


@pytest.mark.unit
def test_merge_progress_block_preserves_manual_html_and_replaces_only_generated_block():
    binding_id = uuid4()
    first, first_hash = merge_progress_block(
        "<p>Manual intro</p><p>Manual details</p>",
        binding_id,
        {"done": ["One"], "now": ["Two"], "waiting": []},
    )
    second, second_hash = merge_progress_block(
        first,
        binding_id,
        {"done": ["One", "Two"], "now": ["Three"], "waiting": ["Review"]},
        expected_block_hash=first_hash,
    )

    assert second.startswith("<p>Manual intro</p><p>Manual details</p>")
    assert second.count('data-node-type="chat-progress"') == 1
    assert "Done:" in second and "One · Two" in second
    assert "Waiting:</strong> Review" in second
    assert second_hash == progress_block_hash(
        render_progress_block(
            binding_id,
            {
                "done": ["One", "Two"],
                "now": ["Three"],
                "waiting": ["Review"],
            },
        )
    )


@pytest.mark.unit
def test_merge_progress_block_rejects_manual_edit_and_removal():
    binding_id = uuid4()
    description, block_hash = merge_progress_block(
        "<p>Manual</p>",
        binding_id,
        {"done": ["One"], "now": ["Two"], "waiting": []},
    )

    edited = description.replace("Now:", "Changed:")
    with pytest.raises(IssueChatProgressConflict) as exc_info:
        merge_progress_block(edited, binding_id, {"done": [], "now": [], "waiting": []}, block_hash)
    assert exc_info.value.code == "manual_edit"

    with pytest.raises(IssueChatProgressConflict) as exc_info:
        merge_progress_block("<p>Manual</p>", binding_id, {"done": [], "now": [], "waiting": []}, block_hash)
    assert exc_info.value.code == "missing_block"


@pytest.mark.unit
def test_merge_progress_block_rejects_duplicate_markers():
    binding_id = uuid4()
    block = render_progress_block(binding_id, {"done": [], "now": [], "waiting": []})

    with pytest.raises(IssueChatProgressConflict) as exc_info:
        merge_progress_block(f"<p>Manual</p>{block}{block}", binding_id, {"done": [], "now": [], "waiting": []})
    assert exc_info.value.code == "duplicate_block"


@pytest.mark.unit
def test_editor_serialization_changes_do_not_look_like_manual_edits():
    binding_id = uuid4()
    block = render_progress_block(binding_id, {"done": ["One"], "now": ["Two"], "waiting": []})
    editor_block = block.replace(
        f'<div data-node-type="chat-progress" id="{progress_block_id(binding_id)}">',
        (
            '<div contenteditable="false" class="my-3" '
            f"id=\"{progress_block_id(binding_id)}\" data-node-type='chat-progress'>"
        ),
    ).replace("</p><p>", "</p>\n  <p>")

    assert editor_block != block
    assert progress_block_hash(editor_block) == progress_block_hash(block)


@pytest.mark.unit
@override_settings(DEBUG=False)
def test_provider_url_validation_canonicalizes_safe_links_and_rejects_unsafe_hosts():
    assert (
        normalize_provider_url("codex", "HTTPS://CHATGPT.COM/codex/tasks/example")
        == "https://chatgpt.com/codex/tasks/example"
    )

    for url in (
        "http://chatgpt.com/codex/tasks/example",
        "https://chatgpt.com.evil.example/codex/tasks/example",
        "https://token@chatgpt.com/codex/tasks/example",
        "https://chatgpt.com:8443/codex/tasks/example",
    ):
        with pytest.raises(IssueChatInputError):
            normalize_provider_url("codex", url)


@pytest.mark.unit
@override_settings(DEBUG=False)
def test_native_codex_url_is_synthesized_only_for_a_matching_uuid_thread():
    thread_id = str(uuid4())
    other_thread_id = str(uuid4())
    native_url = f"codex://threads/{thread_id}"

    assert codex_thread_deep_link(thread_id) == native_url
    assert normalize_chat_binding_url("codex", thread_id, None) == native_url
    assert normalize_chat_binding_url("codex", thread_id, "") == native_url
    assert normalize_chat_binding_url("codex", thread_id, native_url) == native_url
    assert normalize_chat_binding_url("codex", thread_id, native_url.upper()) == native_url
    assert normalize_chat_binding_url("codex", thread_id, "https://chatgpt.com/codex/tasks/example") == (
        "https://chatgpt.com/codex/tasks/example"
    )

    for url in (
        f"codex://threads/{other_thread_id}",
        f"codex://threads/{thread_id}/",
        f"codex://user@{thread_id}",
        f"codex://threads/{thread_id}?source=test",
        f"codex://threads/{thread_id}#fragment",
    ):
        with pytest.raises(IssueChatInputError):
            normalize_chat_binding_url("codex", thread_id, url)

    with pytest.raises(IssueChatInputError):
        normalize_chat_binding_url("codex", "thread-not-a-uuid", None)
    with pytest.raises(IssueChatInputError):
        normalize_chat_binding_url("codex", "thread-not-a-uuid", native_url)
    with pytest.raises(IssueChatInputError):
        normalize_chat_binding_url("chatgpt", thread_id, None)
    with pytest.raises(IssueChatInputError):
        normalize_chat_binding_url("chatgpt", thread_id, native_url)


@pytest.mark.unit
def test_redaction_removes_every_generated_description_representation():
    binding_id = uuid4()
    marker_id = progress_block_id(binding_id)
    block = render_progress_block(
        binding_id,
        {"done": ["Hidden done"], "now": ["Hidden now"], "waiting": []},
    )
    data = {
        "description_html": f"<p>Manual text.</p>{block}",
        "description_stripped": "Manual text. Hidden done Hidden now",
        "description_json": {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Manual text."}]},
                {
                    "type": "chatProgress",
                    "attrs": {
                        "id": marker_id,
                        "progress": {"done": ["Hidden done"], "now": ["Hidden now"], "waiting": []},
                    },
                },
            ],
        },
    }

    redact_issue_description_fields(data)

    assert data["description_html"] == "<p>Manual text.</p>"
    assert data["description_stripped"] == "Manual text."
    assert [node["type"] for node in data["description_json"]["content"]] == ["paragraph"]


@pytest.mark.unit
def test_activity_redaction_covers_history_values_and_nested_issue_detail():
    block = render_progress_block(
        uuid4(),
        {"done": ["History done"], "now": ["History now"], "waiting": []},
    )
    data = {
        "field": "description",
        "old_value": "<p>Previous manual.</p>",
        "new_value": f"<p>Manual history.</p>{block}",
        "issue_detail": {
            "description_html": f"<p>Nested manual.</p>{block}",
            "description_json": {
                "type": "doc",
                "content": [
                    {"type": "chat-progress", "attrs": {"id": progress_block_id(uuid4())}},
                    {"type": "paragraph"},
                ],
            },
        },
    }

    redact_issue_activity_fields(data)

    assert data["new_value"] == "<p>Manual history.</p>"
    assert data["issue_detail"]["description_html"] == "<p>Nested manual.</p>"
    assert [node["type"] for node in data["issue_detail"]["description_json"]["content"]] == ["paragraph"]
