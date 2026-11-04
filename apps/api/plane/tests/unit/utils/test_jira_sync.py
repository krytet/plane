# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from django.contrib.auth import get_user_model

from plane.db.models import (
    Issue,
    JiraAutomationConsent,
    JiraIssueBinding,
    JiraOperationState,
    JiraProjectPolicy,
    JiraSiteRegistration,
    JiraUserGrant,
    Project,
    ProjectMember,
    State,
    Workspace,
    WorkspaceMember,
)
from plane.utils.jira_integration.crypto import encrypt_credential
from plane.utils.jira_integration.policy import compute_automation_scope_hash
from plane.utils.jira_integration.provider import JiraClient
from plane.utils.jira_integration.sync import dispatch_operation, poll_binding, queue_state_change


pytestmark = pytest.mark.django_db


class FakeJira:
    def get_transitions(self, issue_key):
        return [{"id": "transition-done", "to": {"id": "2", "name": "Done"}}]

    def transition_issue(self, issue_key, transition_id, fields=None):
        return {"fields": {"status": {"id": "2", "name": "Done"}}}

    def get_issue(self, issue_key):
        return {"fields": {"status": {"id": "2", "name": "Done"}}}


@pytest.fixture
def sync_fixture(db):
    User = get_user_model()
    owner = User.objects.create(email="sync-owner@plane.so", username="sync-owner", display_name="Owner")
    other = User.objects.create(email="sync-other@plane.so", username="sync-other", display_name="Other")
    workspace = Workspace.objects.create(name="Sync", slug="sync-workspace", owner=owner)
    WorkspaceMember.objects.create(workspace=workspace, member=owner, role=20)
    WorkspaceMember.objects.create(workspace=workspace, member=other, role=15)
    project = Project.objects.create(workspace=workspace, name="Sync project", identifier="SYNC")
    ProjectMember.objects.create(workspace=workspace, project=project, member=owner, role=20)
    ProjectMember.objects.create(workspace=workspace, project=project, member=other, role=15)
    todo = State.objects.create(
        workspace=workspace, project=project, name="Todo", group="unstarted", color="#999", default=True
    )
    done = State.objects.create(workspace=workspace, project=project, name="Done", group="completed", color="#999")
    issue = Issue.objects.create(
        workspace=workspace,
        project=project,
        name="Sync issue",
        description_html="<p>sync</p>",
        state=todo,
    )
    site = JiraSiteRegistration.objects.create(
        workspace=workspace,
        site_url="https://sync.atlassian.net",
        enabled=True,
    )
    JiraUserGrant.objects.create(
        site=site,
        user=owner,
        account_id="owner-account",
        email="sync-owner@plane.so",
        encrypted_credential=encrypt_credential("sync-token"),
        health="healthy",
    )
    policy = JiraProjectPolicy.objects.create(
        workspace=workspace,
        project=project,
        site=site,
        jira_project_key="FP",
        jira_issue_type_id="10001",
        direction="bidirectional",
        close_enabled=True,
        state_mappings={
            str(todo.id): {"jira_status_id": "1", "plane_state_id": str(todo.id)},
            str(done.id): {"jira_status_id": "2", "plane_state_id": str(done.id)},
        },
        enabled=True,
        created_by=owner,
    )
    binding = JiraIssueBinding.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        site=site,
        jira_issue_id="remote-1",
        jira_issue_key="FP-1",
        jira_project_key="FP",
        jira_status_id="1",
        jira_status_name="To Do",
        automation_owner=owner,
        automation_scope={},
        created_by=owner,
    )
    scope = {
        "direction": policy.direction,
        "state_mappings": policy.state_mappings,
        "close_enabled": policy.close_enabled,
        "reopen_enabled": policy.reopen_enabled,
        "jira_project_key": policy.jira_project_key,
        "jira_issue_type_id": policy.jira_issue_type_id,
        "allowed_events": [],
    }
    JiraAutomationConsent.objects.create(
        binding=binding,
        owner=owner,
        scope_hash=compute_automation_scope_hash(scope),
        scope=scope,
        created_by=owner,
    )
    binding.automation_scope = scope
    binding.save(update_fields=["automation_scope"])
    return {
        "user": owner,
        "other": other,
        "workspace": workspace,
        "project": project,
        "todo": todo,
        "done": done,
        "issue": issue,
        "binding": binding,
        "policy": policy,
    }


def test_outbound_state_change_is_queued_and_verified(sync_fixture, monkeypatch):
    fixture = sync_fixture
    fake = FakeJira()
    monkeypatch.setattr(JiraClient, "from_grant", lambda grant, **kwargs: fake)
    fixture["issue"].state = fixture["done"]
    fixture["issue"].save()
    operation = queue_state_change(issue=fixture["issue"], actor=fixture["user"], origin="human")
    assert operation is not None
    assert operation.state == JiraOperationState.QUEUED
    dispatch_operation(operation)
    operation.refresh_from_db()
    assert operation.state == JiraOperationState.SUCCEEDED
    fixture["binding"].refresh_from_db()
    assert fixture["binding"].jira_status_id == "2"


def test_human_change_without_personal_grant_is_blocked(sync_fixture):
    fixture = sync_fixture
    fixture["issue"].state = fixture["done"]
    fixture["issue"].save()
    operation = queue_state_change(issue=fixture["issue"], actor=fixture["other"], origin="human")
    assert operation is None


def test_inbound_poll_applies_only_with_current_consent(sync_fixture, monkeypatch):
    fixture = sync_fixture
    fake = FakeJira()
    monkeypatch.setattr(JiraClient, "from_grant", lambda grant, **kwargs: fake)
    result = poll_binding(fixture["binding"])
    fixture["issue"].refresh_from_db()
    assert result["applied"] is True
    assert fixture["issue"].state_id == fixture["done"].id
