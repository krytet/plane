# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Module imports
from plane.db.models import JiraAutomationConsent, JiraSyncDirection, ProjectMember
from plane.db.models.project import ROLE

from .policy import compute_automation_scope_hash


def effective_direction(binding, policy):
    override = binding.sync_override if binding else "inherit"
    if override and override != "inherit":
        return override
    return policy.direction if policy else JiraSyncDirection.OFF


def actor_can_use_grant(actor, grant):
    if actor is None or not getattr(actor, "is_authenticated", False):
        return False
    if grant is None or grant.user_id != actor.id or grant.deleted_at is not None:
        return False
    if grant.site.deleted_at is not None or not grant.site.enabled:
        return False
    return True


def require_binding_member(actor, project, *, minimum_role=ROLE.MEMBER):
    return ProjectMember.objects.filter(
        project=project,
        member=actor,
        workspace=project.workspace,
        role__in=[ROLE.ADMIN.value, minimum_role.value] if isinstance(minimum_role, ROLE) else [minimum_role],
        is_active=True,
        deleted_at__isnull=True,
    ).exists()


def active_consent(binding, *, at=None):
    return (
        JiraAutomationConsent.objects.filter(
            binding=binding,
            revoked_at__isnull=True,
            deleted_at__isnull=True,
            owner_id=binding.automation_owner_id,
        )
        .order_by("-version")
        .first()
    )


def consent_is_current(binding, policy, *, at=None):
    consent = active_consent(binding, at=at)
    if not consent or not binding.automation_owner_id:
        return False
    current_hash = compute_automation_scope_hash(
        {
            "direction": effective_direction(binding, policy),
            "state_mappings": policy.state_mappings,
            "close_enabled": policy.close_enabled,
            "reopen_enabled": policy.reopen_enabled,
            "jira_project_key": policy.jira_project_key,
            "jira_issue_type_id": policy.jira_issue_type_id,
            "allowed_events": binding.automation_scope.get("allowed_events", []),
        }
    )
    return consent.scope_hash == current_hash
