# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

import json
from uuid import uuid4

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import (
    DeployBoard,
    Issue,
    IssueChatBinding,
    IssueChatBindingSeen,
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


def _issue(project, name="Presence"):
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
        description_html="<p>Manual description.</p>",
        description_json={"type": "doc", "content": [{"type": "paragraph"}]},
    )


def _client_for(user):
    client = APIClient()
    client.force_login(user)
    return client


def _bindings_url(workspace, issue):
    return f"/api/workspaces/{workspace.slug}/projects/{issue.project_id}/issues/{issue.id}/chat-bindings/"


def _binding_url(workspace, issue, action):
    return f"{_bindings_url(workspace, issue)}{issue.chat_bindings.get().id}/{action}/"


def _issues_url(workspace, issue):
    return f"/api/workspaces/{workspace.slug}/projects/{issue.project_id}/issues/"


@pytest.fixture
def tenant(db):
    owner = _user("presence-owner")
    workspace = _workspace(owner, "presence")
    project = _project(workspace, owner, "PRES")
    issue = _issue(project)
    return {
        "owner": owner,
        "workspace": workspace,
        "project": project,
        "issue": issue,
        "client": _client_for(owner),
    }


@pytest.mark.contract
@pytest.mark.django_db
def test_manual_binding_creation_does_not_fabricate_sync_or_progress(tenant):
    issue = tenant["issue"]
    original_description = issue.description_html
    thread_id = str(uuid4())

    response = tenant["client"].post(
        _bindings_url(tenant["workspace"], issue),
        {
            "provider": "codex",
            "thread_id": thread_id,
            "title": "Manual Codex link",
        },
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED, response.content
    assert response.data["applied"] is False
    binding = IssueChatBinding.objects.get(pk=response.data["id"])
    assert binding.provider == "codex"
    assert binding.thread_id == thread_id
    assert binding.sync_cursor == 0
    assert binding.last_synced_at is None
    assert binding.progress == {}
    assert binding.generated_block_hash == ""
    assert binding.observation_state == "unknown"
    assert binding.observation_source is None
    assert binding.observation_observed_at is None
    assert binding.link is not None
    assert binding.link.metadata["chat_binding_id"] == str(binding.id)

    issue.refresh_from_db()
    assert issue.description_html == original_description
    assert str(binding.id) not in issue.description_html


@pytest.mark.contract
@pytest.mark.django_db
def test_seen_is_plane_local_and_scoped_per_user(tenant):
    issue = tenant["issue"]
    binding = IssueChatBinding.objects.create(
        workspace=tenant["workspace"],
        project=tenant["project"],
        issue=issue,
        provider="codex",
        thread_id=f"thread-{uuid4().hex}",
        observation_state="answer_available",
        observation_source="codex",
        observation_observed_at=timezone.now(),
        observation_turn_id="turn-1",
    )
    member = _user("presence-member")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=member, role=15)
    ProjectMember.objects.create(
        project=tenant["project"],
        workspace=tenant["workspace"],
        member=member,
        role=15,
    )
    member_client = _client_for(member)

    owner_list = tenant["client"].get(_bindings_url(tenant["workspace"], issue))
    member_list = member_client.get(_bindings_url(tenant["workspace"], issue))
    assert owner_list.data[0]["unread"] is True
    assert member_list.data[0]["unread"] is True

    seen = tenant["client"].post(
        _binding_url(tenant["workspace"], issue, "seen"),
        {},
        format="json",
    )

    assert seen.status_code == status.HTTP_200_OK, seen.content
    assert seen.data["acknowledgement_source"] == "plane"
    assert seen.data["unread"] is False
    assert IssueChatBindingSeen.objects.filter(binding=binding, member=tenant["owner"]).exists()
    assert not IssueChatBindingSeen.objects.filter(binding=binding, member=member).exists()
    assert tenant["client"].get(_bindings_url(tenant["workspace"], issue)).data[0]["unread"] is False
    assert member_client.get(_bindings_url(tenant["workspace"], issue)).data[0]["unread"] is True

    binding.refresh_from_db()
    assert binding.observation_state == "answer_available"
    assert binding.observation_source == "codex"
    assert binding.observation_turn_id == "turn-1"


@pytest.mark.contract
@pytest.mark.django_db
def test_chatgpt_manual_attention_is_explicit_and_clearable(tenant):
    issue = tenant["issue"]
    original_state_id = issue.state_id
    created = tenant["client"].post(
        _bindings_url(tenant["workspace"], issue),
        {
            "provider": "chatgpt",
            "thread_id": f"chatgpt-{uuid4().hex}",
            "url": "https://chatgpt.com/c/example",
            "title": "Manual ChatGPT link",
        },
        format="json",
    )
    assert created.status_code == status.HTTP_201_CREATED, created.content
    binding = IssueChatBinding.objects.get(pk=created.data["id"])
    action_url = f"{_bindings_url(tenant['workspace'], issue)}{binding.id}/manual-attention/"

    set_response = tenant["client"].post(
        action_url,
        {"manual_attention": "needs_action"},
        format="json",
    )
    assert set_response.status_code == status.HTTP_200_OK, set_response.content
    assert set_response.data["observation_state"] == "needs_action"
    assert set_response.data["observation_source"] == "manual"
    assert set_response.data["manual_attention"] is True
    assert set_response.data["attention"] is True
    assert set_response.data["unread"] is True

    binding.refresh_from_db()
    assert binding.observation_source == "manual"
    assert binding.observation_observed_at is not None
    assert binding.observation_turn_id == ""

    clear_response = tenant["client"].post(
        action_url,
        {"manual_attention": "clear"},
        format="json",
    )
    assert clear_response.status_code == status.HTTP_200_OK, clear_response.content
    assert clear_response.data["observation_state"] == "unknown"
    assert clear_response.data["observation_source"] is None
    assert clear_response.data["manual_attention"] is False
    assert clear_response.data["unread"] is False

    issue.refresh_from_db()
    assert issue.state_id == original_state_id


@pytest.mark.contract
@pytest.mark.django_db
def test_internal_board_presence_is_grouped_but_hidden_from_guests_and_public_space(tenant):
    issue = tenant["issue"]
    binding = IssueChatBinding.objects.create(
        workspace=tenant["workspace"],
        project=tenant["project"],
        issue=issue,
        provider="codex",
        thread_id=f"board-thread-{uuid4().hex}",
        observation_state="failed",
        observation_source="codex",
        observation_observed_at=timezone.now(),
        observation_turn_id="board-turn",
    )

    grouped = tenant["client"].get(
        _issues_url(tenant["workspace"], issue),
        {"group_by": "state_id", "per_page": 100},
    )
    assert grouped.status_code == status.HTTP_200_OK, grouped.content
    group = next(
        item
        for item in grouped.data["results"].values()
        if any(str(card["id"]) == str(issue.id) for card in item["results"])
    )
    card = next(card for card in group["results"] if str(card["id"]) == str(issue.id))
    assert card["chat_presence"] == {
        "count": 1,
        "attention": 1,
        "unread": 1,
        "manual_attention": 0,
    }
    assert group["chat_presence"] == card["chat_presence"]

    tenant["project"].guest_view_all_features = True
    tenant["project"].save(update_fields=["guest_view_all_features"])
    guest = _user("presence-guest")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=guest, role=5)
    ProjectMember.objects.create(
        project=tenant["project"],
        workspace=tenant["workspace"],
        member=guest,
        role=5,
    )
    guest_response = _client_for(guest).get(
        _issues_url(tenant["workspace"], issue),
        {"group_by": "state_id", "per_page": 100},
    )
    assert guest_response.status_code == status.HTTP_200_OK, guest_response.content
    assert "chat_presence" not in json.dumps(guest_response.data, default=str)

    board = DeployBoard.objects.create(
        workspace=tenant["workspace"],
        project=tenant["project"],
        entity_name="project",
        entity_identifier=tenant["project"].id,
    )
    public_response = APIClient().get(
        f"/api/public/anchor/{board.anchor}/issues/",
        {"group_by": "state_id", "per_page": 100},
    )
    assert public_response.status_code == status.HTTP_200_OK, public_response.content
    public_serialized = json.dumps(public_response.data, default=str)
    for private_value in ("chat_presence", binding.thread_id, "observation_state", "failed"):
        assert private_value not in public_serialized


@pytest.mark.contract
@pytest.mark.django_db
def test_guest_cannot_acknowledge_or_set_manual_attention(tenant):
    issue = tenant["issue"]
    tenant["project"].guest_view_all_features = True
    tenant["project"].save(update_fields=["guest_view_all_features"])
    guest = _user("presence-guest-action")
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=guest, role=5)
    ProjectMember.objects.create(
        project=tenant["project"],
        workspace=tenant["workspace"],
        member=guest,
        role=5,
    )
    binding = IssueChatBinding.objects.create(
        workspace=tenant["workspace"],
        project=tenant["project"],
        issue=issue,
        provider="chatgpt",
        thread_id=f"guest-chatgpt-{uuid4().hex}",
    )
    client = _client_for(guest)

    seen = client.post(
        f"{_bindings_url(tenant['workspace'], issue)}{binding.id}/seen/",
        {},
        format="json",
    )
    manual = client.post(
        f"{_bindings_url(tenant['workspace'], issue)}{binding.id}/manual-attention/",
        {"manual_attention": "needs_action"},
        format="json",
    )

    assert seen.status_code == status.HTTP_403_FORBIDDEN
    assert manual.status_code == status.HTTP_403_FORBIDDEN
    assert not IssueChatBindingSeen.objects.filter(binding=binding, member=guest).exists()
    binding.refresh_from_db()
    assert binding.observation_state == "unknown"
