# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import hashlib

# Django imports
from django.db import transaction
from django.utils import timezone

# Module imports
from plane.db.models import (
    Issue,
    JiraIssueBinding,
    JiraOperation,
    JiraOperationKind,
    JiraOperationState,
    JiraProjectPolicy,
    JiraSyncDirection,
    JiraSyncEvent,
    JiraUserGrant,
    State,
    ProjectMember,
)

from .authorization import consent_is_current, effective_direction
from .provider import JiraApiError, JiraClient


BLOCKED_INBOUND_STATE_NAMES = {"llm complete"}


def _grant_for(user, site):
    return JiraUserGrant.objects.filter(
        user=user,
        site=site,
        deleted_at__isnull=True,
    ).first()


def _event(issue, *, binding=None, operation=None, actor=None, authority_owner=None, action, detail=None):
    return JiraSyncEvent.objects.create(
        workspace=issue.project.workspace,
        project=issue.project,
        issue=issue,
        binding=binding,
        operation=operation,
        actor=actor,
        authority_owner=authority_owner,
        jira_actor="unknown",
        action=action,
        detail=detail or {},
    )


def queue_state_change(*, issue, actor, origin="human", credential_owner=None):
    """Persist an outbound intent without making a network call."""
    binding = (
        JiraIssueBinding.objects.filter(
            issue=issue,
            is_active=True,
            deleted_at__isnull=True,
        )
        .select_related("site", "project")
        .first()
    )
    if binding is None:
        return None
    policy = JiraProjectPolicy.objects.filter(project=issue.project, deleted_at__isnull=True).first()
    if policy is None or not policy.enabled:
        _event(issue, binding=binding, actor=actor, authority_owner=actor, action="state_sync_blocked_policy")
        return None
    direction = effective_direction(binding, policy)
    if direction not in {JiraSyncDirection.PLANE_TO_JIRA, JiraSyncDirection.BIDIRECTIONAL}:
        _event(issue, binding=binding, actor=actor, authority_owner=actor, action="state_sync_not_enabled")
        return None
    mapping = (policy.state_mappings or {}).get(str(issue.state_id)) or {}
    target_status_id = str(mapping.get("jira_status_id") or "").strip()
    if not target_status_id:
        _event(issue, binding=binding, actor=actor, authority_owner=actor, action="state_sync_unmapped")
        return None
    if origin == "human":
        if credential_owner is not None and credential_owner != actor:
            _event(
                issue,
                binding=binding,
                actor=actor,
                authority_owner=actor,
                action="state_sync_wrong_credential_owner",
            )
            return None
        owner = actor
    else:
        owner = credential_owner or binding.automation_owner
        if owner is None or not consent_is_current(binding, policy):
            _event(issue, binding=binding, actor=actor, authority_owner=owner, action="state_sync_consent_missing")
            return None
    grant = _grant_for(owner, binding.site)
    if grant is None:
        _event(issue, binding=binding, actor=actor, authority_owner=owner, action="state_sync_blocked_auth")
        return None
    if not ProjectMember.objects.filter(
        project=issue.project,
        member=owner,
        workspace=issue.project.workspace,
        is_active=True,
        deleted_at__isnull=True,
    ).exists():
        _event(issue, binding=binding, actor=actor, authority_owner=owner, action="state_sync_actor_not_member")
        return None
    operation, created = JiraOperation.objects.get_or_create(
        project=issue.project,
        issue=issue,
        kind=JiraOperationKind.TRANSITION_ISSUE,
        idempotency_key=f"state:{issue.id}:{issue.state_id}:{issue.updated_at.isoformat()}",
        defaults={
            "workspace": issue.project.workspace,
            "binding": binding,
            "payload_hash": hashlib.sha256(
                f"{binding.generation}:{issue.state_id}:{target_status_id}".encode("utf-8")
            ).hexdigest(),
            "payload": {
                "origin": origin,
                "binding_id": str(binding.id),
                "generation": binding.generation,
                "target_status_id": target_status_id,
                "issue_state_id": str(issue.state_id),
            },
            "actor": actor,
            "credential_owner": owner,
            "consent_version": binding.consent_version,
            "origin": origin,
            "correlation_id": hashlib.sha256(
                f"{issue.id}:{issue.state_id}:{issue.updated_at.isoformat()}".encode("utf-8")
            ).hexdigest()[:32],
        },
    )
    if created:
        _event(
            issue,
            binding=binding,
            operation=operation,
            actor=actor,
            authority_owner=owner,
            action="state_sync_queued",
            detail={"target_status_id": target_status_id, "origin": origin},
        )
    return operation


def dispatch_operation(operation):
    """Perform one queued transition and read it back. Never retries a POST blindly."""
    if operation.state not in {JiraOperationState.QUEUED, JiraOperationState.DISPATCHING}:
        return operation
    if operation.kind != JiraOperationKind.TRANSITION_ISSUE:
        return operation
    binding = operation.binding
    if binding is None or not binding.is_active or binding.generation != operation.payload.get("generation"):
        operation.state = JiraOperationState.BLOCKED_POLICY
        operation.error_code = "binding_generation_stale"
        operation.save(update_fields=["state", "error_code", "updated_at"])
        return operation
    policy = JiraProjectPolicy.objects.filter(project=operation.project, deleted_at__isnull=True).first()
    if policy is None or not policy.enabled:
        operation.state = JiraOperationState.BLOCKED_POLICY
        operation.error_code = "policy_disabled"
        operation.save(update_fields=["state", "error_code", "updated_at"])
        return operation
    if operation.origin != "human":
        if not consent_is_current(binding, policy):
            operation.state = JiraOperationState.BLOCKED_POLICY
            operation.error_code = "consent_stale"
            operation.save(update_fields=["state", "error_code", "updated_at"])
            return operation
    executing_user = operation.actor if operation.origin == "human" else operation.credential_owner
    grant = _grant_for(executing_user, binding.site)
    if grant is None:
        operation.state = JiraOperationState.BLOCKED_AUTH
        operation.error_code = "personal_grant_missing"
        operation.save(update_fields=["state", "error_code", "updated_at"])
        return operation
    if not ProjectMember.objects.filter(
        project=operation.project,
        member=executing_user,
        workspace=operation.project.workspace,
        is_active=True,
        deleted_at__isnull=True,
    ).exists():
        operation.state = JiraOperationState.BLOCKED_AUTH
        operation.error_code = "executing_user_not_member"
        operation.save(update_fields=["state", "error_code", "updated_at"])
        return operation
    operation.state = JiraOperationState.DISPATCHING
    operation.attempts += 1
    operation.save(update_fields=["state", "attempts", "updated_at"])
    client = JiraClient.from_grant(grant)
    target_status_id = str(operation.payload.get("target_status_id") or "")
    try:
        transitions = client.get_transitions(binding.jira_issue_key)
        transition = next(
            (item for item in transitions if str((item.get("to") or {}).get("id")) == target_status_id),
            None,
        )
        if transition is None:
            operation.state = JiraOperationState.BLOCKED_POLICY
            operation.error_code = "transition_unavailable"
            operation.error_message = "The target status is not reachable from the current Jira status"
            operation.save(update_fields=["state", "error_code", "error_message", "updated_at"])
            return operation
        remote = client.transition_issue(binding.jira_issue_key, transition.get("id"))
        remote_status = str((remote.get("fields") or {}).get("status", {}).get("id") or "")
        if remote_status != target_status_id:
            operation.state = JiraOperationState.CONFLICT
            operation.error_code = "transition_readback_mismatch"
            operation.error_message = "Jira accepted the transition but readback did not match the target status"
            operation.save(update_fields=["state", "error_code", "error_message", "updated_at"])
            return operation
        with transaction.atomic():
            binding.jira_status_id = remote_status
            binding.jira_status_name = str((remote.get("fields") or {}).get("status", {}).get("name") or "")
            binding.last_verified_at = timezone.now()
            binding.remote_fingerprint = hashlib.sha256(
                f"{binding.jira_issue_id}:{binding.jira_issue_key}:{remote_status}".encode("utf-8")
            ).hexdigest()
            binding.save(
                update_fields=[
                    "jira_status_id",
                    "jira_status_name",
                    "last_verified_at",
                    "remote_fingerprint",
                    "updated_at",
                ]
            )
            operation.state = JiraOperationState.SUCCEEDED
            operation.error_code = ""
            operation.error_message = ""
            operation.result = {"jira_status_id": remote_status, "jira_issue_key": binding.jira_issue_key}
            operation.save(update_fields=["state", "error_code", "error_message", "result", "updated_at"])
            _event(
                binding.issue,
                binding=binding,
                operation=operation,
                actor=executing_user,
                authority_owner=operation.credential_owner,
                action="state_sync_verified",
                detail={"target_status_id": target_status_id},
            )
    except JiraApiError as exc:
        if exc.code in {"jira_transport_error", "jira_invalid_response"}:
            operation.state = JiraOperationState.OUTCOME_UNKNOWN
            operation.error_code = exc.code
            operation.error_message = exc.message[:2000]
        elif exc.code == "jira_auth_error":
            operation.state = JiraOperationState.BLOCKED_AUTH
            operation.error_code = exc.code
            operation.error_message = exc.message[:2000]
        else:
            operation.state = JiraOperationState.FAILED
            operation.error_code = exc.code
            operation.error_message = exc.message[:2000]
        operation.save(update_fields=["state", "error_code", "error_message", "updated_at"])
    return operation


def process_pending_operations(limit=50):
    processed = []
    operations = JiraOperation.objects.filter(
        kind=JiraOperationKind.TRANSITION_ISSUE,
        state=JiraOperationState.QUEUED,
        deleted_at__isnull=True,
    ).select_related("binding", "binding__site", "binding__issue", "project", "actor", "credential_owner")[:limit]
    for operation in operations:
        processed.append(dispatch_operation(operation))
    return processed


def poll_binding(binding):
    """Read one remote status and apply only an explicitly consented inbound mapping."""
    policy = JiraProjectPolicy.objects.filter(project=binding.project, deleted_at__isnull=True).first()
    if policy is None or not policy.enabled:
        return {"applied": False, "reason": "policy_missing"}
    direction = effective_direction(binding, policy)
    if direction not in {JiraSyncDirection.JIRA_TO_PLANE, JiraSyncDirection.BIDIRECTIONAL}:
        return {"applied": False, "reason": "inbound_not_enabled"}
    if not binding.automation_owner_id or not consent_is_current(binding, policy):
        return {"applied": False, "reason": "consent_missing"}
    owner = binding.automation_owner
    if (
        not owner.is_active
        or not owner.member_workspace.filter(
            workspace=binding.project.workspace,
            is_active=True,
            deleted_at__isnull=True,
        ).exists()
    ):
        return {"applied": False, "reason": "automation_owner_unavailable"}
    grant = _grant_for(owner, binding.site)
    if grant is None:
        return {"applied": False, "reason": "personal_grant_missing"}
    try:
        remote = JiraClient.from_grant(grant).get_issue(binding.jira_issue_key)
    except JiraApiError as exc:
        return {"applied": False, "reason": exc.code}
    remote_status_id = str((remote.get("fields") or {}).get("status", {}).get("id") or "")
    if remote_status_id == binding.jira_status_id:
        binding.last_verified_at = timezone.now()
        binding.save(update_fields=["last_verified_at", "updated_at"])
        return {"applied": False, "reason": "no_change"}
    matching_targets = {
        str(mapping.get("plane_state_id"))
        for mapping in (policy.state_mappings or {}).values()
        if str(mapping.get("jira_status_id") or "") == remote_status_id and mapping.get("plane_state_id")
    }
    if len(matching_targets) != 1:
        return {"applied": False, "reason": "reverse_mapping_ambiguous"}
    target_state = State.objects.filter(
        id=next(iter(matching_targets)),
        project=binding.project,
        deleted_at__isnull=True,
    ).first()
    if target_state is None or target_state.name.strip().lower() in BLOCKED_INBOUND_STATE_NAMES:
        return {"applied": False, "reason": "inbound_target_blocked"}
    issue = Issue.issue_objects.select_related("project", "state").get(id=binding.issue_id)
    old_state_id = issue.state_id
    if str(old_state_id) != str(target_state.id):
        issue.state = target_state
        issue.save(update_fields=["state", "completed_at", "updated_at"])
        _event(
            issue,
            binding=binding,
            actor=owner,
            authority_owner=owner,
            action="inbound_state_applied",
            detail={
                "from_state_id": str(old_state_id),
                "to_state_id": str(target_state.id),
                "jira_status_id": remote_status_id,
                "jira_actor": "unknown",
            },
        )
    binding.jira_status_id = remote_status_id
    binding.jira_status_name = str((remote.get("fields") or {}).get("status", {}).get("name") or "")
    binding.last_verified_at = timezone.now()
    binding.save(update_fields=["jira_status_id", "jira_status_name", "last_verified_at", "updated_at"])
    return {"applied": str(old_state_id) != str(target_state.id), "reason": "verified"}
