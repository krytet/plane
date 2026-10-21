# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest import mock
from uuid import uuid4

import pytest
from django.db import IntegrityError
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import (
    APIToken,
    Issue,
    IssueActivity,
    IssueChatBinding,
    IssueLink,
    Project,
    ProjectMember,
    State,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.utils.issue_chat import progress_block_id, scope_issue_links_for_user


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


def _result_rows(response):
    if isinstance(response.data, dict) and "results" in response.data:
        return response.data["results"]
    return response.data


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


@pytest.mark.contract
@pytest.mark.django_db
def test_issue_put_denies_a_foreign_user(tenant):
    outsider = _user("put-outsider")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=outsider, role=15)
    client = APIClient()
    client.force_login(outsider)

    response = client.put(
        _issue_url(tenant["workspace"], tenant["issue"]),
        {"name": "Hijacked", "description_html": "<p>Hijacked.</p>"},
        format="json",
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    tenant["issue"].refresh_from_db()
    assert tenant["issue"].name == "Chat progress"
    assert "Hijacked" not in tenant["issue"].description_html


@pytest.mark.contract
@pytest.mark.django_db
def test_issue_put_keeps_manual_text_and_latest_generated_block(tenant):
    issue = tenant["issue"]
    payload = _payload(cursor=1)
    first = tenant["client"].post(_url(tenant["workspace"], issue), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    issue.refresh_from_db()
    stale_editor_html = issue.description_html.replace("Focused tests", "Spoofed generated text").replace(
        "Keep this.",
        "Keep this manual PUT edit.",
    )
    second = tenant["client"].post(
        _url(tenant["workspace"], issue),
        {**payload, "cursor": 2, "progress": {**payload["progress"], "now": ["Latest PUT work"]}},
        format="json",
    )
    assert second.status_code == status.HTTP_200_OK, second.content

    with (
        mock.patch("plane.app.views.issue.base.issue_activity.delay"),
        mock.patch("plane.app.views.issue.base.model_activity.delay"),
        mock.patch("plane.app.views.issue.base.issue_description_version_task.delay"),
    ):
        response = tenant["client"].put(
            _issue_url(tenant["workspace"], issue),
            {"description_html": stale_editor_html},
            format="json",
        )

    assert response.status_code == status.HTTP_200_OK, response.content
    issue.refresh_from_db()
    assert "Keep this manual PUT edit." in issue.description_html
    assert "Latest PUT work" in issue.description_html
    assert "Spoofed generated text" not in issue.description_html
    assert issue.description_html.count('data-node-type="chat-progress"') == 1
    assert response.data["name"] == issue.name
    assert response.data["description_html"] == issue.description_html
    assert response.data["description_stripped"] == issue.description_stripped
    assert "description_json" in response.data


@pytest.mark.contract
@pytest.mark.django_db
def test_issue_put_rejects_a_manual_edit_to_generated_progress(tenant):
    issue = tenant["issue"]
    payload = _payload(cursor=1)
    first = tenant["client"].post(_url(tenant["workspace"], issue), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    issue.refresh_from_db()
    edited_html = issue.description_html.replace("Focused tests", "Edited through PUT")
    Issue.all_objects.filter(pk=issue.pk).update(description_html=edited_html)

    with (
        mock.patch("plane.app.views.issue.base.issue_activity.delay"),
        mock.patch("plane.app.views.issue.base.model_activity.delay"),
        mock.patch("plane.app.views.issue.base.issue_description_version_task.delay"),
    ):
        response = tenant["client"].put(
            _issue_url(tenant["workspace"], issue),
            {"description_html": edited_html},
            format="json",
        )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.data["code"] == "manual_edit"
    issue.refresh_from_db()
    assert issue.description_html == edited_html


@pytest.mark.contract
@pytest.mark.django_db
def test_guest_issue_description_redacts_generated_progress(tenant):
    issue = tenant["issue"]
    first = tenant["client"].post(_url(tenant["workspace"], issue), _payload(cursor=1), format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content

    with mock.patch("plane.app.views.issue.base.recent_visited_task.delay"):
        owner_view = tenant["client"].get(_issue_url(tenant["workspace"], issue))
    assert owner_view.status_code == status.HTTP_200_OK
    assert "Backend model" in owner_view.data["description_html"]

    tenant["project"].guest_view_all_features = True
    tenant["project"].save(update_fields=["guest_view_all_features"])
    guest = _user("description-guest")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=guest, role=5)
    ProjectMember.objects.create(project=tenant["project"], workspace=tenant["workspace"], member=guest, role=5)
    guest_client = APIClient()
    guest_client.force_login(guest)

    with mock.patch("plane.app.views.issue.base.recent_visited_task.delay"):
        guest_view = guest_client.get(_issue_url(tenant["workspace"], issue))

    assert guest_view.status_code == status.HTTP_200_OK
    assert "Manual description." in guest_view.data["description_html"]
    assert "Backend model" not in guest_view.data["description_html"]
    assert "Focused tests" not in guest_view.data["description_html"]
    assert "chat-progress" not in guest_view.data["description_html"]


@pytest.mark.contract
@pytest.mark.django_db
def test_guest_paginated_feed_redacts_generated_progress(tenant):
    issue = tenant["issue"]
    synced = tenant["client"].post(_url(tenant["workspace"], issue), _payload(cursor=1), format="json")
    assert synced.status_code == status.HTTP_201_CREATED, synced.content

    feed_url = f"/api/workspaces/{tenant['workspace'].slug}/projects/{issue.project_id}/v2/issues/"
    owner_feed = tenant["client"].get(feed_url, {"description": "true"})
    assert owner_feed.status_code == status.HTTP_200_OK, owner_feed.content
    owner_html = str(owner_feed.data["results"][0]["description_html"])
    assert "Backend model" in owner_html
    assert 'data-node-type="chat-progress"' in owner_html

    tenant["project"].guest_view_all_features = True
    tenant["project"].save(update_fields=["guest_view_all_features"])
    guest = _user("feed-guest")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=guest, role=5)
    ProjectMember.objects.create(project=tenant["project"], workspace=tenant["workspace"], member=guest, role=5)
    guest_client = APIClient()
    guest_client.force_login(guest)

    guest_feed = guest_client.get(feed_url, {"description": "true"})

    assert guest_feed.status_code == status.HTTP_200_OK, guest_feed.content
    guest_html = str(guest_feed.data["results"][0]["description_html"])
    assert "Manual description." in guest_html
    assert "Backend model" not in guest_html
    assert "Focused tests" not in guest_html
    assert "chat-progress" not in guest_html


@pytest.mark.contract
@pytest.mark.django_db
def test_guest_history_redacts_generated_progress_values_and_issue_detail(tenant):
    issue = tenant["issue"]
    synced = tenant["client"].post(_url(tenant["workspace"], issue), _payload(cursor=1), format="json")
    assert synced.status_code == status.HTTP_201_CREATED, synced.content
    issue.refresh_from_db()
    IssueActivity.objects.create(
        issue=issue,
        project=tenant["project"],
        workspace=tenant["workspace"],
        actor=tenant["owner"],
        verb="updated",
        field="description",
        old_value="<p>Manual description.</p>",
        new_value=issue.description_html,
    )

    history_url = f"/api/workspaces/{tenant['workspace'].slug}/projects/{issue.project_id}/issues/{issue.id}/history/"
    owner_history = tenant["client"].get(history_url)
    assert owner_history.status_code == status.HTTP_200_OK, owner_history.content
    owner_activity = next(row for row in owner_history.data if row["field"] == "description")
    assert "Backend model" in owner_activity["new_value"]

    tenant["project"].guest_view_all_features = True
    tenant["project"].save(update_fields=["guest_view_all_features"])
    guest = _user("history-guest")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=guest, role=5)
    ProjectMember.objects.create(project=tenant["project"], workspace=tenant["workspace"], member=guest, role=5)
    guest_client = APIClient()
    guest_client.force_login(guest)

    guest_history = guest_client.get(history_url)

    assert guest_history.status_code == status.HTTP_200_OK, guest_history.content
    guest_activity = next(row for row in guest_history.data if row["field"] == "description")
    serialized = str(guest_history.data)
    assert "Manual description." in guest_activity["new_value"]
    assert "Backend model" not in serialized
    assert "Focused tests" not in serialized
    assert "chat-progress" not in serialized


@pytest.mark.contract
@pytest.mark.django_db
def test_generic_issue_link_reads_hide_managed_chat_links_from_guests(tenant):
    issue = tenant["issue"]
    synced = tenant["client"].post(_url(tenant["workspace"], issue), _payload(cursor=1), format="json")
    assert synced.status_code == status.HTTP_201_CREATED, synced.content
    binding = IssueChatBinding.objects.get(pk=synced.data["id"])
    regular_link = IssueLink.objects.create(
        workspace=tenant["workspace"],
        project=tenant["project"],
        issue=issue,
        url="https://example.com/regular-link",
        title="Regular link",
        metadata={"source": "manual"},
    )
    links_url = f"/api/workspaces/{tenant['workspace'].slug}/projects/{issue.project_id}/issues/{issue.id}/issue-links/"

    owner_list = tenant["client"].get(links_url)
    owner_urls = {row["url"] for row in _result_rows(owner_list)}
    assert owner_list.status_code == status.HTTP_200_OK, owner_list.content
    assert owner_urls == {binding.link.url, regular_link.url}

    tenant["project"].guest_view_all_features = True
    tenant["project"].save(update_fields=["guest_view_all_features"])
    guest = _user("link-guest")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=guest, role=5)
    ProjectMember.objects.create(project=tenant["project"], workspace=tenant["workspace"], member=guest, role=5)
    guest_client = APIClient()
    guest_client.force_login(guest)

    guest_link_scope = IssueLink.objects.filter(
        workspace__slug=tenant["workspace"].slug,
        project_id=tenant["project"].id,
        issue_id=issue.id,
        project__project_projectmember__member=guest,
        project__project_projectmember__is_active=True,
    )
    assert set(guest_link_scope.values_list("url", flat=True)) == {
        binding.link.url,
        regular_link.url,
    }
    scoped_links = scope_issue_links_for_user(
        guest_link_scope,
        guest,
        tenant["workspace"].slug,
        tenant["project"].id,
    )
    assert set(scoped_links.values_list("url", flat=True)) == {regular_link.url}

    guest_list = guest_client.get(links_url)
    guest_urls = {row["url"] for row in _result_rows(guest_list)}
    assert guest_list.status_code == status.HTTP_200_OK, guest_list.content
    assert guest_urls == {regular_link.url}

    hidden_detail = guest_client.get(f"{links_url}{binding.link_id}/")
    regular_detail = guest_client.get(f"{links_url}{regular_link.id}/")
    assert hidden_detail.status_code == status.HTTP_404_NOT_FOUND
    assert regular_detail.status_code == status.HTTP_200_OK, regular_detail.content
    assert regular_detail.data["url"] == regular_link.url


@pytest.mark.contract
@pytest.mark.django_db
@pytest.mark.parametrize("http_method", ["patch", "put"])
def test_json_only_issue_update_keeps_latest_managed_progress(tenant, http_method):
    issue = tenant["issue"]
    payload = _payload(cursor=1)
    first = tenant["client"].post(_url(tenant["workspace"], issue), payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED, first.content
    binding = IssueChatBinding.objects.get(pk=first.data["id"])

    stale_json = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Manual JSON text."}]},
            {
                "type": "chatProgress",
                "attrs": {"id": progress_block_id(binding.id), "progress": binding.progress},
            },
        ],
    }
    Issue.all_objects.filter(pk=issue.pk).update(description_json=stale_json)
    second = tenant["client"].post(
        _url(tenant["workspace"], issue),
        {**payload, "cursor": 2, "progress": {**payload["progress"], "now": ["Latest JSON work"]}},
        format="json",
    )
    assert second.status_code == status.HTTP_200_OK, second.content

    with (
        mock.patch("plane.app.views.issue.base.issue_activity.delay"),
        mock.patch("plane.app.views.issue.base.model_activity.delay"),
        mock.patch("plane.app.views.issue.base.issue_description_version_task.delay"),
    ):
        response = getattr(tenant["client"], http_method)(
            _issue_url(tenant["workspace"], issue),
            {"description_json": stale_json},
            format="json",
        )

    assert response.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT), response.content
    issue.refresh_from_db()
    json_nodes = [
        node for node in issue.description_json.get("content", []) if node.get("type") == "chatProgress"
    ]
    assert len(json_nodes) == 1
    assert json_nodes[0]["attrs"]["progress"]["now"] == ["Latest JSON work"]
    assert any(
        node.get("type") == "paragraph"
        and node.get("content", [{}])[0].get("text") == "Manual JSON text."
        for node in issue.description_json.get("content", [])
    )
    assert "Latest JSON work" in issue.description_html


@pytest.mark.contract
@pytest.mark.django_db
def test_activity_feeds_and_csv_redact_managed_progress_for_guests(tenant):
    issue = tenant["issue"]
    synced = tenant["client"].post(_url(tenant["workspace"], issue), _payload(cursor=1), format="json")
    assert synced.status_code == status.HTTP_201_CREATED, synced.content
    issue.refresh_from_db()
    owner_activity = IssueActivity.objects.create(
        issue=issue,
        workspace=tenant["workspace"],
        project=tenant["project"],
        actor=tenant["owner"],
        verb="updated",
        field="description",
        old_value="<p>Old manual.</p>",
        new_value=issue.description_html,
    )

    tenant["project"].guest_view_all_features = True
    tenant["project"].save(update_fields=["guest_view_all_features"])
    guest = _user("feed-activity-guest")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=guest, role=15)
    ProjectMember.objects.create(project=tenant["project"], workspace=tenant["workspace"], member=guest, role=5)
    guest_client = APIClient()
    guest_client.force_login(guest)
    IssueActivity.objects.create(
        issue=issue,
        workspace=tenant["workspace"],
        project=tenant["project"],
        actor=guest,
        verb="updated",
        field="description",
        old_value="<p>Old guest manual.</p>",
        new_value=issue.description_html,
    )

    workspace_activity_url = (
        f"/api/workspaces/{tenant['workspace'].slug}/user-activity/{tenant['owner'].id}/"
    )
    owner_feed = tenant["client"].get(workspace_activity_url)
    owner_serialized = str(_result_rows(owner_feed))
    assert owner_feed.status_code == status.HTTP_200_OK, owner_feed.content
    assert "Backend model" in owner_serialized
    assert any(str(row["id"]) == str(owner_activity.id) for row in _result_rows(owner_feed))

    guest_feed = guest_client.get(workspace_activity_url)
    guest_serialized = str(_result_rows(guest_feed))
    assert guest_feed.status_code == status.HTTP_200_OK, guest_feed.content
    assert "Manual description." in guest_serialized
    assert "Backend model" not in guest_serialized
    assert "Focused tests" not in guest_serialized
    assert "chat-progress" not in guest_serialized

    user_feed = guest_client.get("/api/users/me/activities/")
    user_serialized = str(_result_rows(user_feed))
    assert user_feed.status_code == status.HTTP_200_OK, user_feed.content
    assert "Backend model" not in user_serialized
    assert "Focused tests" not in user_serialized
    assert "chat-progress" not in user_serialized

    export_url = f"{workspace_activity_url}export/"
    csv_response = guest_client.post(
        export_url,
        {"date": str(timezone.localdate())},
        format="json",
    )
    csv_content = csv_response.content.decode()
    assert csv_response.status_code == status.HTTP_200_OK, csv_content
    assert "Backend model" not in csv_content
    assert "Focused tests" not in csv_content
    assert "chat-progress" not in csv_content
