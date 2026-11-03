# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from rest_framework.test import APIClient

from plane.db.models import (
    Issue,
    JiraIssueBinding,
    JiraSiteRegistration,
    JiraUserGrant,
    Project,
    ProjectMember,
    State,
    WorkspaceMember,
)
from plane.utils.jira_integration.provider import JiraClient
from plane.utils.jira_integration.crypto import encrypt_credential


pytestmark = pytest.mark.django_db


def make_project(workspace, user, role=20):
    project = Project.objects.create(
        workspace=workspace,
        name="Jira test project",
        identifier="JTEST",
        default_assignee=user,
        project_lead=user,
    )
    ProjectMember.objects.create(workspace=workspace, project=project, member=user, role=role)
    state = State.objects.create(
        workspace=workspace,
        project=project,
        name="Todo",
        group="unstarted",
        color="#999999",
        default=True,
    )
    issue = Issue.objects.create(
        workspace=workspace,
        project=project,
        name="English integration issue",
        description_html="<p>Review the Jira integration.</p>",
        state=state,
    )
    return project, state, issue


@pytest.fixture
def jira_project(db, workspace, create_user):
    return make_project(workspace, create_user)


@pytest.fixture
def jira_client(create_user):
    client = APIClient()
    client.force_authenticate(user=create_user)
    return client


def test_personal_connection_and_readiness_do_not_leak_credentials(
    jira_client,
    workspace,
    create_user,
    jira_project,
):
    project, _, _ = jira_project
    response = jira_client.get(f"/api/workspaces/{workspace.slug}/integrations/jira/me/")
    assert response.status_code == 200
    assert response.data["connected"] is False

    readiness = jira_client.get(
        f"/api/workspaces/{workspace.slug}/integrations/jira/readiness/",
        {"project_id": str(project.id)},
    )
    assert readiness.status_code == 200
    assert readiness.data["blockers"][0]["code"] == "personal_grant_missing"
    assert "api_token" not in readiness.data


def test_personal_connection_uses_the_requesting_user(
    jira_client,
    workspace,
    create_user,
    monkeypatch,
):
    monkeypatch.setattr(
        JiraClient,
        "myself",
        lambda self: {"accountId": "account-1", "displayName": "Demo User", "emailAddress": "demo@plane.so"},
    )
    response = jira_client.post(
        f"/api/workspaces/{workspace.slug}/integrations/jira/me/",
        {
            "site_url": "https://example.atlassian.net",
            "email": "demo@plane.so",
            "api_token": "not-a-real-token",
        },
        format="json",
    )
    assert response.status_code == 201
    grant = JiraUserGrant.objects.get(user=create_user)
    assert grant.account_id == "account-1"
    assert "encrypted_credential" not in response.data
    assert "not-a-real-token" not in str(response.data)


def test_only_project_admin_can_save_jira_policy(jira_client, workspace, create_user, jira_project):
    project, state, _ = jira_project
    member = create_user.__class__.objects.create(
        email="member-jira@plane.so",
        username="member-jira",
        display_name="Member",
    )
    WorkspaceMember.objects.create(workspace=workspace, member=member, role=15)
    ProjectMember.objects.create(workspace=workspace, project=project, member=member, role=15)
    member_client = APIClient()
    member_client.force_authenticate(user=member)
    payload = {
        "jira_project_key": "FP",
        "jira_issue_type_id": "10001",
        "direction": "off",
        "state_mappings": {},
        "enabled": False,
    }
    response = member_client.put(
        f"/api/workspaces/{workspace.slug}/projects/{project.id}/integrations/jira/policy/",
        payload,
        format="json",
    )
    assert response.status_code == 403

    admin_payload = {
        **payload,
        "site_id": str(
            JiraSiteRegistration.objects.create(
                workspace=workspace,
                site_url="https://example.atlassian.net",
                enabled=True,
            ).id
        ),
    }
    response = jira_client.put(
        f"/api/workspaces/{workspace.slug}/projects/{project.id}/integrations/jira/policy/",
        admin_payload,
        format="json",
    )
    assert response.status_code == 200
    assert response.data["policy"]["jira_project_key"] == "FP"


def test_linked_binding_is_hidden_without_personal_grant(jira_client, workspace, jira_project, create_user):
    project, _, issue = jira_project
    site = JiraSiteRegistration.objects.create(
        workspace=workspace,
        site_url="https://example.atlassian.net",
        enabled=True,
    )
    JiraIssueBinding.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        site=site,
        jira_issue_id="10001",
        jira_issue_key="FP-10001",
        jira_project_key="FP",
        created_by=create_user,
    )
    response = jira_client.get(f"/api/workspaces/{workspace.slug}/projects/{project.id}/issues/{issue.id}/jira/")
    assert response.status_code == 200
    assert response.data["linked"] is True
    assert response.data["access_required"] is True
    assert "binding" not in response.data


def test_draft_generation_is_english_and_does_not_call_jira(jira_client, workspace, jira_project, create_user):
    project, _, issue = jira_project
    site = JiraSiteRegistration.objects.create(
        workspace=workspace,
        site_url="https://example.atlassian.net",
        enabled=True,
    )
    from plane.db.models import JiraProjectPolicy

    JiraProjectPolicy.objects.create(
        workspace=workspace,
        project=project,
        site=site,
        jira_project_key="FP",
        jira_issue_type_id="10001",
        direction="plane_to_jira",
        state_mappings={},
        enabled=True,
        created_by=create_user,
    )
    response = jira_client.post(
        f"/api/workspaces/{workspace.slug}/projects/{project.id}/issues/{issue.id}/jira/drafts/",
        {"summary": "Review the Jira integration", "description_markdown": "The integration needs review."},
        format="json",
    )
    assert response.status_code == 201
    assert response.data["summary"] == "Review the Jira integration"
    assert "# Context" in response.data["description_markdown"]


def test_link_existing_issue_verifies_remote_and_prevents_duplicate(
    jira_client,
    workspace,
    jira_project,
    create_user,
    monkeypatch,
):
    project, _, issue = jira_project
    site = JiraSiteRegistration.objects.create(
        workspace=workspace,
        site_url="https://example.atlassian.net",
        enabled=True,
    )
    JiraUserGrant.objects.create(
        site=site,
        user=create_user,
        account_id="link-account",
        email="link@plane.so",
        encrypted_credential=encrypt_credential("link-token"),
        health="healthy",
    )
    from plane.db.models import JiraProjectPolicy

    JiraProjectPolicy.objects.create(
        workspace=workspace,
        project=project,
        site=site,
        jira_project_key="FP",
        jira_issue_type_id="10001",
        direction="off",
        state_mappings={},
        enabled=False,
        created_by=create_user,
    )
    monkeypatch.setattr(
        JiraClient,
        "get_issue",
        lambda self, key: {
            "id": "remote-10001",
            "key": "FP-10001",
            "fields": {
                "summary": "Remote",
                "status": {"id": "1", "name": "To Do"},
                "project": {"key": "FP"},
                "issuetype": {"id": "10001"},
            },
        },
    )
    response = jira_client.post(
        f"/api/workspaces/{workspace.slug}/projects/{project.id}/issues/{issue.id}/jira/",
        {"issue_key": "FP-10001"},
        format="json",
    )
    assert response.status_code == 201
    assert response.data["binding"]["jira_issue_key"] == "FP-10001"
    duplicate = jira_client.post(
        f"/api/workspaces/{workspace.slug}/projects/{project.id}/issues/{issue.id}/jira/",
        {"issue_key": "FP-10001"},
        format="json",
    )
    assert duplicate.status_code == 200
    assert JiraIssueBinding.objects.filter(issue=issue, is_active=True).count() == 1
