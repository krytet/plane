# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import (
    APIToken,
    Issue,
    IssueChatBinding,
    Project,
    ProjectMember,
    State,
    User,
    Workspace,
    WorkspaceMember,
)


def _user(prefix):
    suffix = uuid4().hex[:8]
    return User.objects.create(email=f"{prefix}-{suffix}@plane.so", username=f"{prefix}_{suffix}")


def _issue(project):
    state, _ = State.objects.get_or_create(
        project=project,
        name="In Progress",
        defaults={"color": "#F59E0B", "group": "started"},
    )
    return Issue.objects.create(
        name="API presence",
        project=project,
        workspace=project.workspace,
        state=state,
        description_html="<p>Manual.</p>",
    )


def _app_bindings_url(workspace, issue):
    return f"/api/workspaces/{workspace.slug}/projects/{issue.project_id}/issues/{issue.id}/chat-bindings/"


def _observation_url(workspace, issue, binding):
    return (
        f"/api/v1/workspaces/{workspace.slug}/projects/{issue.project_id}/"
        f"work-items/{issue.id}/chat-bindings/{binding.id}/observations/"
    )


@pytest.fixture
def tenant(db):
    owner = _user("api-presence-owner")
    workspace = Workspace.objects.create(
        name="API Presence",
        owner=owner,
        slug=f"api-presence-{uuid4().hex[:8]}",
    )
    WorkspaceMember.objects.create(workspace=workspace, member=owner, role=20)
    project = Project.objects.create(
        name="API Presence Project",
        identifier="APIP",
        workspace=workspace,
        created_by=owner,
    )
    ProjectMember.objects.create(project=project, member=owner, workspace=workspace, role=20)
    issue = _issue(project)
    token = APIToken.objects.create(user=owner, label=f"presence-{uuid4().hex[:8]}")
    api_client = APIClient()
    api_client.credentials(HTTP_X_API_KEY=token.token)
    app_client = APIClient()
    app_client.force_login(owner)
    return {
        "owner": owner,
        "workspace": workspace,
        "project": project,
        "issue": issue,
        "token": token,
        "api_client": api_client,
        "app_client": app_client,
    }


def _create_binding(tenant, provider="codex"):
    payload = {
        "provider": provider,
        "thread_id": str(uuid4()) if provider == "codex" else f"chatgpt-{uuid4().hex}",
        "title": f"{provider} binding",
    }
    if provider == "chatgpt":
        payload["url"] = "https://chatgpt.com/c/example"
    response = tenant["app_client"].post(
        _app_bindings_url(tenant["workspace"], tenant["issue"]),
        payload,
        format="json",
    )
    assert response.status_code == status.HTTP_201_CREATED, response.content
    return IssueChatBinding.objects.get(pk=response.data["id"])


@pytest.mark.contract
@pytest.mark.django_db
def test_codex_observation_is_api_key_only_idempotent_and_ordered(tenant):
    binding = _create_binding(tenant)
    issue_state_id = tenant["issue"].state_id
    observed_at = timezone.now()
    url = _observation_url(tenant["workspace"], tenant["issue"], binding)
    payload = {
        "state": "running",
        "observed_at": observed_at.isoformat(),
        "turn_id": "turn-1",
    }

    first = tenant["api_client"].post(url, payload, format="json")
    assert first.status_code == status.HTTP_200_OK, first.content
    assert first.data["applied"] is True
    assert first.data["observation_state"] == "running"
    assert first.data["observation_source"] == "codex"
    assert first.data["observation_turn_id"] == "turn-1"
    assert first.data["attention"] is False
    assert first.data["unread"] is False
    assert "text" not in first.data

    replay = tenant["api_client"].post(url, payload, format="json")
    assert replay.status_code == status.HTTP_200_OK, replay.content
    assert replay.data["applied"] is False

    reused = tenant["api_client"].post(
        url,
        {**payload, "state": "answer_available"},
        format="json",
    )
    assert reused.status_code == status.HTTP_409_CONFLICT
    assert reused.data["code"] == "observation_reused"

    stale = tenant["api_client"].post(
        url,
        {
            **payload,
            "observed_at": (observed_at - timedelta(seconds=1)).isoformat(),
            "turn_id": "turn-old",
        },
        format="json",
    )
    assert stale.status_code == status.HTTP_409_CONFLICT
    assert stale.data["code"] == "stale_observation"

    binding.refresh_from_db()
    assert binding.observation_state == "running"
    assert binding.observation_turn_id == "turn-1"
    assert binding.sync_cursor == 0
    assert binding.last_synced_at is None
    assert binding.progress == {}
    tenant["issue"].refresh_from_db()
    assert tenant["issue"].state_id == issue_state_id

    session_client = APIClient()
    session_client.force_login(tenant["owner"])
    session_response = session_client.post(url, payload, format="json")
    assert session_response.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )


@pytest.mark.contract
@pytest.mark.django_db
def test_completed_turn_cannot_return_to_running(tenant):
    binding = _create_binding(tenant)
    url = _observation_url(tenant["workspace"], tenant["issue"], binding)
    observed_at = timezone.now()
    completed = tenant["api_client"].post(
        url,
        {
            "state": "answer_available",
            "observed_at": observed_at.isoformat(),
            "turn_id": "turn-completed",
        },
        format="json",
    )
    assert completed.status_code == status.HTTP_200_OK

    regression = tenant["api_client"].post(
        url,
        {
            "state": "running",
            "observed_at": (observed_at + timedelta(seconds=1)).isoformat(),
            "turn_id": "turn-completed",
        },
        format="json",
    )
    assert regression.status_code == status.HTTP_409_CONFLICT
    assert regression.data["code"] == "invalid_transition"
    binding.refresh_from_db()
    assert binding.observation_state == "answer_available"


@pytest.mark.contract
@pytest.mark.django_db
def test_answer_can_resume_same_turn_only_with_explicit_stop_resume_flag(tenant):
    binding = _create_binding(tenant)
    url = _observation_url(tenant["workspace"], tenant["issue"], binding)
    observed_at = timezone.now()
    completed = tenant["api_client"].post(
        url,
        {
            "state": "answer_available",
            "observed_at": observed_at.isoformat(),
            "turn_id": "turn-resume",
        },
        format="json",
    )
    assert completed.status_code == status.HTTP_200_OK

    implicit = tenant["api_client"].post(
        url,
        {
            "state": "running",
            "observed_at": (observed_at + timedelta(seconds=1)).isoformat(),
            "turn_id": "turn-resume",
        },
        format="json",
    )
    assert implicit.status_code == status.HTTP_409_CONFLICT
    assert implicit.data["code"] == "invalid_transition"

    explicit = tenant["api_client"].post(
        url,
        {
            "state": "running",
            "observed_at": (observed_at + timedelta(seconds=2)).isoformat(),
            "turn_id": "turn-resume",
            "resume_after_stop": True,
        },
        format="json",
    )
    assert explicit.status_code == status.HTTP_200_OK
    assert explicit.data["observation_state"] == "running"
    binding.refresh_from_db()
    assert binding.observation_state == "running"
    assert binding.observation_turn_id == "turn-resume"


@pytest.mark.contract
@pytest.mark.django_db
def test_failed_turn_cannot_resume_with_stop_resume_flag(tenant):
    binding = _create_binding(tenant)
    url = _observation_url(tenant["workspace"], tenant["issue"], binding)
    observed_at = timezone.now()
    failed = tenant["api_client"].post(
        url,
        {
            "state": "failed",
            "observed_at": observed_at.isoformat(),
            "turn_id": "turn-failed",
        },
        format="json",
    )
    assert failed.status_code == status.HTTP_200_OK

    regression = tenant["api_client"].post(
        url,
        {
            "state": "running",
            "observed_at": (observed_at + timedelta(seconds=1)).isoformat(),
            "turn_id": "turn-failed",
            "resume_after_stop": True,
        },
        format="json",
    )
    assert regression.status_code == status.HTTP_409_CONFLICT
    assert regression.data["code"] == "invalid_transition"


@pytest.mark.contract
@pytest.mark.django_db
def test_stop_resume_flag_is_rejected_for_non_running_observation(tenant):
    binding = _create_binding(tenant)
    response = tenant["api_client"].post(
        _observation_url(tenant["workspace"], tenant["issue"], binding),
        {
            "state": "answer_available",
            "observed_at": timezone.now().isoformat(),
            "turn_id": "turn-invalid-flag",
            "resume_after_stop": True,
        },
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "resume_after_stop" in response.data
    binding.refresh_from_db()
    assert binding.observation_state == "unknown"


@pytest.mark.contract
@pytest.mark.django_db
def test_observation_requires_existing_codex_binding_and_never_accepts_chat_text(tenant):
    binding = _create_binding(tenant)
    url = _observation_url(tenant["workspace"], tenant["issue"], binding)
    payload = {
        "state": "needs_action",
        "observed_at": timezone.now().isoformat(),
        "turn_id": "turn-text",
    }

    text_response = tenant["api_client"].post(
        url,
        {**payload, "text": "full chat transcript must not be stored"},
        format="json",
    )
    assert text_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "text" in text_response.data
    binding.refresh_from_db()
    assert binding.observation_state == "unknown"

    missing_url = (
        f"/api/v1/workspaces/{tenant['workspace'].slug}/projects/{tenant['issue'].project_id}/"
        f"work-items/{tenant['issue'].id}/chat-bindings/{uuid4()}/observations/"
    )
    missing = tenant["api_client"].post(missing_url, payload, format="json")
    assert missing.status_code == status.HTTP_404_NOT_FOUND
    assert IssueChatBinding.objects.filter(issue=tenant["issue"]).count() == 1

    chatgpt_binding = _create_binding(tenant, provider="chatgpt")
    chatgpt_response = tenant["api_client"].post(
        _observation_url(tenant["workspace"], tenant["issue"], chatgpt_binding),
        payload,
        format="json",
    )
    assert chatgpt_response.status_code == status.HTTP_409_CONFLICT
    assert chatgpt_response.data["code"] == "provider_observation_unsupported"
    chatgpt_binding.refresh_from_db()
    assert chatgpt_binding.observation_state == "unknown"
    assert chatgpt_binding.observation_source is None
