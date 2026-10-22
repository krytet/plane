# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
from datetime import timedelta
from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from plane.db.models import (
    Issue,
    IssueChatBinding,
    IssueChatWorkflowAudit,
    IssueChatWorkflowPolicy,
    Project,
    ProjectMember,
    State,
    StateGroup,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.utils.issue_chat import update_issue_chat_observation


def _tenant(policy_enabled=False):
    owner = User.objects.create(
        email=f"workflow-command-{uuid4().hex[:8]}@plane.so",
        username=f"workflow_command_{uuid4().hex[:8]}",
    )
    workspace = Workspace.objects.create(
        name="Workflow command",
        owner=owner,
        slug=f"workflow-command-{uuid4().hex[:8]}",
    )
    WorkspaceMember.objects.create(workspace=workspace, member=owner, role=20)
    project = Project.objects.create(
        name="Workflow command project",
        identifier=f"WC{uuid4().hex[:4].upper()}",
        workspace=workspace,
        created_by=owner,
    )
    ProjectMember.objects.create(
        project=project,
        workspace=workspace,
        member=owner,
        role=20,
    )
    states = {
        "source": State.objects.create(
            project=project,
            name="In Progress",
            color="#F59E0B",
            group=StateGroup.STARTED.value,
        ),
        "running": State.objects.create(
            project=project,
            name="LLM Work",
            color="#F59E0B",
            group=StateGroup.STARTED.value,
        ),
        "attention": State.objects.create(
            project=project,
            name="Requires attention",
            color="#F59E0B",
            group=StateGroup.STARTED.value,
        ),
        "manual_hold": State.objects.create(
            project=project,
            name="Come back later",
            color="#F59E0B",
            group=StateGroup.UNSTARTED.value,
        ),
        "completion": State.objects.create(
            project=project,
            name="LLM Complete",
            color="#F59E0B",
            group=StateGroup.STARTED.value,
        ),
    }
    issue = Issue.objects.create(
        project=project,
        workspace=workspace,
        name="Workflow command issue",
        state=states["source"],
    )
    binding = IssueChatBinding.objects.create(
        project=project,
        workspace=workspace,
        issue=issue,
        provider=IssueChatBinding.Provider.CODEX,
        thread_id=str(uuid4()),
        progress={},
    )
    if policy_enabled:
        IssueChatWorkflowPolicy.objects.create(
            project=project,
            workspace=workspace,
            source_state=states["source"],
            running_state=states["running"],
            attention_state=states["attention"],
            manual_hold_state=states["manual_hold"],
            completion_state=states["completion"],
            enabled=True,
            version=1,
            effective_at=timezone.now() - timedelta(minutes=5),
            stale_running_ttl_seconds=300,
        )
    return {
        "owner": owner,
        "workspace": workspace,
        "project": project,
        "states": states,
        "issue": issue,
        "binding": binding,
    }


@pytest.mark.unit
@pytest.mark.django_db
def test_configure_requires_explicit_enable_and_all_state_ids():
    tenant = _tenant()

    with pytest.raises(CommandError, match="explicit --enable or --disable"):
        call_command(
            "issue_chat_workflow",
            "configure",
            workspace=tenant["workspace"].slug,
            project_id=tenant["project"].id,
            operator_id=tenant["owner"].id,
            stdout=StringIO(),
        )

    with pytest.raises(CommandError, match="completion-state-id"):
        call_command(
            "issue_chat_workflow",
            "configure",
            workspace=tenant["workspace"].slug,
            project_id=tenant["project"].id,
            operator_id=tenant["owner"].id,
            enable=True,
            source_state_id=tenant["states"]["source"].id,
            running_state_id=tenant["states"]["running"].id,
            attention_state_id=tenant["states"]["attention"].id,
            manual_hold_state_id=tenant["states"]["manual_hold"].id,
            stdout=StringIO(),
        )


@pytest.mark.unit
@pytest.mark.django_db
def test_configure_explicitly_enables_exact_project_mapping():
    tenant = _tenant()
    stdout = StringIO()

    call_command(
        "issue_chat_workflow",
        "configure",
        workspace=tenant["workspace"].slug,
        project_id=tenant["project"].id,
        operator_id=tenant["owner"].id,
        enable=True,
        source_state_id=tenant["states"]["source"].id,
        running_state_id=tenant["states"]["running"].id,
        attention_state_id=tenant["states"]["attention"].id,
        manual_hold_state_id=tenant["states"]["manual_hold"].id,
        completion_state_id=tenant["states"]["completion"].id,
        stale_running_ttl_seconds=300,
        stdout=stdout,
    )

    policy = IssueChatWorkflowPolicy.objects.get(project=tenant["project"])
    assert policy.enabled is True
    assert policy.version == 1
    assert policy.completion_state_id == tenant["states"]["completion"].id
    payload = json.loads(stdout.getvalue())
    assert payload["writes_performed"] is True
    assert payload["historical_issues_mutated"] == 0
    assert payload["adopted_completion_issues"] == 0
    tenant["issue"].refresh_from_db()
    assert tenant["issue"].state_id == tenant["states"]["source"].id


@pytest.mark.unit
@pytest.mark.django_db
def test_diagnostics_include_binding_reasons_and_last_decisions_without_writes():
    tenant = _tenant(policy_enabled=True)
    update_issue_chat_observation(
        workspace_slug=tenant["workspace"].slug,
        project_id=tenant["project"].id,
        issue_id=tenant["issue"].id,
        binding_id=tenant["binding"].id,
        state=IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        observed_at=timezone.now(),
        turn_id="turn-diagnostics",
    )
    before = {
        "audits": IssueChatWorkflowAudit.objects.count(),
        "state_id": Issue.objects.get(pk=tenant["issue"].id).state_id,
    }
    stdout = StringIO()

    call_command(
        "issue_chat_workflow",
        "diagnostics",
        workspace=tenant["workspace"].slug,
        project_id=tenant["project"].id,
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    binding = next(row for row in payload["bindings"] if row["binding_id"] == str(tenant["binding"].id))
    assert payload["writes_performed"] is False
    assert binding["reason"] == "answer_ready_missing_report"
    assert binding["last_decision"]["reason"] == "answer_ready_missing_report"
    assert payload["recent_decisions"][0]["reason"] == "answer_ready_missing_report"
    assert IssueChatWorkflowAudit.objects.count() == before["audits"]
    assert Issue.objects.get(pk=tenant["issue"].id).state_id == before["state_id"]
