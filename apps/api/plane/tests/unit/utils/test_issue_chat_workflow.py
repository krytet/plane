# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from plane.db.models import (
    Issue,
    IssueChatBinding,
    IssueChatWorkflowAudit,
    IssueChatWorkflowCursor,
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
from plane.utils.issue_chat_workflow import (
    IssueChatWorkflowConfigurationError,
    configure_issue_chat_workflow,
    record_verified_issue_chat_completion,
)


def _user(prefix):
    suffix = uuid4().hex[:8]
    return User.objects.create(
        email=f"{prefix}-{suffix}@plane.so",
        username=f"{prefix}_{suffix}",
        is_active=True,
    )


def _state(project, name, group=StateGroup.STARTED.value):
    return State.objects.create(
        project=project,
        name=name,
        color="#F59E0B",
        group=group,
    )


def _binding(issue):
    return IssueChatBinding.objects.create(
        project=issue.project,
        workspace=issue.workspace,
        issue=issue,
        provider=IssueChatBinding.Provider.CODEX,
        thread_id=str(uuid4()),
        progress={},
    )


@pytest.fixture
def workflow_tenant(db):
    owner = _user("workflow-owner")
    workspace = Workspace.objects.create(
        name="Workflow",
        owner=owner,
        slug=f"workflow-{uuid4().hex[:8]}",
    )
    WorkspaceMember.objects.create(workspace=workspace, member=owner, role=20)
    project = Project.objects.create(
        name="Workflow project",
        identifier=f"WF{uuid4().hex[:4].upper()}",
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
        "source": _state(project, "In Progress"),
        "running": _state(project, "LLM Work"),
        "attention": _state(project, "Requires attention"),
        "manual_hold": _state(project, "Come back later", StateGroup.UNSTARTED.value),
        "completion": _state(project, "LLM Complete"),
        "backlog": _state(project, "Backlog", StateGroup.BACKLOG.value),
        "unstarted": _state(project, "Todo", StateGroup.UNSTARTED.value),
        "completed": _state(project, "Done", StateGroup.COMPLETED.value),
        "cancelled": _state(project, "Cancelled", StateGroup.CANCELLED.value),
    }
    issue = Issue.objects.create(
        project=project,
        workspace=workspace,
        name="Automatic board workflow",
        state=states["source"],
        description_html="<p>Manual description stays.</p>",
        description_json={"type": "doc", "content": [{"type": "paragraph"}]},
    )
    effective_at = timezone.now() - timedelta(minutes=10)
    policy = IssueChatWorkflowPolicy.objects.create(
        project=project,
        workspace=workspace,
        source_state=states["source"],
        running_state=states["running"],
        attention_state=states["attention"],
        manual_hold_state=states["manual_hold"],
        completion_state=states["completion"],
        enabled=True,
        version=1,
        effective_at=effective_at,
        stale_running_ttl_seconds=300,
    )
    binding = _binding(issue)
    return {
        "owner": owner,
        "workspace": workspace,
        "project": project,
        "states": states,
        "issue": issue,
        "policy": policy,
        "binding": binding,
        "effective_at": effective_at,
    }


def _observe(tenant, binding, state, observed_at, turn_id, *, resume_after_stop=False):
    result = update_issue_chat_observation(
        workspace_slug=tenant["workspace"].slug,
        project_id=tenant["project"].id,
        issue_id=tenant["issue"].id,
        binding_id=binding.id,
        state=state,
        observed_at=observed_at,
        turn_id=turn_id,
        resume_after_stop=resume_after_stop,
    )
    tenant["issue"].refresh_from_db()
    return result


def _configure_workflow(tenant, *, todo=False):
    return configure_issue_chat_workflow(
        workspace_slug=tenant["workspace"].slug,
        project_id=tenant["project"].id,
        operator_id=tenant["owner"].id,
        enabled=True,
        source_state_id=tenant["states"]["source"].id,
        running_state_id=tenant["states"]["running"].id,
        attention_state_id=tenant["states"]["attention"].id,
        manual_hold_state_id=tenant["states"]["manual_hold"].id,
        completion_state_id=tenant["states"]["completion"].id,
        todo_state_id=tenant["states"]["unstarted"].id if todo else None,
        stale_running_ttl_seconds=300,
    )


@pytest.mark.unit
@pytest.mark.django_db
def test_disabled_policy_never_changes_issue_state(workflow_tenant):
    tenant = workflow_tenant
    tenant["policy"].enabled = False
    tenant["policy"].save(update_fields=["enabled", "updated_at"])

    result = _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        timezone.now(),
        "turn-disabled",
    )

    assert result.applied is True
    assert tenant["issue"].state_id == tenant["states"]["source"].id
    assert not IssueChatWorkflowAudit.objects.filter(issue=tenant["issue"]).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_configuration_rejects_foreign_project_and_duplicate_state_mappings(workflow_tenant):
    tenant = workflow_tenant
    other_project = Project.objects.create(
        name="Other",
        identifier=f"OT{uuid4().hex[:4].upper()}",
        workspace=tenant["workspace"],
        created_by=tenant["owner"],
    )
    ProjectMember.objects.create(
        project=other_project,
        workspace=tenant["workspace"],
        member=tenant["owner"],
        role=20,
    )
    foreign_state = _state(other_project, "LLM Complete")

    with pytest.raises(IssueChatWorkflowConfigurationError, match="exact target project"):
        configure_issue_chat_workflow(
            workspace_slug=tenant["workspace"].slug,
            project_id=tenant["project"].id,
            operator_id=tenant["owner"].id,
            enabled=True,
            source_state_id=tenant["states"]["source"].id,
            running_state_id=tenant["states"]["running"].id,
            attention_state_id=tenant["states"]["attention"].id,
            manual_hold_state_id=tenant["states"]["manual_hold"].id,
            completion_state_id=foreign_state.id,
            stale_running_ttl_seconds=300,
        )

    with pytest.raises(IssueChatWorkflowConfigurationError, match="distinct"):
        configure_issue_chat_workflow(
            workspace_slug=tenant["workspace"].slug,
            project_id=tenant["project"].id,
            operator_id=tenant["owner"].id,
            enabled=True,
            source_state_id=tenant["states"]["source"].id,
            running_state_id=tenant["states"]["source"].id,
            attention_state_id=tenant["states"]["attention"].id,
            manual_hold_state_id=tenant["states"]["manual_hold"].id,
            completion_state_id=tenant["states"]["completion"].id,
            stale_running_ttl_seconds=300,
        )


@pytest.mark.unit
@pytest.mark.django_db
def test_configuration_requires_started_states_and_exact_llm_complete(workflow_tenant):
    tenant = workflow_tenant
    not_complete = _state(tenant["project"], "Almost Complete")

    with pytest.raises(IssueChatWorkflowConfigurationError, match="LLM Complete"):
        configure_issue_chat_workflow(
            workspace_slug=tenant["workspace"].slug,
            project_id=tenant["project"].id,
            operator_id=tenant["owner"].id,
            enabled=True,
            source_state_id=tenant["states"]["source"].id,
            running_state_id=tenant["states"]["running"].id,
            attention_state_id=tenant["states"]["attention"].id,
            manual_hold_state_id=tenant["states"]["manual_hold"].id,
            completion_state_id=not_complete.id,
            stale_running_ttl_seconds=300,
        )

    with pytest.raises(IssueChatWorkflowConfigurationError, match="started group"):
        configure_issue_chat_workflow(
            workspace_slug=tenant["workspace"].slug,
            project_id=tenant["project"].id,
            operator_id=tenant["owner"].id,
            enabled=True,
            source_state_id=tenant["states"]["unstarted"].id,
            running_state_id=tenant["states"]["running"].id,
            attention_state_id=tenant["states"]["attention"].id,
            manual_hold_state_id=tenant["states"]["manual_hold"].id,
            completion_state_id=tenant["states"]["completion"].id,
            stale_running_ttl_seconds=300,
        )


@pytest.mark.unit
@pytest.mark.django_db
def test_configuration_accepts_real_board_shape_with_unstarted_manual_hold(workflow_tenant):
    tenant = workflow_tenant

    result = configure_issue_chat_workflow(
        workspace_slug=tenant["workspace"].slug,
        project_id=tenant["project"].id,
        operator_id=tenant["owner"].id,
        enabled=True,
        source_state_id=tenant["states"]["source"].id,
        running_state_id=tenant["states"]["running"].id,
        attention_state_id=tenant["states"]["attention"].id,
        manual_hold_state_id=tenant["states"]["manual_hold"].id,
        completion_state_id=tenant["states"]["completion"].id,
        todo_state_id=tenant["states"]["unstarted"].id,
        stale_running_ttl_seconds=300,
    )

    assert result.policy.enabled is True
    assert result.policy.manual_hold_state.group == StateGroup.UNSTARTED.value
    assert result.policy.todo_state_id == tenant["states"]["unstarted"].id
    assert result.policy.todo_state_id != result.policy.manual_hold_state_id
    started_mappings = {
        result.policy.source_state_id,
        result.policy.running_state_id,
        result.policy.attention_state_id,
        result.policy.completion_state_id,
    }
    assert len(started_mappings) == 4
    assert all(
        getattr(result.policy, field).group == StateGroup.STARTED.value
        for field in ("source_state", "running_state", "attention_state", "completion_state")
    )


@pytest.mark.unit
@pytest.mark.django_db
def test_configuration_rejects_started_manual_hold(workflow_tenant):
    tenant = workflow_tenant
    started_hold = _state(tenant["project"], "Started hold")

    with pytest.raises(IssueChatWorkflowConfigurationError, match="unstarted group"):
        configure_issue_chat_workflow(
            workspace_slug=tenant["workspace"].slug,
            project_id=tenant["project"].id,
            operator_id=tenant["owner"].id,
            enabled=True,
            source_state_id=tenant["states"]["source"].id,
            running_state_id=tenant["states"]["running"].id,
            attention_state_id=tenant["states"]["attention"].id,
            manual_hold_state_id=started_hold.id,
            completion_state_id=tenant["states"]["completion"].id,
            stale_running_ttl_seconds=300,
        )


@pytest.mark.unit
@pytest.mark.django_db
def test_configuration_rejects_started_todo_state(workflow_tenant):
    tenant = workflow_tenant
    started_todo = _state(tenant["project"], "Started Todo")

    with pytest.raises(IssueChatWorkflowConfigurationError, match="todo_state must use the unstarted group"):
        configure_issue_chat_workflow(
            workspace_slug=tenant["workspace"].slug,
            project_id=tenant["project"].id,
            operator_id=tenant["owner"].id,
            enabled=True,
            source_state_id=tenant["states"]["source"].id,
            running_state_id=tenant["states"]["running"].id,
            attention_state_id=tenant["states"]["attention"].id,
            manual_hold_state_id=tenant["states"]["manual_hold"].id,
            completion_state_id=tenant["states"]["completion"].id,
            todo_state_id=started_todo.id,
            stale_running_ttl_seconds=300,
        )


@pytest.mark.unit
@pytest.mark.django_db
def test_existing_completion_is_adopted_without_claiming_verified_report(workflow_tenant):
    tenant = workflow_tenant
    snapshot_at = timezone.now() - timedelta(minutes=2)
    IssueChatBinding.objects.filter(pk=tenant["binding"].id).update(
        observation_state=IssueChatBinding.ObservationState.RUNNING,
        observation_source=IssueChatBinding.ObservationSource.CODEX,
        observation_observed_at=snapshot_at,
        observation_turn_id="turn-existing",
    )
    tenant["issue"].state = tenant["states"]["completion"]
    tenant["issue"].save(update_fields=["state", "updated_at"])

    result = _configure_workflow(tenant)

    assert result.adopted_completion_issues == 1
    cursor = IssueChatWorkflowCursor.objects.get(issue=tenant["issue"])
    assert cursor.completion_cutoff_source == "existing_state"
    assert cursor.completion_cutoff_at is not None
    assert cursor.completion_snapshots[0]["source"] == "existing_state"
    assert cursor.completion_snapshots[0]["records"] == [
        {
            "binding_id": str(tenant["binding"].id),
            "turn_id": "turn-existing",
            "observation_state": "running",
            "observed_at": snapshot_at.isoformat(timespec="microseconds"),
        }
    ]

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        cursor.completion_cutoff_at + timedelta(seconds=1),
        "turn-existing",
    )
    assert tenant["issue"].state_id == tenant["states"]["completion"].id

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        cursor.completion_cutoff_at + timedelta(seconds=2),
        "turn-new",
    )
    assert tenant["issue"].state_id == tenant["states"]["attention"].id


@pytest.mark.unit
@pytest.mark.django_db
def test_completion_snapshots_and_manual_cutoff_survive_policy_revisions(workflow_tenant):
    tenant = workflow_tenant
    IssueChatBinding.objects.filter(pk=tenant["binding"].id).update(
        observation_state=IssueChatBinding.ObservationState.RUNNING,
        observation_source=IssueChatBinding.ObservationSource.CODEX,
        observation_observed_at=timezone.now() - timedelta(minutes=1),
        observation_turn_id="turn-adopted",
    )
    tenant["issue"].state = tenant["states"]["completion"]
    tenant["issue"].save(update_fields=["state", "updated_at"])
    _configure_workflow(tenant)
    cursor = IssueChatWorkflowCursor.objects.get(issue=tenant["issue"])
    manual_cutoff = timezone.now() - timedelta(hours=1)
    cursor.manual_resume_cutoff_at = manual_cutoff
    cursor.manual_resume_cutoff_source = "source_state"
    cursor.save(
        update_fields=[
            "manual_resume_cutoff_at",
            "manual_resume_cutoff_source",
            "updated_at",
        ]
    )
    record_verified_issue_chat_completion(
        issue=tenant["issue"],
        state=tenant["states"]["completion"],
        previous_state_id=tenant["states"]["source"].id,
        verified_at=timezone.now(),
    )

    _configure_workflow(tenant)

    cursor.refresh_from_db()
    assert cursor.manual_resume_cutoff_at == manual_cutoff
    assert cursor.manual_resume_cutoff_source == "source_state"
    assert [snapshot["source"] for snapshot in cursor.completion_snapshots] == [
        "existing_state",
        "verified_report",
        "existing_state",
    ]


@pytest.mark.unit
@pytest.mark.django_db
def test_configuration_locks_completion_issues_before_policy(workflow_tenant):
    tenant = workflow_tenant
    tenant["issue"].state = tenant["states"]["completion"]
    tenant["issue"].save(update_fields=["state", "updated_at"])

    with CaptureQueriesContext(connection) as queries:
        _configure_workflow(tenant)

    issue_locks = [
        index
        for index, query in enumerate(queries.captured_queries)
        if 'FROM "issues"' in query["sql"] and "FOR UPDATE" in query["sql"]
    ]
    policy_locks = [
        index
        for index, query in enumerate(queries.captured_queries)
        if 'FROM "issue_chat_workflow_policies"' in query["sql"] and "FOR UPDATE" in query["sql"]
    ]
    assert issue_locks
    assert policy_locks
    assert min(issue_locks) < min(policy_locks)


@pytest.mark.unit
@pytest.mark.django_db
@pytest.mark.parametrize(
    ("state_key", "reason"),
    (
        ("manual_hold", "manual_protected"),
        ("completed", "protected_state_group"),
        ("cancelled", "protected_state_group"),
        ("backlog", "protected_state_group"),
        ("unstarted", "protected_state_group"),
    ),
)
def test_manual_and_terminal_groups_are_protected(workflow_tenant, state_key, reason):
    tenant = workflow_tenant
    tenant["issue"].state = tenant["states"][state_key]
    tenant["issue"].save(update_fields=["state", "updated_at"])

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.NEEDS_ACTION,
        timezone.now(),
        f"turn-{state_key}",
    )

    assert tenant["issue"].state_id == tenant["states"][state_key].id
    assert (
        IssueChatWorkflowAudit.objects.filter(issue=tenant["issue"])
        .order_by("-created_at")
        .values_list("reason", flat=True)
        .first()
        == reason
    )


@pytest.mark.unit
@pytest.mark.django_db
@pytest.mark.parametrize("scope", ("archived", "draft", "triage"))
def test_out_of_scope_issue_is_a_noop_without_server_error(workflow_tenant, scope):
    tenant = workflow_tenant
    if scope == "archived":
        tenant["issue"].archived_at = timezone.localdate()
        tenant["issue"].save(update_fields=["archived_at", "updated_at"])
    elif scope == "draft":
        tenant["issue"].is_draft = True
        tenant["issue"].save(update_fields=["is_draft", "updated_at"])
    else:
        triage = _state(tenant["project"], "Triage", StateGroup.TRIAGE.value)
        tenant["issue"].state = triage
        tenant["issue"].save(update_fields=["state", "updated_at"])

    result = _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        timezone.now(),
        f"turn-{scope}",
    )

    assert result.applied is True
    tenant["issue"].refresh_from_db()
    assert (
        IssueChatWorkflowAudit.objects.filter(issue=tenant["issue"])
        .order_by("-created_at")
        .values_list("reason", flat=True)
        .first()
        == "issue_out_of_scope"
    )


@pytest.mark.unit
@pytest.mark.django_db
def test_duplicate_observation_is_idempotent(workflow_tenant):
    tenant = workflow_tenant
    observed_at = timezone.now()

    first = _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        observed_at,
        "turn-duplicate",
    )
    second = _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        observed_at,
        "turn-duplicate",
    )

    assert first.applied is True
    assert second.applied is False
    assert IssueChatWorkflowAudit.objects.filter(issue=tenant["issue"]).count() == 1
    assert tenant["issue"].state_id == tenant["states"]["attention"].id


@pytest.mark.unit
@pytest.mark.django_db
def test_active_late_sibling_after_unrelated_answer_still_moves_to_work(workflow_tenant):
    tenant = workflow_tenant
    answer_binding = _binding(tenant["issue"])
    running_binding = tenant["binding"]
    now = timezone.now()

    _observe(
        tenant,
        answer_binding,
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        now,
        "turn-answer",
    )
    _observe(
        tenant,
        running_binding,
        IssueChatBinding.ObservationState.RUNNING,
        now - timedelta(seconds=30),
        "turn-running",
    )

    assert tenant["issue"].state_id == tenant["states"]["running"].id


@pytest.mark.unit
@pytest.mark.django_db
def test_multiple_bindings_prefer_any_active_running_then_attention(workflow_tenant):
    tenant = workflow_tenant
    attention_binding = tenant["binding"]
    running_binding = _binding(tenant["issue"])
    now = timezone.now()

    _observe(
        tenant,
        attention_binding,
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        now - timedelta(seconds=20),
        "turn-attention",
    )
    _observe(
        tenant,
        running_binding,
        IssueChatBinding.ObservationState.RUNNING,
        now - timedelta(seconds=10),
        "turn-running",
    )
    _observe(
        tenant,
        attention_binding,
        IssueChatBinding.ObservationState.NEEDS_ACTION,
        now,
        "turn-attention",
    )

    assert tenant["issue"].state_id == tenant["states"]["running"].id
    assert (
        IssueChatWorkflowAudit.objects.filter(issue=tenant["issue"])
        .order_by("-created_at")
        .values_list("reason", flat=True)
        .first()
        == "state_already_running"
    )


@pytest.mark.unit
@pytest.mark.django_db
def test_answer_without_verified_pdf_goes_to_attention_only(workflow_tenant):
    tenant = workflow_tenant

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        timezone.now(),
        "turn-answer-only",
    )

    assert tenant["issue"].state_id == tenant["states"]["attention"].id
    assert tenant["issue"].state_id != tenant["states"]["completion"].id
    assert tenant["issue"].description_html == "<p>Manual description stays.</p>"


@pytest.mark.unit
@pytest.mark.django_db
def test_stale_running_observation_is_ignored_and_never_completes(workflow_tenant):
    tenant = workflow_tenant
    stale_binding = tenant["binding"]
    attention_binding = _binding(tenant["issue"])
    now = timezone.now()

    _observe(
        tenant,
        stale_binding,
        IssueChatBinding.ObservationState.RUNNING,
        now - timedelta(minutes=10),
        "turn-stale-running",
    )
    assert tenant["issue"].state_id == tenant["states"]["source"].id

    _observe(
        tenant,
        attention_binding,
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        now,
        "turn-fresh-attention",
    )

    assert tenant["issue"].state_id == tenant["states"]["attention"].id
    assert tenant["issue"].state_id != tenant["states"]["completion"].id


@pytest.mark.unit
@pytest.mark.django_db
def test_todo_fresh_running_observation_moves_to_work(workflow_tenant):
    tenant = workflow_tenant
    tenant["policy"].todo_state = tenant["states"]["unstarted"]
    tenant["policy"].save(update_fields=["todo_state", "updated_at"])
    tenant["issue"].state = tenant["states"]["unstarted"]
    tenant["issue"].save(update_fields=["state", "updated_at"])

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.RUNNING,
        timezone.now(),
        "turn-todo-running",
    )

    assert tenant["issue"].state_id == tenant["states"]["running"].id


@pytest.mark.unit
@pytest.mark.django_db
def test_todo_historical_answer_does_not_move(workflow_tenant):
    tenant = workflow_tenant
    tenant["policy"].todo_state = tenant["states"]["unstarted"]
    tenant["policy"].save(update_fields=["todo_state", "updated_at"])
    tenant["issue"].state = tenant["states"]["unstarted"]
    tenant["issue"].save(update_fields=["state", "updated_at"])

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        timezone.now(),
        "turn-todo-answer",
    )

    assert tenant["issue"].state_id == tenant["states"]["unstarted"].id
    assert (
        IssueChatWorkflowAudit.objects.filter(issue=tenant["issue"])
        .order_by("-created_at")
        .values_list("reason", flat=True)
        .first()
        == "todo_requires_running"
    )


@pytest.mark.unit
@pytest.mark.django_db
def test_needs_action_can_resume_running_in_same_turn(workflow_tenant):
    tenant = workflow_tenant
    now = timezone.now()

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.NEEDS_ACTION,
        now,
        "turn-permission",
    )
    assert tenant["issue"].state_id == tenant["states"]["attention"].id

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.RUNNING,
        now + timedelta(seconds=1),
        "turn-permission",
    )
    assert tenant["issue"].state_id == tenant["states"]["running"].id


@pytest.mark.unit
@pytest.mark.django_db
def test_verified_completion_preserves_old_answer_and_new_turn_can_reactivate(workflow_tenant):
    tenant = workflow_tenant
    same_turn_binding = tenant["binding"]
    new_turn_binding = _binding(tenant["issue"])
    now = timezone.now()

    _observe(
        tenant,
        same_turn_binding,
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        now,
        "turn-complete",
    )
    _observe(
        tenant,
        new_turn_binding,
        IssueChatBinding.ObservationState.RUNNING,
        now + timedelta(milliseconds=500),
        "turn-running-complete",
    )
    previous_state_id = tenant["issue"].state_id
    tenant["issue"].state = tenant["states"]["completion"]
    tenant["issue"].save(update_fields=["state", "updated_at"])
    verified_at = now + timedelta(seconds=1)
    cursor = record_verified_issue_chat_completion(
        issue=tenant["issue"],
        state=tenant["states"]["completion"],
        previous_state_id=previous_state_id,
        verified_at=verified_at,
    )

    assert cursor is not None
    assert cursor.completion_cutoff_at == verified_at
    assert cursor.completion_cutoff_source == "verified_report"
    assert len(cursor.completion_snapshots) == 1
    snapshot_records = cursor.completion_snapshots[0]["records"]
    assert {
        (record["binding_id"], record["turn_id"])
        for record in snapshot_records
    } == {
        (str(same_turn_binding.id), "turn-complete"),
        (str(new_turn_binding.id), "turn-running-complete"),
    }
    assert all(record["observed_at"] for record in snapshot_records)
    finalizer_audit = IssueChatWorkflowAudit.objects.get(
        issue=tenant["issue"],
        reason="verified_completion",
    )
    assert finalizer_audit.previous_state_id == previous_state_id
    assert finalizer_audit.target_state_id == tenant["states"]["completion"].id

    _observe(
        tenant,
        same_turn_binding,
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        now + timedelta(seconds=2),
        "turn-complete",
    )
    assert tenant["issue"].state_id == tenant["states"]["completion"].id
    cursor.refresh_from_db()
    assert cursor.expected_state_id == tenant["states"]["completion"].id

    _observe(
        tenant,
        new_turn_binding,
        IssueChatBinding.ObservationState.NEEDS_ACTION,
        now + timedelta(seconds=3),
        "turn-next-attention",
    )
    assert tenant["issue"].state_id == tenant["states"]["attention"].id
    cursor = IssueChatWorkflowCursor.objects.get(issue=tenant["issue"])
    assert len(cursor.completion_snapshots) == 1


@pytest.mark.unit
@pytest.mark.django_db
def test_same_turn_sibling_heartbeat_cannot_reopen_verified_completion(workflow_tenant):
    tenant = workflow_tenant
    same_turn_binding = tenant["binding"]
    now = timezone.now()
    _observe(
        tenant,
        same_turn_binding,
        IssueChatBinding.ObservationState.RUNNING,
        now,
        "turn-snapshot",
    )
    previous_state_id = tenant["issue"].state_id
    tenant["issue"].state = tenant["states"]["completion"]
    tenant["issue"].save(update_fields=["state", "updated_at"])
    record_verified_issue_chat_completion(
        issue=tenant["issue"],
        state=tenant["states"]["completion"],
        previous_state_id=previous_state_id,
        verified_at=now + timedelta(seconds=1),
    )

    _observe(
        tenant,
        same_turn_binding,
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        now + timedelta(seconds=2),
        "turn-snapshot",
    )
    _observe(
        tenant,
        same_turn_binding,
        IssueChatBinding.ObservationState.RUNNING,
        now + timedelta(seconds=3),
        "turn-snapshot",
        resume_after_stop=True,
    )

    assert tenant["issue"].state_id == tenant["states"]["completion"].id
    same_turn_binding.refresh_from_db()
    assert same_turn_binding.observation_turn_id == "turn-snapshot"


@pytest.mark.unit
@pytest.mark.django_db
def test_existing_completion_without_cursor_can_reactivate_on_new_running(workflow_tenant):
    tenant = workflow_tenant
    tenant["issue"].state = tenant["states"]["completion"]
    tenant["issue"].save(update_fields=["state", "updated_at"])
    assert not IssueChatWorkflowCursor.objects.filter(issue=tenant["issue"]).exists()

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.RUNNING,
        timezone.now(),
        "turn-new-after-activation",
    )

    assert tenant["issue"].state_id == tenant["states"]["running"].id
    assert tenant["issue"].state_id != tenant["states"]["completed"].id


@pytest.mark.unit
@pytest.mark.django_db
def test_unrelated_trigger_uses_selected_new_completion_turn_for_attention(workflow_tenant):
    tenant = workflow_tenant
    old_binding = tenant["binding"]
    new_binding = _binding(tenant["issue"])
    now = timezone.now()
    _observe(
        tenant,
        old_binding,
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        now,
        "turn-old",
    )
    previous_state_id = tenant["issue"].state_id
    tenant["issue"].state = tenant["states"]["completion"]
    tenant["issue"].save(update_fields=["state", "updated_at"])
    record_verified_issue_chat_completion(
        issue=tenant["issue"],
        state=tenant["states"]["completion"],
        previous_state_id=previous_state_id,
        verified_at=now + timedelta(seconds=1),
    )
    IssueChatBinding.objects.filter(pk=new_binding.id).update(
        observation_state=IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        observation_source=IssueChatBinding.ObservationSource.CODEX,
        observation_observed_at=now + timedelta(seconds=2),
        observation_turn_id="turn-new",
    )
    _observe(
        tenant,
        old_binding,
        IssueChatBinding.ObservationState.NEEDS_ACTION,
        now + timedelta(seconds=3),
        "turn-old",
    )

    assert tenant["issue"].state_id == tenant["states"]["attention"].id


@pytest.mark.unit
@pytest.mark.django_db
def test_manual_override_against_expected_state_is_preserved(workflow_tenant):
    tenant = workflow_tenant
    now = timezone.now()

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.RUNNING,
        now,
        "turn-running",
    )
    assert tenant["issue"].state_id == tenant["states"]["running"].id

    tenant["issue"].state = tenant["states"]["attention"]
    tenant["issue"].save(update_fields=["state", "updated_at"])
    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        now + timedelta(seconds=1),
        "turn-running",
    )

    assert tenant["issue"].state_id == tenant["states"]["attention"].id
    assert (
        IssueChatWorkflowAudit.objects.filter(issue=tenant["issue"])
        .order_by("-created_at")
        .values_list("reason", flat=True)
        .first()
        == "manual_override"
    )


@pytest.mark.unit
@pytest.mark.django_db
def test_return_to_source_initializes_cutoff_and_requires_next_fresh_event(workflow_tenant):
    tenant = workflow_tenant
    observed_at = timezone.now()

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.RUNNING,
        observed_at,
        "turn-first",
    )
    assert tenant["issue"].state_id == tenant["states"]["running"].id

    tenant["issue"].state = tenant["states"]["source"]
    tenant["issue"].save(update_fields=["state", "updated_at"])
    tenant["issue"].description_html = "<p>Unrelated later edit.</p>"
    tenant["issue"].save(update_fields=["description_html", "updated_at"])

    first = _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        timezone.now(),
        "turn-delayed",
    )
    assert first.applied is True
    assert tenant["issue"].state_id == tenant["states"]["source"].id
    cursor = IssueChatWorkflowCursor.objects.get(issue=tenant["issue"])
    assert cursor.manual_resume_cutoff_at is not None
    assert cursor.manual_resume_cutoff_source == "source_state"
    assert cursor.expected_state_id == tenant["states"]["source"].id
    assert (
        IssueChatWorkflowAudit.objects.filter(issue=tenant["issue"])
        .order_by("-created_at")
        .values_list("reason", flat=True)
        .first()
        == "manual_resume_cutoff_initialized"
    )

    old_sibling = _binding(tenant["issue"])
    _observe(
        tenant,
        old_sibling,
        IssueChatBinding.ObservationState.RUNNING,
        cursor.manual_resume_cutoff_at - timedelta(microseconds=1),
        "turn-old-sibling",
    )
    assert tenant["issue"].state_id == tenant["states"]["source"].id

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.RUNNING,
        cursor.manual_resume_cutoff_at + timedelta(seconds=1),
        "turn-resumed",
    )
    assert tenant["issue"].state_id == tenant["states"]["running"].id


@pytest.mark.unit
@pytest.mark.django_db
def test_return_to_todo_also_initializes_cutoff(workflow_tenant):
    tenant = workflow_tenant
    tenant["policy"].todo_state = tenant["states"]["unstarted"]
    tenant["policy"].save(update_fields=["todo_state", "updated_at"])
    now = timezone.now()
    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.RUNNING,
        now,
        "turn-work",
    )
    assert tenant["issue"].state_id == tenant["states"]["running"].id

    tenant["issue"].state = tenant["states"]["unstarted"]
    tenant["issue"].save(update_fields=["state", "updated_at"])
    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.RUNNING,
        now + timedelta(seconds=1),
        "turn-next",
    )
    assert tenant["issue"].state_id == tenant["states"]["unstarted"].id
    cursor = IssueChatWorkflowCursor.objects.get(issue=tenant["issue"])
    assert cursor.manual_resume_cutoff_source == "todo_state"

    _observe(
        tenant,
        tenant["binding"],
        IssueChatBinding.ObservationState.RUNNING,
        cursor.manual_resume_cutoff_at + timedelta(seconds=1),
        "turn-next",
    )
    assert tenant["issue"].state_id == tenant["states"]["running"].id
