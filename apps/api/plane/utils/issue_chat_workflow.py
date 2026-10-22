# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from django.db import transaction
from django.db.models import OuterRef, Subquery
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
    WorkspaceMember,
)
from plane.db.models.project import ROLE
from plane.utils.llm_complete import LLM_COMPLETE_STATE_NAME


ATTENTION_OBSERVATION_STATES = frozenset(
    {
        IssueChatBinding.ObservationState.ANSWER_AVAILABLE,
        IssueChatBinding.ObservationState.NEEDS_ACTION,
        IssueChatBinding.ObservationState.FAILED,
    }
)
PROTECTED_STATE_GROUPS = frozenset(
    {
        StateGroup.BACKLOG.value,
        StateGroup.UNSTARTED.value,
        StateGroup.COMPLETED.value,
        StateGroup.CANCELLED.value,
        StateGroup.TRIAGE.value,
    }
)
POLICY_STATE_FIELDS = (
    "source_state",
    "running_state",
    "attention_state",
    "manual_hold_state",
    "completion_state",
    "todo_state",
)
REQUIRED_POLICY_STATE_FIELDS = (
    "source_state",
    "running_state",
    "attention_state",
    "manual_hold_state",
    "completion_state",
)
MIN_STALE_RUNNING_TTL_SECONDS = 60
MAX_STALE_RUNNING_TTL_SECONDS = 7 * 24 * 60 * 60

REASON_APPLIED = "applied"
REASON_ANSWER_READY_MISSING_REPORT = "answer_ready_missing_report"
REASON_ATTENTION = "attention"
REASON_BEFORE_ACTIVATION = "before_activation"
REASON_COMPLETION_PRESERVED = "completion_preserved"
REASON_DISABLED = "disabled"
REASON_EXISTING_COMPLETION_ADOPTED = "existing_completion_adopted"
REASON_FAILED_OBSERVATION = "failed_observation"
REASON_INVALID_CONFIGURATION = "invalid_configuration"
REASON_ISSUE_OUT_OF_SCOPE = "issue_out_of_scope"
REASON_MANUAL_OVERRIDE = "manual_override"
REASON_MANUAL_PROTECTED = "manual_protected"
REASON_MANUAL_RESUME_CUTOFF_INITIALIZED = "manual_resume_cutoff_initialized"
REASON_MANUAL_RESUME_REQUIRES_NEWER_EVENT = "manual_resume_requires_newer_event"
REASON_NO_ACTIONABLE_OBSERVATION = "no_actionable_observation"
REASON_NOT_CONFIGURED = "not_configured"
REASON_NEEDS_ACTION = "needs_action"
REASON_PROTECTED_STATE_GROUP = "protected_state_group"
REASON_STALE_RUNNING_ONLY = "stale_running_only"
REASON_STATE_ALREADY_RUNNING = "state_already_running"
REASON_STATE_ALREADY_ATTENTION = "state_already_attention"
REASON_STATE_MISSING = "state_missing"
REASON_TODO_REQUIRES_RUNNING = "todo_requires_running"
REASON_UNSUPPORTED_SOURCE = "unsupported_source"
REASON_VERIFIED_COMPLETION = "verified_completion"


class IssueChatWorkflowError(Exception):
    pass


class IssueChatWorkflowConfigurationError(IssueChatWorkflowError):
    pass


class IssueChatWorkflowNotFound(IssueChatWorkflowError):
    pass


@dataclass(frozen=True)
class IssueChatWorkflowConfigurationResult:
    policy: IssueChatWorkflowPolicy
    created: bool
    adopted_completion_issues: int = 0


@dataclass(frozen=True)
class IssueChatWorkflowDecision:
    issue: Issue
    policy: IssueChatWorkflowPolicy | None
    cursor: IssueChatWorkflowCursor | None
    audit: IssueChatWorkflowAudit | None
    applied: bool
    reason: str
    target_state: State | None = None


@dataclass(frozen=True)
class _BindingSignals:
    active_running: list[IssueChatBinding]
    attention: list[IssueChatBinding]
    stale_running: list[IssueChatBinding]
    pre_activation_running: list[IssueChatBinding]
    manual_cutoff_excluded: list[IssueChatBinding]
    completion_cutoff_excluded: list[IssueChatBinding]


def _project(workspace_slug: str, project_id: UUID, *, for_update: bool = False) -> Project:
    queryset = Project.objects.filter(
        pk=project_id,
        workspace__slug=workspace_slug,
        deleted_at__isnull=True,
        archived_at__isnull=True,
    ).select_related("workspace")
    if for_update:
        queryset = queryset.select_for_update(of=("self",))
    project = queryset.first()
    if project is None:
        raise IssueChatWorkflowNotFound("The project was not found in the requested workspace.")
    return project


def _configuration_state_errors(
    *,
    project: Project,
    source_state: State,
    running_state: State,
    attention_state: State,
    manual_hold_state: State,
    completion_state: State,
    todo_state: State | None,
    stale_running_ttl_seconds: int,
) -> list[str]:
    states = {
        "source_state": source_state,
        "running_state": running_state,
        "attention_state": attention_state,
        "manual_hold_state": manual_hold_state,
        "completion_state": completion_state,
    }
    errors = []
    seen_ids = set()
    for field, state in states.items():
        if state.project_id != project.id or state.deleted_at is not None:
            errors.append(f"{field} must be an active state in the exact configured project")
        if state.id in seen_ids:
            errors.append(f"{field} must use a distinct state")
        seen_ids.add(state.id)
    for field in ("source_state", "running_state", "attention_state", "completion_state"):
        if states[field].group != StateGroup.STARTED.value:
            errors.append(f"{field} must use the started group")
    if manual_hold_state.group != StateGroup.UNSTARTED.value:
        errors.append("manual_hold_state must use the unstarted group")
    if todo_state is not None:
        if todo_state.project_id != project.id or todo_state.deleted_at is not None:
            errors.append("todo_state must be an active state in the exact configured project")
        if todo_state.group != StateGroup.UNSTARTED.value:
            errors.append("todo_state must use the unstarted group")
        if todo_state.id in seen_ids:
            errors.append("todo_state must use a distinct state")
    if completion_state.name.casefold() != LLM_COMPLETE_STATE_NAME.casefold():
        errors.append(f"completion_state must be the project's {LLM_COMPLETE_STATE_NAME!r} state")
    if not MIN_STALE_RUNNING_TTL_SECONDS <= stale_running_ttl_seconds <= MAX_STALE_RUNNING_TTL_SECONDS:
        errors.append(
            f"stale_running_ttl_seconds must be between {MIN_STALE_RUNNING_TTL_SECONDS} "
            f"and {MAX_STALE_RUNNING_TTL_SECONDS}"
        )
    return errors


def _load_configuration_states(project: Project, state_ids: dict[str, UUID | None]) -> dict[str, State | None]:
    missing_required = [
        field for field in REQUIRED_POLICY_STATE_FIELDS if state_ids.get(field) is None
    ]
    if missing_required:
        missing = ", ".join(missing_required)
        raise IssueChatWorkflowConfigurationError(f"Explicit state IDs are required: {missing}.")

    requested_ids = {state_id for state_id in state_ids.values() if state_id is not None}
    if len(requested_ids) != sum(state_id is not None for state_id in state_ids.values()):
        raise IssueChatWorkflowConfigurationError("All policy mappings must use distinct state IDs.")

    states = {
        state.id: state
        for state in State.objects.filter(
            project=project,
            pk__in=requested_ids,
            deleted_at__isnull=True,
        ).select_related("project")
    }
    missing_ids = requested_ids - states.keys()
    if missing_ids:
        raise IssueChatWorkflowConfigurationError(
            "Every configured state must be an active state in the exact target project."
        )

    loaded = {
        field: states[state_id] if state_id is not None else None
        for field, state_id in state_ids.items()
    }
    errors = _configuration_state_errors(
        project=project,
        stale_running_ttl_seconds=MIN_STALE_RUNNING_TTL_SECONDS,
        **loaded,
    )
    if errors:
        raise IssueChatWorkflowConfigurationError("; ".join(errors))
    return loaded


def _has_configuration_permission(workspace_member, project_member) -> bool:
    return bool(
        workspace_member
        and workspace_member.is_active
        and workspace_member.role in {ROLE.ADMIN.value, ROLE.MEMBER.value}
        and project_member
        and project_member.is_active
        and project_member.role in {ROLE.ADMIN.value, ROLE.MEMBER.value}
    )


def _completion_issue_queryset(project: Project, completion_state: State):
    return Issue.objects.filter(
        workspace_id=project.workspace_id,
        project_id=project.id,
        state_id=completion_state.id,
        archived_at__isnull=True,
        is_draft=False,
    )


def _lock_completion_issues(project: Project, completion_state: State) -> list[Issue]:
    queryset = _completion_issue_queryset(project, completion_state)
    candidate_ids = list(queryset.order_by("id").values_list("id", flat=True))
    if not candidate_ids:
        return []

    issues = list(
        Issue.objects.select_for_update(of=("self",))
        .filter(pk__in=candidate_ids)
        .select_related("state", "project", "workspace")
        .order_by("id")
    )
    locked_ids = {issue.id for issue in issues}
    if set(candidate_ids) != locked_ids or set(queryset.values_list("id", flat=True)) != locked_ids:
        raise IssueChatWorkflowConfigurationError(
            "The completion-state issue set changed during configuration; retry without applying changes."
        )
    return issues


def configure_issue_chat_workflow(
    *,
    workspace_slug: str,
    project_id: UUID,
    operator_id: UUID,
    enabled: bool,
    source_state_id: UUID | None = None,
    running_state_id: UUID | None = None,
    attention_state_id: UUID | None = None,
    manual_hold_state_id: UUID | None = None,
    completion_state_id: UUID | None = None,
    todo_state_id: UUID | None = None,
    stale_running_ttl_seconds: int = 3600,
) -> IssueChatWorkflowConfigurationResult:
    """Explicitly configure one project without mutating any issue history."""

    with transaction.atomic():
        project = _project(workspace_slug, project_id, for_update=True)
        operator = User.objects.filter(pk=operator_id, is_active=True, is_bot=False).first()
        if operator is None:
            raise IssueChatWorkflowConfigurationError("The operator must be an active non-bot user.")
        workspace_member = WorkspaceMember.objects.filter(
            workspace_id=project.workspace_id,
            member_id=operator.id,
            is_active=True,
        ).first()
        project_member = ProjectMember.objects.filter(
            workspace_id=project.workspace_id,
            project_id=project.id,
            member_id=operator.id,
            is_active=True,
        ).first()
        if not _has_configuration_permission(workspace_member, project_member):
            raise IssueChatWorkflowConfigurationError(
                "The operator must be an active project admin or member."
            )

        state_ids = {
            "source_state": source_state_id,
            "running_state": running_state_id,
            "attention_state": attention_state_id,
            "manual_hold_state": manual_hold_state_id,
            "completion_state": completion_state_id,
            "todo_state": todo_state_id,
        }
        loaded_states = _load_configuration_states(project, state_ids) if enabled else None
        if loaded_states is not None:
            errors = _configuration_state_errors(
                project=project,
                stale_running_ttl_seconds=stale_running_ttl_seconds,
                **loaded_states,
            )
            if errors:
                raise IssueChatWorkflowConfigurationError("; ".join(errors))

        if enabled:
            completion_issues = _lock_completion_issues(
                project,
                loaded_states["completion_state"],
            )
        else:
            completion_issues = []

        policy = (
            IssueChatWorkflowPolicy.objects.select_for_update(of=("self",))
            .filter(project=project, deleted_at__isnull=True)
            .select_related(*POLICY_STATE_FIELDS)
            .first()
        )
        created = policy is None
        if created and not enabled:
            raise IssueChatWorkflowConfigurationError(
                "No policy exists for this project; use --enable with explicit state IDs."
            )

        now = timezone.now()
        if created:
            policy = IssueChatWorkflowPolicy(
                project=project,
                workspace=project.workspace,
                enabled=False,
                version=0,
                created_by_id=operator.id,
            )
        if enabled:
            for field, state in loaded_states.items():
                setattr(policy, field, state)
            policy.enabled = True
            policy.stale_running_ttl_seconds = stale_running_ttl_seconds
        else:
            policy.enabled = False
        policy.version = (policy.version or 0) + 1
        policy.effective_at = now
        policy.updated_by_id = operator.id
        policy.save(disable_auto_set_user=True)

        adopted_completion_issues = 0
        if enabled and completion_issues:
            issue_ids = [issue.id for issue in completion_issues]
            cursors = {
                cursor.issue_id: cursor
                for cursor in (
                    IssueChatWorkflowCursor.objects.select_for_update(of=("self",))
                    .filter(issue_id__in=issue_ids, deleted_at__isnull=True)
                    .order_by("issue_id")
                )
            }
            bindings_by_issue = {}
            for binding in (
                IssueChatBinding.objects.select_for_update(of=("self",))
                .filter(issue_id__in=issue_ids, deleted_at__isnull=True)
                .order_by("issue_id", "id")
            ):
                bindings_by_issue.setdefault(binding.issue_id, []).append(binding)
            for issue in completion_issues:
                cursor = cursors.get(issue.id)
                if cursor is None:
                    cursor = _new_cursor(issue=issue, policy=policy)
                elif cursor.policy_version != policy.version:
                    cursor.expected_state = issue.state
                cursor.policy = policy
                cursor.policy_version = policy.version
                cursor.expected_state = issue.state
                _append_completion_snapshot(
                    cursor,
                    source=IssueChatWorkflowCursor.CompletionCutoffSource.EXISTING_STATE,
                    cutoff_at=now,
                    policy_version=policy.version,
                    bindings=bindings_by_issue.get(issue.id, []),
                )
                cursor.last_reason = REASON_EXISTING_COMPLETION_ADOPTED
                cursor.save()
                adopted_completion_issues += 1

    return IssueChatWorkflowConfigurationResult(
        policy=policy,
        created=created,
        adopted_completion_issues=adopted_completion_issues,
    )


def _policy_errors(policy: IssueChatWorkflowPolicy) -> list[str]:
    if policy.effective_at is None:
        return ["effective_at is missing"]
    states = {field: getattr(policy, field) for field in POLICY_STATE_FIELDS}
    return _configuration_state_errors(
        project=policy.project,
        stale_running_ttl_seconds=policy.stale_running_ttl_seconds,
        **states,
    )


def _is_completion_state(
    state: State | None,
    *,
    policy: IssueChatWorkflowPolicy,
) -> bool:
    return bool(
        state
        and state.id == policy.completion_state_id
        and state.name.casefold() == LLM_COMPLETE_STATE_NAME.casefold()
    )


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _completion_snapshot_records(bindings: list[IssueChatBinding]) -> list[dict]:
    return [
        {
            "binding_id": str(binding.id),
            "turn_id": binding.observation_turn_id,
            "observation_state": binding.observation_state,
            "observed_at": (
                _isoformat(binding.observation_observed_at)
                if binding.observation_observed_at is not None
                else None
            ),
        }
        for binding in bindings
    ]


def _append_completion_snapshot(
    cursor: IssueChatWorkflowCursor,
    *,
    source: str,
    cutoff_at: datetime,
    policy_version: int,
    bindings: list[IssueChatBinding],
) -> dict:
    snapshot = {
        "schema_version": 1,
        "source": str(source),
        "cutoff_at": _isoformat(cutoff_at),
        "policy_version": policy_version,
        "records": _completion_snapshot_records(bindings),
    }
    history = list(cursor.completion_snapshots or [])
    history.append(snapshot)
    cursor.completion_snapshots = history
    cursor.completion_cutoff_at = cutoff_at
    cursor.completion_cutoff_source = str(source)
    return snapshot


def _idempotency_key(
    *,
    policy: IssueChatWorkflowPolicy,
    binding: IssueChatBinding,
    observation_state: str,
    observed_at: datetime,
    turn_id: str,
) -> str:
    payload = {
        "policy_id": str(policy.id),
        "policy_version": policy.version,
        "binding_id": str(binding.id),
        "observation_state": observation_state,
        "observed_at": _isoformat(observed_at),
        "turn_id": turn_id,
    }
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _binding_is_workflow_source(binding: IssueChatBinding) -> bool:
    return (
        binding.provider == IssueChatBinding.Provider.CODEX
        and binding.observation_source == IssueChatBinding.ObservationSource.CODEX
    )


def _completion_snapshot_pairs(completion_snapshots: list[dict]) -> set[tuple[str, str]]:
    return {
        (str(record.get("binding_id")), record.get("turn_id"))
        for snapshot in completion_snapshots
        if isinstance(snapshot, dict)
        for record in (snapshot.get("records") or [])
        if isinstance(record, dict) and record.get("turn_id")
    }


def _binding_matches_completion_snapshot(
    binding: IssueChatBinding,
    completion_snapshots: list[dict],
) -> bool:
    return (
        str(binding.id),
        binding.observation_turn_id,
    ) in _completion_snapshot_pairs(completion_snapshots)


def _classify_bindings(
    bindings: list[IssueChatBinding],
    *,
    policy: IssueChatWorkflowPolicy,
    now: datetime,
    manual_resume_cutoff_at: datetime | None,
    completion_cutoff_at: datetime | None,
    completion_snapshots: list[dict],
) -> _BindingSignals:
    cutoff = now - timedelta(seconds=policy.stale_running_ttl_seconds)
    active_running = []
    attention = []
    stale_running = []
    pre_activation_running = []
    manual_cutoff_excluded = []
    completion_cutoff_excluded = []
    completion_records = _completion_snapshot_pairs(completion_snapshots)
    for binding in bindings:
        if not _binding_is_workflow_source(binding) or binding.observation_observed_at is None:
            continue
        if binding.observation_observed_at < policy.effective_at:
            if binding.observation_state == IssueChatBinding.ObservationState.RUNNING:
                pre_activation_running.append(binding)
            continue
        if (
            manual_resume_cutoff_at is not None
            and binding.observation_observed_at <= manual_resume_cutoff_at
        ):
            manual_cutoff_excluded.append(binding)
            continue
        if (
            completion_cutoff_at is not None
            and binding.observation_observed_at <= completion_cutoff_at
        ) or (str(binding.id), binding.observation_turn_id) in completion_records:
            completion_cutoff_excluded.append(binding)
            continue
        if binding.observation_state == IssueChatBinding.ObservationState.RUNNING:
            if binding.observation_observed_at >= cutoff:
                active_running.append(binding)
            else:
                stale_running.append(binding)
        elif binding.observation_state in ATTENTION_OBSERVATION_STATES:
            attention.append(binding)

    def order_key(binding):
        return (
            binding.observation_observed_at,
            binding.observation_turn_id,
            str(binding.id),
        )

    return _BindingSignals(
        active_running=sorted(active_running, key=order_key),
        attention=sorted(attention, key=order_key),
        stale_running=sorted(stale_running, key=order_key),
        pre_activation_running=sorted(pre_activation_running, key=order_key),
        manual_cutoff_excluded=sorted(manual_cutoff_excluded, key=order_key),
        completion_cutoff_excluded=sorted(completion_cutoff_excluded, key=order_key),
    )


def _binding_ids(bindings: list[IssueChatBinding]) -> list[str]:
    return [str(binding.id) for binding in bindings]


def _details_for_bindings(
    *,
    trigger: IssueChatBinding,
    signals: _BindingSignals,
    selected_binding: IssueChatBinding | None = None,
) -> dict:
    return {
        "trigger_binding_id": str(trigger.id),
        "trigger_observed_at": _isoformat(trigger.observation_observed_at),
        "trigger_observation_state": trigger.observation_state,
        "trigger_turn_id": trigger.observation_turn_id,
        "selected_binding_id": str(selected_binding.id) if selected_binding is not None else None,
        "selected_turn_id": selected_binding.observation_turn_id if selected_binding is not None else None,
        "selected_observed_at": (
            _isoformat(selected_binding.observation_observed_at)
            if selected_binding is not None and selected_binding.observation_observed_at is not None
            else None
        ),
        "active_running_binding_ids": _binding_ids(signals.active_running),
        "attention_binding_ids": _binding_ids(signals.attention),
        "stale_running_binding_ids": _binding_ids(signals.stale_running),
        "pre_activation_running_binding_ids": _binding_ids(signals.pre_activation_running),
        "manual_cutoff_excluded_binding_ids": _binding_ids(signals.manual_cutoff_excluded),
        "completion_cutoff_excluded_binding_ids": _binding_ids(signals.completion_cutoff_excluded),
    }


def _current_state_guard(
    issue: Issue,
    *,
    policy: IssueChatWorkflowPolicy,
    cursor: IssueChatWorkflowCursor,
    target_state: State | None,
    observation_state: str,
) -> str | None:
    state = issue.state
    if state is None:
        return REASON_STATE_MISSING
    if state.id == policy.manual_hold_state_id:
        return REASON_MANUAL_PROTECTED
    if policy.todo_state_id is not None and state.id == policy.todo_state_id:
        if (
            target_state is not None
            and target_state.id == policy.running_state_id
            and observation_state == IssueChatBinding.ObservationState.RUNNING
        ):
            return None
        return REASON_TODO_REQUIRES_RUNNING
    if state.group in PROTECTED_STATE_GROUPS:
        return REASON_PROTECTED_STATE_GROUP
    if state.id == policy.source_state_id:
        return None
    if state.id not in {policy.running_state_id, policy.attention_state_id}:
        return REASON_UNSUPPORTED_SOURCE
    if (
        cursor.policy_version == policy.version
        and cursor.expected_state_id is not None
        and cursor.expected_state_id != state.id
    ):
        return REASON_MANUAL_OVERRIDE
    return None


def _new_cursor(
    *,
    issue: Issue,
    policy: IssueChatWorkflowPolicy,
) -> IssueChatWorkflowCursor:
    return IssueChatWorkflowCursor(
        project=issue.project,
        workspace=issue.workspace,
        issue=issue,
        policy=policy,
        policy_version=policy.version,
        expected_state=issue.state,
    )


def _save_cursor(
    cursor: IssueChatWorkflowCursor,
    *,
    issue: Issue,
    policy: IssueChatWorkflowPolicy,
    trigger: IssueChatBinding,
    reason: str,
    observed_at: datetime,
    applied: bool,
    target_state: State | None,
) -> None:
    cursor.policy = policy
    cursor.policy_version = policy.version
    cursor.last_binding = trigger
    cursor.last_turn_id = trigger.observation_turn_id
    cursor.last_observation_state = trigger.observation_state
    cursor.last_reason = reason
    if cursor.baseline_event_at is None or observed_at > cursor.baseline_event_at:
        cursor.baseline_event_at = observed_at
    if applied and target_state is not None:
        cursor.expected_state = target_state
        cursor.last_applied_at = timezone.now()
    elif reason in {
        REASON_COMPLETION_PRESERVED,
        REASON_STATE_ALREADY_RUNNING,
        REASON_STATE_ALREADY_ATTENTION,
    }:
        cursor.expected_state = issue.state
    cursor.save()


def _attention_reason(observation_state: str) -> str:
    if observation_state == IssueChatBinding.ObservationState.ANSWER_AVAILABLE:
        return REASON_ANSWER_READY_MISSING_REPORT
    if observation_state == IssueChatBinding.ObservationState.NEEDS_ACTION:
        return REASON_NEEDS_ACTION
    if observation_state == IssueChatBinding.ObservationState.FAILED:
        return REASON_FAILED_OBSERVATION
    return REASON_ATTENTION


def _create_audit(
    *,
    policy: IssueChatWorkflowPolicy,
    issue: Issue,
    binding: IssueChatBinding,
    idempotency_key: str,
    observed_at: datetime,
    applied: bool,
    reason: str,
    previous_state: State | None,
    target_state: State | None,
    details: dict,
) -> IssueChatWorkflowAudit:
    return IssueChatWorkflowAudit.objects.create(
        project=issue.project,
        workspace=issue.workspace,
        policy=policy,
        issue=issue,
        binding=binding,
        previous_state=previous_state,
        target_state=target_state,
        idempotency_key=idempotency_key,
        policy_version=policy.version,
        observation_state=binding.observation_state,
        observed_at=observed_at,
        turn_id=binding.observation_turn_id,
        applied=applied,
        reason=reason,
        details=details,
    )


def _skip_without_policy(*, issue: Issue, reason: str) -> IssueChatWorkflowDecision:
    return IssueChatWorkflowDecision(
        issue=issue,
        policy=None,
        cursor=None,
        audit=None,
        applied=False,
        reason=reason,
    )


def evaluate_issue_chat_workflow(
    *,
    workspace_slug: str,
    project_id: UUID,
    issue_id: UUID,
    binding_id: UUID,
    observation_state: str,
    observed_at: datetime,
    turn_id: str,
) -> IssueChatWorkflowDecision:
    """Apply one typed observation without inventing task completion."""

    with transaction.atomic():
        issue = (
            Issue.objects.select_for_update(of=("self",))
            .filter(
                pk=issue_id,
                workspace__slug=workspace_slug,
                project_id=project_id,
                project__archived_at__isnull=True,
            )
            .select_related("workspace", "project", "state")
            .first()
        )
        if issue is None:
            raise IssueChatWorkflowNotFound("The work item was not found in the requested project.")

        policy = (
            IssueChatWorkflowPolicy.objects.select_for_update(of=("self",))
            .filter(project_id=project_id, deleted_at__isnull=True)
            .select_related("project", "workspace", *POLICY_STATE_FIELDS)
            .first()
        )
        if policy is None:
            return _skip_without_policy(issue=issue, reason=REASON_NOT_CONFIGURED)
        if not policy.enabled:
            return IssueChatWorkflowDecision(
                issue=issue,
                policy=policy,
                cursor=None,
                audit=None,
                applied=False,
                reason=REASON_DISABLED,
            )

        bindings = list(
            IssueChatBinding.objects.select_for_update(of=("self",))
            .filter(
                workspace_id=issue.workspace_id,
                project_id=issue.project_id,
                issue_id=issue.id,
                provider=IssueChatBinding.Provider.CODEX,
                deleted_at__isnull=True,
            )
            .order_by("id")
        )
        trigger = next((binding for binding in bindings if binding.id == binding_id), None)
        if trigger is None:
            raise IssueChatWorkflowNotFound("The chat binding was not found for this work item.")

        normalized_turn_id = (turn_id or "").strip()
        idempotency_key = _idempotency_key(
            policy=policy,
            binding=trigger,
            observation_state=observation_state,
            observed_at=observed_at,
            turn_id=normalized_turn_id,
        )
        existing_audit = (
            IssueChatWorkflowAudit.objects.filter(idempotency_key=idempotency_key)
            .select_related("target_state")
            .first()
        )
        if existing_audit is not None:
            return IssueChatWorkflowDecision(
                issue=issue,
                policy=policy,
                cursor=IssueChatWorkflowCursor.objects.filter(
                    issue=issue,
                    deleted_at__isnull=True,
                ).first(),
                audit=existing_audit,
                applied=existing_audit.applied,
                reason=existing_audit.reason,
                target_state=existing_audit.target_state,
            )

        configuration_errors = _policy_errors(policy)
        details = {
            "trigger_binding_id": str(trigger.id),
            "trigger_turn_id": normalized_turn_id,
        }
        if configuration_errors:
            details["configuration_errors"] = configuration_errors
            audit = _create_audit(
                policy=policy,
                issue=issue,
                binding=trigger,
                idempotency_key=idempotency_key,
                observed_at=observed_at,
                applied=False,
                reason=REASON_INVALID_CONFIGURATION,
                previous_state=issue.state,
                target_state=None,
                details=details,
            )
            return IssueChatWorkflowDecision(
                issue=issue,
                policy=policy,
                cursor=None,
                audit=audit,
                applied=False,
                reason=REASON_INVALID_CONFIGURATION,
            )

        if observed_at < policy.effective_at:
            audit = _create_audit(
                policy=policy,
                issue=issue,
                binding=trigger,
                idempotency_key=idempotency_key,
                observed_at=observed_at,
                applied=False,
                reason=REASON_BEFORE_ACTIVATION,
                previous_state=issue.state,
                target_state=None,
                details=details,
            )
            return IssueChatWorkflowDecision(
                issue=issue,
                policy=policy,
                cursor=None,
                audit=audit,
                applied=False,
                reason=REASON_BEFORE_ACTIVATION,
            )

        if issue.archived_at is not None or issue.is_draft or (
            issue.state is not None and issue.state.group == StateGroup.TRIAGE.value
        ):
            details["scope"] = {
                "archived": issue.archived_at is not None,
                "draft": issue.is_draft,
                "triage": bool(
                    issue.state is not None and issue.state.group == StateGroup.TRIAGE.value
                ),
            }
            audit = _create_audit(
                policy=policy,
                issue=issue,
                binding=trigger,
                idempotency_key=idempotency_key,
                observed_at=observed_at,
                applied=False,
                reason=REASON_ISSUE_OUT_OF_SCOPE,
                previous_state=issue.state,
                target_state=None,
                details=details,
            )
            return IssueChatWorkflowDecision(
                issue=issue,
                policy=policy,
                cursor=None,
                audit=audit,
                applied=False,
                reason=REASON_ISSUE_OUT_OF_SCOPE,
            )

        cursor = (
            IssueChatWorkflowCursor.objects.select_for_update(of=("self",))
            .filter(issue=issue, deleted_at__isnull=True)
            .select_related("expected_state")
            .first()
        )
        if cursor is None:
            cursor = _new_cursor(issue=issue, policy=policy)
        elif cursor.policy_version != policy.version:
            cursor.policy = policy
            cursor.policy_version = policy.version
            cursor.expected_state = issue.state

        now = timezone.now()
        current_state = issue.state
        if (
            current_state is not None
            and current_state.id in {policy.source_state_id, policy.todo_state_id}
            and cursor.expected_state_id not in {None, current_state.id}
        ):
            cursor.manual_resume_cutoff_at = now
            cursor.manual_resume_cutoff_source = (
                IssueChatWorkflowCursor.ManualResumeSource.TODO_STATE
                if current_state.id == policy.todo_state_id
                else IssueChatWorkflowCursor.ManualResumeSource.SOURCE_STATE
            )
            cursor.expected_state = current_state
            cursor.last_binding = trigger
            cursor.last_turn_id = normalized_turn_id
            cursor.last_observation_state = trigger.observation_state
            cursor.last_reason = REASON_MANUAL_RESUME_CUTOFF_INITIALIZED
            cursor.save()
            details["manual_resume_cutoff_at"] = _isoformat(now)
            details["manual_resume_cutoff_source"] = cursor.manual_resume_cutoff_source
            audit = _create_audit(
                policy=policy,
                issue=issue,
                binding=trigger,
                idempotency_key=idempotency_key,
                observed_at=observed_at,
                applied=False,
                reason=REASON_MANUAL_RESUME_CUTOFF_INITIALIZED,
                previous_state=current_state,
                target_state=None,
                details=details,
            )
            return IssueChatWorkflowDecision(
                issue=issue,
                policy=policy,
                cursor=cursor,
                audit=audit,
                applied=False,
                reason=REASON_MANUAL_RESUME_CUTOFF_INITIALIZED,
            )

        completion_snapshots = list(cursor.completion_snapshots or [])
        signals = _classify_bindings(
            bindings,
            policy=policy,
            now=now,
            manual_resume_cutoff_at=cursor.manual_resume_cutoff_at,
            completion_cutoff_at=cursor.completion_cutoff_at,
            completion_snapshots=completion_snapshots,
        )
        selected_binding = (
            signals.active_running[-1]
            if signals.active_running
            else signals.attention[-1]
            if signals.attention
            else None
        )
        details = _details_for_bindings(
            trigger=trigger,
            signals=signals,
            selected_binding=selected_binding,
        )
        target_state = (
            policy.running_state
            if signals.active_running
            else policy.attention_state
            if signals.attention
            else None
        )

        previous_state = issue.state
        reason = REASON_NO_ACTIONABLE_OBSERVATION
        applied = False
        is_completion = _is_completion_state(previous_state, policy=policy)
        guard_reason = None if is_completion else _current_state_guard(
            issue,
            policy=policy,
            cursor=cursor,
            target_state=target_state,
            observation_state=observation_state,
        )

        if is_completion:
            if target_state is None:
                reason = REASON_COMPLETION_PRESERVED
            elif target_state.id == policy.running_state_id:
                reason = REASON_APPLIED
                applied = True
            else:
                reason = (
                    _attention_reason(selected_binding.observation_state)
                    if selected_binding is not None
                    else REASON_ATTENTION
                )
                applied = True
        elif guard_reason is not None:
            reason = guard_reason
            target_state = None
        elif target_state is None:
            if signals.manual_cutoff_excluded:
                reason = REASON_MANUAL_RESUME_REQUIRES_NEWER_EVENT
            elif signals.stale_running or signals.pre_activation_running:
                reason = REASON_STALE_RUNNING_ONLY
            else:
                reason = REASON_NO_ACTIONABLE_OBSERVATION
        elif target_state.id == previous_state.id:
            reason = (
                REASON_STATE_ALREADY_RUNNING
                if target_state.id == policy.running_state_id
                else REASON_STATE_ALREADY_ATTENTION
            )
        else:
            reason = (
                _attention_reason(observation_state)
                if target_state.id == policy.attention_state_id
                else REASON_APPLIED
            )
            applied = True

        idempotency_payload = {
            "policy_id": str(policy.id),
            "policy_version": policy.version,
            "binding_id": str(trigger.id),
            "observation_state": observation_state,
            "observed_at": _isoformat(observed_at),
            "turn_id": normalized_turn_id,
        }
        details["idempotency_payload"] = idempotency_payload

        if applied and target_state is not None:
            previous_description_html = issue.description_html
            previous_description_json = issue.description_json
            issue.state = target_state
            issue.completed_at = None
            issue.updated_at = now
            issue.save(
                update_fields=["state", "completed_at", "updated_at"],
                disable_auto_set_user=True,
            )
            issue.refresh_from_db(
                fields=["state_id", "completed_at", "description_html", "description_json"],
            )
            if (
                issue.state_id != target_state.id
                or issue.completed_at is not None
                or issue.description_html != previous_description_html
                or issue.description_json != previous_description_json
            ):
                raise IssueChatWorkflowError(
                    "The workflow state update did not preserve the expected state or description."
                )

        _save_cursor(
            cursor,
            issue=issue,
            policy=policy,
            trigger=trigger,
            reason=reason,
            observed_at=observed_at,
            applied=applied,
            target_state=target_state,
        )
        audit = _create_audit(
            policy=policy,
            issue=issue,
            binding=trigger,
            idempotency_key=idempotency_key,
            observed_at=observed_at,
            applied=applied,
            reason=reason,
            previous_state=previous_state,
            target_state=target_state if applied or reason in {
                REASON_COMPLETION_PRESERVED,
                REASON_STATE_ALREADY_RUNNING,
                REASON_STATE_ALREADY_ATTENTION,
            } else None,
            details=details,
        )

    return IssueChatWorkflowDecision(
        issue=issue,
        policy=policy,
        cursor=cursor,
        audit=audit,
        applied=applied,
        reason=reason,
        target_state=target_state,
    )


def record_verified_issue_chat_completion(
    *,
    issue: Issue,
    state: State,
    previous_state_id: UUID | None,
    verified_at: datetime,
) -> IssueChatWorkflowCursor | None:
    """Record verified finalization without claiming a single verified turn."""

    with transaction.atomic():
        locked_issue = (
            Issue.objects.select_for_update(of=("self",))
            .filter(
                pk=issue.id,
                workspace_id=issue.workspace_id,
                project_id=issue.project_id,
            )
            .select_related("state", "project", "workspace")
            .first()
        )
        if locked_issue is None or locked_issue.state_id != state.id:
            return None
        policy = (
            IssueChatWorkflowPolicy.objects.select_for_update(of=("self",))
            .filter(project_id=locked_issue.project_id, enabled=True, deleted_at__isnull=True)
            .first()
        )
        if policy is None:
            return None
        if policy.completion_state_id != state.id:
            return None
        cursor = (
            IssueChatWorkflowCursor.objects.select_for_update(of=("self",))
            .filter(issue=locked_issue, deleted_at__isnull=True)
            .first()
        )
        if cursor is None:
            cursor = _new_cursor(issue=locked_issue, policy=policy)
        bindings = list(
            IssueChatBinding.objects.select_for_update(of=("self",))
            .filter(
                workspace_id=locked_issue.workspace_id,
                project_id=locked_issue.project_id,
                issue_id=locked_issue.id,
                deleted_at__isnull=True,
            )
            .order_by("id")
        )
        previous_state = (
            State.all_objects.filter(
                pk=previous_state_id,
                project_id=locked_issue.project_id,
            ).first()
            if previous_state_id is not None
            else None
        )
        cursor.policy = policy
        cursor.policy_version = policy.version
        cursor.expected_state = state
        cursor.baseline_event_at = verified_at
        snapshot = _append_completion_snapshot(
            cursor,
            source=IssueChatWorkflowCursor.CompletionCutoffSource.VERIFIED_REPORT,
            cutoff_at=verified_at,
            policy_version=policy.version,
            bindings=bindings,
        )
        cursor.last_reason = REASON_VERIFIED_COMPLETION
        cursor.last_applied_at = verified_at
        cursor.save()

        payload = {
            "policy_id": str(policy.id),
            "policy_version": policy.version,
            "issue_id": str(locked_issue.id),
            "verified_at": _isoformat(verified_at),
            "observed_state": "verified_completion",
            "previous_state_id": str(previous_state_id) if previous_state_id else None,
        }
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        idempotency_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        IssueChatWorkflowAudit.objects.create(
            project=locked_issue.project,
            workspace=locked_issue.workspace,
            policy=policy,
            issue=locked_issue,
            binding=cursor.last_binding,
            previous_state=previous_state,
            target_state=state,
            idempotency_key=idempotency_key,
            policy_version=policy.version,
            observation_state=REASON_VERIFIED_COMPLETION,
            observed_at=verified_at,
            turn_id="",
            applied=True,
            reason=REASON_VERIFIED_COMPLETION,
            details={
                "completion_cutoff_at": _isoformat(verified_at),
                "completion_cutoff_source": (
                    IssueChatWorkflowCursor.CompletionCutoffSource.VERIFIED_REPORT.value
                ),
                "snapshot": snapshot,
            },
        )
    return cursor


def _binding_diagnostic_reason(
    binding: IssueChatBinding,
    *,
    policy: IssueChatWorkflowPolicy | None,
    now: datetime,
    manual_resume_cutoff_at: datetime | None,
    completion_cutoff_at: datetime | None,
    completion_snapshots: list[dict],
) -> str:
    if policy is None:
        return REASON_NOT_CONFIGURED
    if not policy.enabled:
        return REASON_DISABLED
    if not _binding_is_workflow_source(binding) or binding.observation_observed_at is None:
        return REASON_NO_ACTIONABLE_OBSERVATION
    if binding.observation_observed_at < policy.effective_at:
        return REASON_BEFORE_ACTIVATION
    if (
        manual_resume_cutoff_at is not None
        and binding.observation_observed_at <= manual_resume_cutoff_at
    ):
        return REASON_MANUAL_RESUME_REQUIRES_NEWER_EVENT
    if (
        completion_cutoff_at is not None
        and binding.observation_observed_at <= completion_cutoff_at
    ) or _binding_matches_completion_snapshot(binding, completion_snapshots):
        return REASON_COMPLETION_PRESERVED
    if binding.observation_state == IssueChatBinding.ObservationState.RUNNING:
        cutoff = now - timedelta(seconds=policy.stale_running_ttl_seconds)
        return "active_running" if binding.observation_observed_at >= cutoff else "stale_running"
    if binding.observation_state in ATTENTION_OBSERVATION_STATES:
        return _attention_reason(binding.observation_state)
    return REASON_NO_ACTIONABLE_OBSERVATION


def get_issue_chat_workflow_diagnostics(
    *,
    workspace_slug: str,
    project_id: UUID,
    binding_limit: int = 500,
) -> dict:
    """Return a read-only policy, binding, cursor, and decision diagnostic snapshot."""

    project = _project(workspace_slug, project_id)
    policy = (
        IssueChatWorkflowPolicy.objects.filter(project=project, deleted_at__isnull=True)
        .select_related(*POLICY_STATE_FIELDS)
        .first()
    )
    last_audit = IssueChatWorkflowAudit.objects.filter(binding_id=OuterRef("pk")).order_by("-created_at")
    issue_cursor = IssueChatWorkflowCursor.objects.filter(
        issue_id=OuterRef("issue_id"),
        deleted_at__isnull=True,
    ).order_by("-updated_at")
    bindings = list(
        IssueChatBinding.objects.filter(
            project=project,
            deleted_at__isnull=True,
            issue__deleted_at__isnull=True,
        )
        .select_related("issue", "issue__state")
        .annotate(
            last_decision_reason=Subquery(last_audit.values("reason")[:1]),
            last_decision_applied=Subquery(last_audit.values("applied")[:1]),
            last_decision_at=Subquery(last_audit.values("created_at")[:1]),
            last_decision_target_state_id=Subquery(last_audit.values("target_state_id")[:1]),
            workflow_manual_resume_cutoff_at=Subquery(
                issue_cursor.values("manual_resume_cutoff_at")[:1]
            ),
            workflow_completion_cutoff_at=Subquery(
                issue_cursor.values("completion_cutoff_at")[:1]
            ),
            workflow_completion_snapshots=Subquery(
                issue_cursor.values("completion_snapshots")[:1]
            ),
        )
        .order_by("issue_id", "id")[:binding_limit]
    )
    now = timezone.now()
    binding_rows = []
    reason_counts = {}
    for binding in bindings:
        reason = _binding_diagnostic_reason(
            binding,
            policy=policy,
            now=now,
            manual_resume_cutoff_at=binding.workflow_manual_resume_cutoff_at,
            completion_cutoff_at=binding.workflow_completion_cutoff_at,
            completion_snapshots=list(binding.workflow_completion_snapshots or []),
        )
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        binding_rows.append(
            {
                "binding_id": str(binding.id),
                "issue_id": str(binding.issue_id),
                "issue_identifier": f"{project.identifier}-{binding.issue.sequence_id}",
                "current_state": {
                    "id": str(binding.issue.state_id) if binding.issue.state_id else None,
                    "name": binding.issue.state.name if binding.issue.state else None,
                    "group": binding.issue.state.group if binding.issue.state else None,
                },
                "observation_state": binding.observation_state,
                "observation_source": binding.observation_source,
                "observed_at": binding.observation_observed_at,
                "turn_id": binding.observation_turn_id,
                "reason": reason,
                "last_decision": {
                    "reason": binding.last_decision_reason,
                    "applied": binding.last_decision_applied,
                    "at": binding.last_decision_at,
                    "target_state_id": (
                        str(binding.last_decision_target_state_id)
                        if binding.last_decision_target_state_id
                        else None
                    ),
                },
            }
        )

    recent_audits = [
        {
            "id": str(audit.id),
            "issue_id": str(audit.issue_id),
            "binding_id": str(audit.binding_id) if audit.binding_id else None,
            "reason": audit.reason,
            "applied": audit.applied,
            "observation_state": audit.observation_state,
            "observed_at": audit.observed_at,
            "turn_id": audit.turn_id,
            "policy_version": audit.policy_version,
            "target_state_id": str(audit.target_state_id) if audit.target_state_id else None,
            "created_at": audit.created_at,
        }
        for audit in IssueChatWorkflowAudit.objects.filter(project=project)
        .select_related("target_state")
        .order_by("-created_at")[:20]
    ]
    cursor_count = IssueChatWorkflowCursor.objects.filter(
        project=project,
        deleted_at__isnull=True,
    ).count()
    workflow_cursors = [
        {
            "issue_id": str(cursor.issue_id),
            "expected_state_id": (
                str(cursor.expected_state_id) if cursor.expected_state_id else None
            ),
            "manual_resume_cutoff_at": cursor.manual_resume_cutoff_at,
            "manual_resume_cutoff_source": cursor.manual_resume_cutoff_source or None,
            "completion_cutoff_at": cursor.completion_cutoff_at,
            "completion_cutoff_source": cursor.completion_cutoff_source or None,
            "completion_snapshot_count": len(cursor.completion_snapshots or []),
            "last_reason": cursor.last_reason,
        }
        for cursor in IssueChatWorkflowCursor.objects.filter(project=project, deleted_at__isnull=True)
        .order_by("-updated_at")[:50]
    ]
    audit_count = IssueChatWorkflowAudit.objects.filter(project=project).count()
    policy_payload = None
    policy_errors = []
    if policy is not None:
        policy_errors = _policy_errors(policy)
        policy_payload = {
            "id": str(policy.id),
            "enabled": policy.enabled,
            "version": policy.version,
            "effective_at": policy.effective_at,
            "stale_running_ttl_seconds": policy.stale_running_ttl_seconds,
            "states": {
                field: (
                    {
                        "id": str(getattr(policy, field).id),
                        "name": getattr(policy, field).name,
                        "group": getattr(policy, field).group,
                    }
                    if getattr(policy, field) is not None
                    else None
                )
                for field in POLICY_STATE_FIELDS
            },
        }

    return {
        "mode": "diagnostics",
        "writes_performed": False,
        "project": {
            "id": str(project.id),
            "workspace": project.workspace.slug,
            "identifier": project.identifier,
            "name": project.name,
        },
        "policy": policy_payload,
        "validation": {
            "valid": policy is not None and not policy_errors,
            "errors": policy_errors,
        },
        "counts": {
            "bindings": IssueChatBinding.objects.filter(
                project=project,
                deleted_at__isnull=True,
                issue__deleted_at__isnull=True,
            ).count(),
            "bindings_shown": len(binding_rows),
            "cursors": cursor_count,
            "audits": audit_count,
        },
        "binding_reason_counts": dict(sorted(reason_counts.items())),
        "bindings": binding_rows,
        "workflow_cursors": workflow_cursors,
        "recent_decisions": recent_audits,
    }
