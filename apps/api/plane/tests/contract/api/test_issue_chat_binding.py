# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest import mock
from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction
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
from plane.utils.issue_chat import can_view_issue_chat_progress


def _user(prefix):
    suffix = uuid4().hex[:8]
    return User.objects.create(email=f"{prefix}-{suffix}@plane.so", username=f"{prefix}_{suffix}")


def _project(workspace, owner, identifier):
    project = Project.objects.create(
        name=f"{identifier} project",
        identifier=identifier,
        workspace=workspace,
        created_by=owner,
    )
    ProjectMember.objects.create(project=project, member=owner, workspace=workspace, role=20)
    return project


def _issue(project, name):
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
    )


def _client_for(user, token=None):
    client = APIClient()
    if token is None:
        token = APIToken.objects.create(user=user, label=f"token-{uuid4().hex[:8]}")
    client.credentials(HTTP_X_API_KEY=token.token)
    return client


def _url(workspace, issue):
    return f"/api/v1/workspaces/{workspace.slug}/projects/{issue.project_id}/work-items/{issue.id}/chat-bindings/"


def _work_item_url(workspace, issue):
    return f"/api/v1/workspaces/{workspace.slug}/projects/{issue.project_id}/work-items/{issue.id}/"


def _payload(cursor=1, now=None):
    return {
        "provider": "codex",
        "thread_id": f"thread-{uuid4().hex}",
        "url": "https://chatgpt.com/codex/tasks/example",
        "title": "Codex task",
        "cursor": cursor,
        "progress": {
            "done": ["Backend model"],
            "now": now or ["Focused tests"],
            "waiting": ["Frontend integration"],
        },
    }


@pytest.fixture
def tenant(db):
    owner = _user("owner")
    workspace = Workspace.objects.create(name="Chat Workspace", owner=owner, slug=f"chat-{uuid4().hex[:8]}")
    WorkspaceMember.objects.create(workspace=workspace, member=owner, role=20)
    project = _project(workspace, owner, "CHAT")
    issue = _issue(project, "Chat progress")
    return {
        "owner": owner,
        "workspace": workspace,
        "project": project,
        "issue": issue,
        "client": _client_for(owner),
    }


@pytest.mark.contract
@pytest.mark.django_db
def test_sync_creates_visible_link_and_preserves_manual_description_and_state(tenant):
    issue = tenant["issue"]
    original_state_id = issue.state_id
    payload = _payload(cursor=1)

    response = tenant["client"].post(_url(tenant["workspace"], issue), payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED, response.content
    assert response.data["applied"] is True
    binding = IssueChatBinding.objects.get(pk=response.data["id"])
    assert binding.provider == "codex"
    assert binding.thread_id == payload["thread_id"]
    assert binding.sync_cursor == 1
    assert binding.link_id is not None
    assert binding.link.metadata["kind"] == "chat"
    assert binding.link.metadata["chat_binding_id"] == str(binding.id)

    issue.refresh_from_db()
    assert issue.state_id == original_state_id
    assert issue.description_html.startswith("<p>Manual description.</p><p>Keep this.</p>")
    assert 'data-node-type="chat-progress"' in issue.description_html
    assert "Backend model" in issue.description_html
    assert "Manual description." in issue.description_stripped

    links_response = tenant["client"].get(
        f"/api/v1/workspaces/{tenant['workspace'].slug}/projects/{issue.project_id}/work-items/{issue.id}/links/"
    )
    assert links_response.status_code == status.HTTP_200_OK, links_response.content
    assert payload["url"] in str(links_response.data)


@pytest.mark.contract
@pytest.mark.django_db
def test_api_sync_accepts_native_codex_uuid_without_url(tenant):
    thread_id = str(uuid4())
    payload = _payload(cursor=1)
    payload["thread_id"] = thread_id
    payload.pop("url")

    response = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED, response.content
    binding = IssueChatBinding.objects.get(pk=response.data["id"])
    assert binding.thread_id == thread_id
    assert binding.link is not None
    assert binding.link.url == f"codex://threads/{thread_id}"


@pytest.mark.contract
@pytest.mark.django_db
def test_same_cursor_is_idempotent_and_stale_or_changed_cursor_is_rejected(tenant):
    issue = tenant["issue"]
    payload = _payload(cursor=2)
    first = tenant["client"].post(_url(tenant["workspace"], issue), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content
    issue.refresh_from_db()
    description_after_first = issue.description_html
    link_count = IssueLink.objects.filter(issue=issue).count()

    replay = tenant["client"].post(_url(tenant["workspace"], issue), payload, format="json")
    assert replay.status_code == status.HTTP_200_OK, replay.content
    assert replay.data["applied"] is False

    changed = {**payload, "progress": {**payload["progress"], "now": ["Different work"]}}
    reused = tenant["client"].post(_url(tenant["workspace"], issue), changed, format="json")
    assert reused.status_code == status.HTTP_409_CONFLICT
    assert reused.data["code"] == "cursor_reused"

    stale = tenant["client"].post(_url(tenant["workspace"], issue), {**payload, "cursor": 1}, format="json")
    assert stale.status_code == status.HTTP_409_CONFLICT
    assert stale.data["code"] == "stale_cursor"

    issue.refresh_from_db()
    assert issue.description_html == description_after_first
    assert IssueLink.objects.filter(issue=issue).count() == link_count


@pytest.mark.contract
@pytest.mark.django_db
def test_new_cursor_updates_progress_without_overwriting_later_manual_edits(tenant):
    issue = tenant["issue"]
    payload = _payload(cursor=1)
    first = tenant["client"].post(_url(tenant["workspace"], issue), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    issue.refresh_from_db()
    Issue.all_objects.filter(pk=issue.pk).update(
        description_html=f"<p>Added manually later.</p>{issue.description_html}"
    )

    updated_payload = {**payload, "cursor": 2, "progress": {**payload["progress"], "waiting": []}}
    second = tenant["client"].post(_url(tenant["workspace"], issue), updated_payload, format="json")

    assert second.status_code == status.HTTP_200_OK, second.content
    issue.refresh_from_db()
    assert issue.description_html.startswith("<p>Added manually later.</p><p>Manual description.</p>")
    assert issue.description_html.count('data-node-type="chat-progress"') == 1
    assert "Waiting:</strong> —" in issue.description_html


@pytest.mark.contract
@pytest.mark.django_db
def test_manual_edit_to_generated_block_returns_conflict_without_advancing_cursor(tenant):
    issue = tenant["issue"]
    payload = _payload(cursor=1)
    first = tenant["client"].post(_url(tenant["workspace"], issue), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    issue.refresh_from_db()
    edited_html = issue.description_html.replace("Focused tests", "Edited manually")
    Issue.all_objects.filter(pk=issue.pk).update(description_html=edited_html)

    second = tenant["client"].post(
        _url(tenant["workspace"], issue),
        {**payload, "cursor": 2, "progress": {**payload["progress"], "now": ["New sync"]}},
        format="json",
    )

    assert second.status_code == status.HTTP_409_CONFLICT
    assert second.data["code"] == "manual_edit"
    issue.refresh_from_db()
    binding = IssueChatBinding.objects.get(pk=first.data["id"])
    assert issue.description_html == edited_html
    assert binding.sync_cursor == 1
    assert binding.progress["now"] == ["Focused tests"]


@pytest.mark.contract
@pytest.mark.django_db
def test_stale_public_api_patch_keeps_manual_text_and_latest_generated_block(tenant):
    issue = tenant["issue"]
    payload = _payload(cursor=1)
    first = tenant["client"].post(_url(tenant["workspace"], issue), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    issue.refresh_from_db()
    stale_editor_html = issue.description_html.replace("Focused tests", "Spoofed generated text").replace(
        "Manual description.",
        "Manual API edit from the stale client.",
    )
    second = tenant["client"].post(
        _url(tenant["workspace"], issue),
        {**payload, "cursor": 2, "progress": {**payload["progress"], "now": ["Latest API work"]}},
        format="json",
    )
    assert second.status_code == status.HTTP_200_OK, second.content

    with (
        mock.patch("plane.api.views.issue.issue_activity.delay"),
        mock.patch("plane.api.views.issue.model_activity.delay"),
    ):
        response = tenant["client"].patch(
            _work_item_url(tenant["workspace"], issue),
            {"description_html": stale_editor_html},
            format="json",
        )

    assert response.status_code == status.HTTP_200_OK, response.content
    issue.refresh_from_db()
    assert "Manual API edit from the stale client." in issue.description_html
    assert "Latest API work" in issue.description_html
    assert "Spoofed generated text" not in issue.description_html
    assert issue.description_html.count('data-node-type="chat-progress"') == 1


@pytest.mark.contract
@pytest.mark.django_db
def test_provider_thread_can_only_bind_one_work_item(tenant):
    first_issue = tenant["issue"]
    second_issue = _issue(tenant["project"], "Second issue")
    payload = _payload(cursor=1)
    first = tenant["client"].post(_url(tenant["workspace"], first_issue), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    second = tenant["client"].post(_url(tenant["workspace"], second_issue), payload, format="json")

    assert second.status_code == status.HTTP_409_CONFLICT
    assert second.data["code"] == "thread_already_bound"
    assert IssueChatBinding.objects.filter(thread_id=payload["thread_id"]).count() == 1
    assert not IssueLink.objects.filter(issue=second_issue).exists()


@pytest.mark.contract
@pytest.mark.django_db
def test_database_constraint_prevents_duplicate_provider_thread(tenant):
    payload = _payload(cursor=1)
    first = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content
    other_issue = _issue(tenant["project"], "Other issue")

    with pytest.raises(IntegrityError), transaction.atomic():
        IssueChatBinding.objects.create(
            project=tenant["project"],
            issue=other_issue,
            provider=payload["provider"],
            thread_id=payload["thread_id"],
        )


@pytest.mark.contract
@pytest.mark.django_db
def test_endpoint_authentication_and_project_scope(tenant):
    issue = tenant["issue"]
    payload = _payload(cursor=1)

    anonymous = APIClient().post(_url(tenant["workspace"], issue), payload, format="json")
    assert anonymous.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)

    outsider = _user("outsider")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=outsider, role=15)
    outsider_response = _client_for(outsider).post(_url(tenant["workspace"], issue), payload, format="json")
    assert outsider_response.status_code == status.HTTP_403_FORBIDDEN

    sibling_project = _project(tenant["workspace"], tenant["owner"], "SIB")
    sibling_issue = _issue(sibling_project, "Sibling issue")
    wrong_project_url = _url(tenant["workspace"], issue).replace(str(issue.project_id), str(sibling_project.id))
    scoped = tenant["client"].post(wrong_project_url, {**payload, "thread_id": "scoped-thread"}, format="json")
    assert scoped.status_code == status.HTTP_404_NOT_FOUND
    assert not IssueChatBinding.objects.filter(thread_id="scoped-thread").exists()
    assert not IssueLink.objects.filter(issue=sibling_issue).exists()


@pytest.mark.contract
@pytest.mark.django_db
def test_api_chat_bindings_deny_project_guests_even_with_full_feature_access(tenant):
    tenant["project"].guest_view_all_features = True
    tenant["project"].save(update_fields=["guest_view_all_features"])
    first = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), _payload(), format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    guest = _user("guest")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=guest, role=5)
    ProjectMember.objects.create(project=tenant["project"], workspace=tenant["workspace"], member=guest, role=5)
    guest_client = _client_for(guest)
    assert not can_view_issue_chat_progress(guest, tenant["workspace"].slug, tenant["project"].id)

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
def test_api_sync_canonicalizes_codex_uuid_thread_ids(tenant):
    thread_id = str(uuid4()).upper()
    payload = _payload(cursor=1)
    payload["thread_id"] = thread_id
    payload.pop("url")

    response = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED, response.content
    binding = IssueChatBinding.objects.get(pk=response.data["id"])
    assert binding.thread_id == thread_id.lower()
    assert binding.link.url == f"codex://threads/{thread_id.lower()}"


@pytest.mark.contract
@pytest.mark.django_db
def test_api_sync_rejects_cursor_above_database_integer_bound(tenant):
    payload = {**_payload(cursor=1), "cursor": 9223372036854775808}

    response = tenant["client"].post(_url(tenant["workspace"], tenant["issue"]), payload, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "cursor" in response.data
    assert not IssueChatBinding.objects.filter(issue=tenant["issue"]).exists()


@pytest.mark.contract
@pytest.mark.django_db
def test_api_guest_work_item_list_and_detail_redact_generated_progress(tenant):
    issue = tenant["issue"]
    synced = tenant["client"].post(_url(tenant["workspace"], issue), _payload(cursor=1), format="json")
    assert synced.status_code == status.HTTP_201_CREATED, synced.content

    list_url = f"/api/v1/workspaces/{tenant['workspace'].slug}/projects/{issue.project_id}/work-items/"
    owner_list = tenant["client"].get(list_url)
    owner_detail = tenant["client"].get(_work_item_url(tenant["workspace"], issue))
    assert owner_list.status_code == status.HTTP_200_OK, owner_list.content
    assert owner_detail.status_code == status.HTTP_200_OK, owner_detail.content
    assert "Backend model" in str(owner_list.data)
    assert "Backend model" in owner_detail.data["description_html"]

    tenant["project"].guest_view_all_features = True
    tenant["project"].save(update_fields=["guest_view_all_features"])
    guest = _user("api-guest")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=guest, role=5)
    ProjectMember.objects.create(project=tenant["project"], workspace=tenant["workspace"], member=guest, role=5)
    guest_client = _client_for(guest)

    guest_list = guest_client.get(list_url)
    guest_detail = guest_client.get(_work_item_url(tenant["workspace"], issue))

    assert guest_list.status_code == status.HTTP_200_OK, guest_list.content
    assert guest_detail.status_code == status.HTTP_200_OK, guest_detail.content
    guest_list_html = str(guest_list.data["results"][0]["description_html"])
    guest_detail_html = str(guest_detail.data["description_html"])
    assert "Manual description." in guest_list_html
    assert "Backend model" not in guest_list_html
    assert "Focused tests" not in guest_list_html
    assert "chat-progress" not in guest_list_html
    assert "Manual description." in guest_detail_html
    assert "Backend model" not in guest_detail_html
    assert "Focused tests" not in guest_detail_html
    assert "chat-progress" not in guest_detail_html
