# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

import pytest
from django.utils import timezone
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
from plane.utils.codex_sync import TOKEN_LABEL, codex_sync_bot_username


def _user(prefix):
    suffix = uuid4().hex[:8]
    user = User.objects.create(
        email=f"{prefix}-{suffix}@plane.local",
        username=f"{prefix}_{suffix}",
    )
    user.set_password("test-password")
    user.save()
    return user


def _client(token):
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=token.token)
    return client


def _thread_id():
    value = uuid4()
    return f"{value.hex[:8]}-{value.hex[8:12]}-7{value.hex[13:16]}-8{value.hex[17:20]}-{value.hex[20:32]}"


def _tenant():
    owner = _user("owner")
    workspace = Workspace.objects.create(
        name="Codex auth workspace",
        owner=owner,
        slug=f"codex-auth-{uuid4().hex[:8]}",
    )
    WorkspaceMember.objects.create(workspace=workspace, member=owner, role=20)
    project = Project.objects.create(
        name="Codex auth project",
        identifier=f"CA{uuid4().hex[:5].upper()}",
        workspace=workspace,
        created_by=owner,
    )
    ProjectMember.objects.create(workspace=workspace, project=project, member=owner, role=20)
    state, _ = State.objects.get_or_create(
        project=project,
        name="In Progress",
        defaults={"color": "#F59E0B", "group": "started"},
    )
    issue = Issue.objects.create(
        name="Linked Codex issue",
        project=project,
        workspace=workspace,
        state=state,
        description_html="<p>Manual text.</p>",
    )
    thread_id = _thread_id()
    link = IssueLink.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        url=f"codex://threads/{thread_id}",
        title="Codex chat",
        metadata={"kind": "chat", "thread_id": thread_id},
        created_by=owner,
    )
    binding = IssueChatBinding.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        provider=IssueChatBinding.Provider.CODEX,
        thread_id=thread_id,
        link=link,
        created_by=owner,
    )
    return {
        "owner": owner,
        "workspace": workspace,
        "project": project,
        "issue": issue,
        "binding": binding,
        "thread_id": thread_id,
    }


def _bot_client(tenant, *, label=TOKEN_LABEL, user_type=1, include_workspace=True):
    bot = User.objects.create(
        email=f"{uuid4().hex}@local.invalid",
        username=codex_sync_bot_username(tenant["workspace"].id),
        is_bot=True,
        is_active=True,
    )
    bot.set_unusable_password()
    bot.save()
    WorkspaceMember.objects.create(workspace=tenant["workspace"], member=bot, role=15)
    ProjectMember.objects.create(
        workspace=tenant["workspace"],
        project=tenant["project"],
        member=bot,
        role=15,
    )
    token_options = {
        "label": label,
        "user": bot,
        "user_type": user_type,
    }
    if include_workspace:
        token_options["workspace"] = tenant["workspace"]
    token = APIToken.objects.create(**token_options)
    return _client(token)


def _binding_url(tenant):
    return (
        f"/api/v1/workspaces/{tenant['workspace'].slug}"
        f"/projects/{tenant['project'].id}"
        f"/work-items/{tenant['issue'].id}/chat-bindings/"
    )


def _work_item_url(tenant):
    return (
        f"/api/v1/workspaces/{tenant['workspace'].slug}"
        f"/projects/{tenant['project'].id}/work-items/{tenant['issue'].id}/"
    )


def _binding_payload(tenant, cursor=1):
    return {
        "provider": "codex",
        "thread_id": tenant["thread_id"],
        "url": f"codex://threads/{tenant['thread_id']}",
        "cursor": cursor,
        "progress": {
            "done": [],
            "now": ["Latest Codex answer (summary only)"],
            "waiting": [],
        },
    }


@pytest.mark.contract
@pytest.mark.django_db
def test_sync_bot_allows_only_existing_binding_progress_and_observation_routes():
    tenant = _tenant()
    client = _bot_client(tenant)
    binding_url = _binding_url(tenant)

    listed = client.get(binding_url)
    synced = client.post(binding_url, _binding_payload(tenant), format="json")
    observed = client.post(
        f"{binding_url}{tenant['binding'].id}/observations/",
        {
            "state": "answer_available",
            "observed_at": timezone.now().isoformat(),
            "turn_id": "turn-auth-scope",
        },
        format="json",
    )

    assert listed.status_code == status.HTTP_200_OK, listed.content
    assert synced.status_code in {status.HTTP_200_OK, status.HTTP_201_CREATED}, synced.content
    assert observed.status_code == status.HTTP_200_OK, observed.content


@pytest.mark.contract
@pytest.mark.django_db
def test_sync_bot_cannot_mutate_other_resources_or_create_new_bindings():
    tenant = _tenant()
    client = _bot_client(tenant)
    binding_url = _binding_url(tenant)

    work_item = client.get(_work_item_url(tenant))
    work_item_create = client.post(
        f"/api/v1/workspaces/{tenant['workspace'].slug}/projects/{tenant['project'].id}/work-items/",
        {"name": "Bot-created work item"},
        format="json",
    )
    project_create = client.post(
        f"/api/v1/workspaces/{tenant['workspace'].slug}/projects/",
        {"name": "Bot-created project", "identifier": "BOT"},
        format="json",
    )
    token_create = client.post("/api/users/api-tokens/", {"label": "Bot token"}, format="json")
    new_binding = client.post(
        binding_url,
        {**_binding_payload(tenant), "thread_id": _thread_id()},
        format="json",
    )
    wrong_provider = client.post(
        binding_url,
        {**_binding_payload(tenant), "provider": "chatgpt"},
        format="json",
    )

    for response in (
        work_item,
        work_item_create,
        project_create,
        token_create,
        new_binding,
        wrong_provider,
    ):
        assert response.status_code in {
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        }, response.content


@pytest.mark.contract
@pytest.mark.django_db
def test_sync_bot_is_denied_other_workspaces_and_unlinked_issues():
    tenant = _tenant()
    client = _bot_client(tenant)
    other = _tenant()

    other_workspace = client.get(_binding_url(other))
    unlinked_issue = Issue.objects.create(
        name="Unlinked issue",
        project=tenant["project"],
        workspace=tenant["workspace"],
        state=tenant["issue"].state,
        description_html="<p>No Codex binding.</p>",
    )
    unlinked = client.get(
        f"/api/v1/workspaces/{tenant['workspace'].slug}"
        f"/projects/{tenant['project'].id}/work-items/{unlinked_issue.id}/chat-bindings/"
    )

    assert other_workspace.status_code in {
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    }
    assert unlinked.status_code in {
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    }


@pytest.mark.contract
@pytest.mark.django_db
@pytest.mark.parametrize(
    "token_options",
    [
        {"label": "Wrong label"},
        {"user_type": 0},
        {"include_workspace": False},
    ],
)
def test_reserved_sync_bot_with_invalid_token_metadata_is_denied(token_options):
    tenant = _tenant()
    client = _bot_client(tenant, **token_options)

    response = client.get(_binding_url(tenant))

    assert response.status_code in {
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    }


@pytest.mark.contract
@pytest.mark.django_db
def test_ordinary_api_tokens_keep_general_workspace_access():
    tenant = _tenant()
    token = APIToken.objects.create(user=tenant["owner"], label="Human API token")
    client = _client(token)

    detail = client.get(_work_item_url(tenant))
    projects = client.get(f"/api/v1/workspaces/{tenant['workspace'].slug}/projects/")

    assert detail.status_code == status.HTTP_200_OK, detail.content
    assert projects.status_code == status.HTTP_200_OK, projects.content
