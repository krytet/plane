# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest import mock
from uuid import uuid4

import pytest
from django.db import IntegrityError
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import (
    APIToken,
    Issue,
    IssueChatBinding,
    IssueLink,
    Project,
    ProjectMember,
    State,
    User,
    Workspace,
    WorkspaceMember,
)


def _user(prefix):
    suffix = uuid4().hex[:8]
    user = User.objects.create(email=f"{prefix}-{suffix}@plane.so", username=f"{prefix}_{suffix}")
    user.set_password("test-password")
    user.save()
    return user


def _workspace(owner, prefix):
    workspace = Workspace.objects.create(
        name=f"{prefix} workspace",
        owner=owner,
        slug=f"{prefix}-{uuid4().hex[:8]}",
    )
    WorkspaceMember.objects.create(workspace=workspace, member=owner, role=20)
    return workspace


def _project(workspace, owner, identifier):
    project = Project.objects.create(
        name=f"{identifier} project",
        identifier=identifier,
        workspace=workspace,
        created_by=owner,
    )
    ProjectMember.objects.create(project=project, member=owner, workspace=workspace, role=20)
    return project


def _issue(project, name="Chat progress"):
    state, _ = State.objects.get_or_create(
        project=project,
        name="In Progress",
        defaults={"color": "#F59E0B", "group": "started"},
    )
    return Issue.objects.create(
        name=name,
        project=project,
        workspace=project.workspace,
        state=state,
        description_html="<p>Manual description.</p><p>Keep this.</p>",
        description_json={"type": "doc", "content": [{"type": "paragraph"}]},
    )


def _payload(thread_id=None, cursor=1):
    return {
        "provider": "codex",
        "thread_id": thread_id or f"thread-{uuid4().hex}",
        "url": "https://chatgpt.com/codex/tasks/example",
        "title": "Codex task",
        "cursor": cursor,
        "progress": {
            "done": ["Backend model"],
            "now": ["Focused tests"],
            "waiting": ["Frontend integration"],
        },
    }


def _url(workspace, issue):
    return f"/api/workspaces/{workspace.slug}/projects/{issue.project_id}/issues/{issue.id}/chat-bindings/"


def _issue_url(workspace, issue):
    return f"/api/workspaces/{workspace.slug}/projects/{issue.project_id}/issues/{issue.id}/"


@pytest.fixture
def tenant(db):
    owner = _user("owner")
    workspace = _workspace(owner, "chat")
    project = _project(workspace, owner, "CHAT")
    issue = _issue(project)
    client = APIClient()
    client.force_login(owner)
    return {
        "owner": owner,
        "workspace": workspace,
        "project": project,
        "issue": issue,
        "client": client,
    }


@pytest.mark.contract
@pytest.mark.django_db
def test_cookie_session_app_route_syncs_lists_and_preserves_description(tenant):
    issue = tenant["issue"]
    response = tenant["client"].post(_url(tenant["workspace"], issue), _payload(), format="json")

    assert response.status_code == status.HTTP_201_CREATED, response.content
    assert response.data["applied"] is True
    binding = IssueChatBinding.objects.get(pk=response.data["id"])
    assert binding.last_synced_at is not None

    issue.refresh_from_db()
    assert issue.description_html.startswith("<p>Manual description.</p><p>Keep this.</p>")
    assert "Backend model" in issue.description_stripped
    assert "Manual description." in issue.description_stripped
    assert issue.description_json == {"type": "doc", "content": [{"type": "paragraph"}]}

    listed = tenant["client"].get(_url(tenant["workspace"], issue))
    assert listed.status_code == status.HTTP_200_OK, listed.content
    assert [str(item["id"]) for item in listed.data] == [str(binding.id)]
    assert listed.data[0]["link"]["url"] == "https://chatgpt.com/codex/tasks/example"


@pytest.mark.contract
@pytest.mark.django_db
def test_app_route_accepts_native_codex_uuid_without_url(tenant):
    thread_id = str(uuid4())
    payload = _payload(thread_id=thread_id)
    payload.pop("url")

    response = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED, response.content
    binding = IssueChatBinding.objects.get(pk=response.data["id"])
    assert binding.thread_id == thread_id
    assert binding.link is not None
    assert binding.link.url == f"codex://threads/{thread_id}"
    assert response.data["link"]["url"] == f"codex://threads/{thread_id}"


@pytest.mark.contract
@pytest.mark.django_db
def test_app_route_accepts_only_matching_native_codex_urls_and_keeps_chatgpt_https_only(tenant):
    thread_id = str(uuid4())
    other_thread_id = str(uuid4())
    native_url = f"codex://threads/{thread_id}"
    matching = {**_payload(thread_id=thread_id), "url": native_url}

    response = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), matching, format="json")
    assert response.status_code == status.HTTP_201_CREATED, response.content
    assert response.data["link"]["url"] == native_url

    mismatch = {**_payload(thread_id=thread_id), "url": f"codex://threads/{other_thread_id}"}
    mismatch_response = tenant["client"].post(
        _url(tenant["workspace"], tenant["issue"]),
        mismatch,
        format="json",
    )
    assert mismatch_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "url" in mismatch_response.data

    chatgpt_payload = {
        **_payload(thread_id=str(uuid4())),
        "provider": "chatgpt",
        "url": native_url,
    }
    chatgpt_response = tenant["client"].post(
        _url(tenant["workspace"], tenant["issue"]),
        chatgpt_payload,
        format="json",
    )
    assert chatgpt_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "url" in chatgpt_response.data

    chatgpt_payload.pop("url")
    missing_web_url = tenant["client"].post(
        _url(tenant["workspace"], tenant["issue"]),
        chatgpt_payload,
        format="json",
    )
    assert missing_web_url.status_code == status.HTTP_400_BAD_REQUEST
    assert "url" in missing_web_url.data


@pytest.mark.contract
@pytest.mark.django_db
def test_app_route_uses_session_auth_and_does_not_accept_api_keys(tenant):
    token = APIToken.objects.create(user=tenant["owner"], label=f"token-{uuid4().hex[:8]}")
    api_key_client = APIClient()
    api_key_client.credentials(HTTP_X_API_KEY=token.token)

    response = api_key_client.post(_url(tenant["workspace"], tenant["issue"]), _payload(), format="json")

    assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
    assert not IssueChatBinding.objects.filter(issue=tenant["issue"]).exists()


@pytest.mark.contract
@pytest.mark.django_db
def test_app_chat_bindings_deny_project_guests_even_with_full_feature_access(tenant):
    tenant["project"].guest_view_all_features = True
    tenant["project"].save(update_fields=["guest_view_all_features"])
    first = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), _payload(), format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    guest = _user("guest")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=guest, role=5)
    ProjectMember.objects.create(project=tenant["project"], workspace=tenant["workspace"], member=guest, role=5)
    guest_client = APIClient()
    guest_client.force_login(guest)

    listed = guest_client.get(_url(tenant["workspace"], tenant["issue"]))
    posted = guest_client.post(
        _url(tenant["workspace"], tenant["issue"]),
        _payload(cursor=2),
        format="json",
    )

    assert listed.status_code == status.HTTP_403_FORBIDDEN
    assert posted.status_code == status.HTTP_403_FORBIDDEN
    assert IssueChatBinding.objects.filter(issue=tenant["issue"]).count() == 1


@pytest.mark.contract
@pytest.mark.django_db
def test_provider_thread_is_unique_per_workspace_and_independent_across_workspaces(tenant):
    payload = _payload(thread_id="workspace-scoped-thread")
    first = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    second_workspace = _workspace(tenant["owner"], "other")
    second_project = _project(second_workspace, tenant["owner"], "OTHER")
    second_issue = _issue(second_project, "Other workspace issue")
    second = tenant["client"].post(_url(second_workspace, second_issue), payload, format="json")
    assert second.status_code == status.HTTP_201_CREATED, second.content

    sibling_issue = _issue(tenant["project"], "Same workspace issue")
    collision = tenant["client"].post(_url(tenant["workspace"], sibling_issue), payload, format="json")
    assert collision.status_code == status.HTTP_409_CONFLICT
    assert collision.data["code"] == "thread_already_bound"
    assert IssueChatBinding.objects.filter(thread_id=payload["thread_id"]).count() == 2


@pytest.mark.contract
@pytest.mark.django_db
def test_first_bind_database_race_returns_conflict_instead_of_server_error(tenant):
    with mock.patch.object(IssueChatBinding.objects, "create", side_effect=IntegrityError("duplicate")):
        response = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), _payload(), format="json")

    assert response.status_code == status.HTTP_409_CONFLICT, response.content
    assert response.data["code"] == "thread_already_bound"
    assert not IssueLink.objects.filter(issue=tenant["issue"]).exists()


@pytest.mark.contract
@pytest.mark.django_db
def test_same_cursor_rejects_changed_url_or_title(tenant):
    payload = _payload(cursor=1)
    first = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    changed_url = {**payload, "url": "https://chatgpt.com/codex/tasks/other"}
    response = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), changed_url, format="json")
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.data["code"] == "cursor_reused"

    changed_title = {**payload, "title": "Different title"}
    response = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), changed_title, format="json")
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.data["code"] == "cursor_reused"


@pytest.mark.contract
@pytest.mark.django_db
@pytest.mark.parametrize(
    "url",
    [
        "http://chatgpt.com/codex/tasks/example",
        "https://chatgpt.com.evil.example/codex/tasks/example",
        "https://user:token@chatgpt.com/codex/tasks/example",
    ],
)
def test_app_route_rejects_unsafe_provider_urls(tenant, url):
    response = tenant["client"].post(
        _url(tenant["workspace"], tenant["issue"]),
        {**_payload(), "url": url},
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "url" in response.data
    assert not IssueChatBinding.objects.filter(issue=tenant["issue"]).exists()


@pytest.mark.contract
@pytest.mark.django_db
def test_editor_normalized_block_is_accepted_and_manual_text_edit_is_rejected(tenant):
    issue = tenant["issue"]
    payload = _payload(cursor=1)
    first = tenant["client"].post(_url(tenant["workspace"], issue), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    issue.refresh_from_db()
    binding_id = first.data["id"]
    editor_html = issue.description_html.replace(
        f'<div data-node-type="chat-progress" id="plane-chat-progress-{binding_id}">',
        (
            '<div contenteditable="false" class="my-3 space-y-1" '
            f"id=\"plane-chat-progress-{binding_id}\" data-node-type='chat-progress'>"
        ),
    )
    assert editor_html != issue.description_html
    Issue.all_objects.filter(pk=issue.pk).update(description_html=editor_html)

    second = tenant["client"].post(
        _url(tenant["workspace"], issue),
        {**payload, "cursor": 2, "progress": {**payload["progress"], "waiting": []}},
        format="json",
    )
    assert second.status_code == status.HTTP_200_OK, second.content

    issue.refresh_from_db()
    edited_html = issue.description_html.replace("Focused tests", "Edited manually")
    Issue.all_objects.filter(pk=issue.pk).update(description_html=edited_html)
    conflict = tenant["client"].post(
        _url(tenant["workspace"], issue),
        {**payload, "cursor": 3, "progress": {**payload["progress"], "now": ["New work"]}},
        format="json",
    )
    assert conflict.status_code == status.HTTP_409_CONFLICT
    assert conflict.data["code"] == "manual_edit"
    binding = IssueChatBinding.objects.get(pk=binding_id)
    assert binding.sync_cursor == 2


@pytest.mark.contract
@pytest.mark.django_db
def test_normal_issue_description_patch_succeeds_without_a_chat_binding(tenant):
    issue = tenant["issue"]
    assert not IssueChatBinding.objects.filter(issue=issue).exists()

    with (
        mock.patch("plane.app.views.issue.base.issue_activity.delay"),
        mock.patch("plane.app.views.issue.base.model_activity.delay"),
        mock.patch("plane.app.views.issue.base.issue_description_version_task.delay"),
    ):
        response = tenant["client"].patch(
            _issue_url(tenant["workspace"], issue),
            {"description_html": "<p>Normal manual description update.</p>"},
            format="json",
        )

    assert response.status_code == status.HTTP_204_NO_CONTENT, response.content
    issue.refresh_from_db()
    assert issue.description_html == "<p>Normal manual description update.</p>"
    assert issue.description_stripped == "Normal manual description update."


@pytest.mark.contract
@pytest.mark.django_db
def test_stale_issue_description_patch_keeps_manual_text_and_latest_generated_block(tenant):
    issue = tenant["issue"]
    payload = _payload(cursor=1)
    first = tenant["client"].post(_url(tenant["workspace"], issue), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    issue.refresh_from_db()
    stale_editor_html = issue.description_html.replace("Focused tests", "Spoofed generated text").replace(
        "Keep this.",
        "Keep this manual edit from the stale tab.",
    )
    second = tenant["client"].post(
        _url(tenant["workspace"], issue),
        {**payload, "cursor": 2, "progress": {**payload["progress"], "now": ["Latest concurrent work"]}},
        format="json",
    )
    assert second.status_code == status.HTTP_200_OK, second.content

    with (
        mock.patch("plane.app.views.issue.base.issue_activity.delay"),
        mock.patch("plane.app.views.issue.base.model_activity.delay"),
        mock.patch("plane.app.views.issue.base.issue_description_version_task.delay"),
    ):
        response = tenant["client"].patch(
            _issue_url(tenant["workspace"], issue),
            {"description_html": stale_editor_html},
            format="json",
        )

    assert response.status_code == status.HTTP_204_NO_CONTENT, response.content
    issue.refresh_from_db()
    assert "Keep this manual edit from the stale tab." in issue.description_html
    assert "Latest concurrent work" in issue.description_html
    assert "Spoofed generated text" not in issue.description_html
    assert issue.description_html.count('data-node-type="chat-progress"') == 1
