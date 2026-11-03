# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.conf import settings
from django.db import models
from django.db.models import Q

# Module imports
from plane.db.models.base import BaseModel
from plane.db.models.project import ProjectBaseModel


class JiraAuthType(models.TextChoices):
    API_TOKEN = "api_token", "Personal API token"
    OAUTH = "oauth", "OAuth"


class JiraSyncDirection(models.TextChoices):
    OFF = "off", "Off"
    PLANE_TO_JIRA = "plane_to_jira", "Plane to Jira"
    JIRA_TO_PLANE = "jira_to_plane", "Jira to Plane"
    BIDIRECTIONAL = "bidirectional", "Bidirectional"


class JiraCredentialHealth(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    HEALTHY = "healthy", "Healthy"
    REAUTH_REQUIRED = "reauth_required", "Reauthentication required"
    REVOKED = "revoked", "Revoked"


class JiraOperationKind(models.TextChoices):
    CREATE_ISSUE = "create_issue", "Create issue"
    TRANSITION_ISSUE = "transition_issue", "Transition issue"
    DEVELOPMENT_PLAN = "development_plan", "Development plan"


class JiraOperationState(models.TextChoices):
    QUEUED = "queued", "Queued"
    DISPATCHING = "dispatching", "Dispatching"
    VERIFYING = "verifying", "Verifying"
    SUCCEEDED = "succeeded", "Succeeded"
    OUTCOME_UNKNOWN = "outcome_unknown", "Outcome unknown"
    CONFLICT = "conflict", "Conflict"
    BLOCKED_AUTH = "blocked_auth", "Blocked by authentication"
    BLOCKED_POLICY = "blocked_policy", "Blocked by policy"
    FAILED = "failed", "Failed"


class JiraSiteRegistration(BaseModel):
    """Workspace-level Jira site registration without user credentials."""

    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="jira_site_registrations",
    )
    site_url = models.URLField(max_length=500)
    site_id = models.CharField(max_length=255, blank=True, default="")
    enabled = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "jira_site_registrations"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "site_url"],
                condition=Q(deleted_at__isnull=True),
                name="jira_site_unique_active_workspace_url",
            )
        ]


class JiraUserGrant(BaseModel):
    """A user's own Jira identity and encrypted credential for one site."""

    site = models.ForeignKey(
        JiraSiteRegistration,
        on_delete=models.CASCADE,
        related_name="user_grants",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="jira_user_grants",
    )
    account_id = models.CharField(max_length=255)
    display_name = models.CharField(max_length=500, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    auth_type = models.CharField(
        max_length=20,
        choices=JiraAuthType.choices,
        default=JiraAuthType.API_TOKEN,
    )
    encrypted_credential = models.TextField()
    scopes = models.JSONField(default=list, blank=True)
    credential_version = models.PositiveIntegerField(default=1)
    health = models.CharField(
        max_length=30,
        choices=JiraCredentialHealth.choices,
        default=JiraCredentialHealth.UNKNOWN,
    )
    last_verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "jira_user_grants"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["site", "user"],
                condition=Q(deleted_at__isnull=True),
                name="jira_grant_unique_active_site_user",
            )
        ]


class JiraProjectPolicy(ProjectBaseModel):
    """Project-level routing policy. It contains no personal credentials."""

    site = models.ForeignKey(
        JiraSiteRegistration,
        on_delete=models.PROTECT,
        related_name="project_policies",
    )
    jira_project_key = models.CharField(max_length=64)
    jira_project_id = models.CharField(max_length=255, blank=True, default="")
    jira_issue_type_id = models.CharField(max_length=255)
    jira_issue_type_name = models.CharField(max_length=255, blank=True, default="")
    board_id = models.CharField(max_length=255, blank=True, default="")
    direction = models.CharField(
        max_length=30,
        choices=JiraSyncDirection.choices,
        default=JiraSyncDirection.OFF,
    )
    close_enabled = models.BooleanField(default=False)
    reopen_enabled = models.BooleanField(default=False)
    state_mappings = models.JSONField(default=dict, blank=True)
    template_version = models.CharField(max_length=50, default="jackrabbit-english-v1")
    repository_base_ref = models.CharField(max_length=255, default="master")
    repository_target_ref = models.CharField(max_length=255, default="staging")
    branch_template = models.CharField(max_length=255, default="{jira_key}-{slug}")
    pr_title_template = models.CharField(max_length=255, default="{jira_key}: {summary}")
    enabled = models.BooleanField(default=False)
    version = models.PositiveBigIntegerField(default=1)
    effective_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "jira_project_policies"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["project"],
                condition=Q(deleted_at__isnull=True),
                name="jira_policy_unique_active_project",
            )
        ]


class JiraIssueBinding(ProjectBaseModel):
    """A durable link between one Plane issue and one Jira issue."""

    issue = models.ForeignKey(
        "db.Issue",
        on_delete=models.CASCADE,
        related_name="jira_bindings",
    )
    site = models.ForeignKey(
        JiraSiteRegistration,
        on_delete=models.PROTECT,
        related_name="issue_bindings",
    )
    jira_issue_id = models.CharField(max_length=255)
    jira_issue_key = models.CharField(max_length=64)
    jira_project_key = models.CharField(max_length=64)
    jira_issue_type_id = models.CharField(max_length=255, blank=True, default="")
    jira_status_id = models.CharField(max_length=255, blank=True, default="")
    jira_status_name = models.CharField(max_length=255, blank=True, default="")
    remote_fingerprint = models.CharField(max_length=128, blank=True, default="")
    generation = models.PositiveIntegerField(default=1)
    sync_override = models.CharField(
        max_length=30,
        choices=JiraSyncDirection.choices,
        default="inherit",
    )
    automation_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="jira_automation_bindings",
    )
    automation_scope = models.JSONField(default=dict, blank=True)
    consent_version = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "jira_issue_bindings"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["project", "issue"],
                condition=Q(is_active=True, deleted_at__isnull=True),
                name="jira_binding_unique_active_issue",
            ),
            models.UniqueConstraint(
                fields=["project", "site", "jira_issue_id"],
                condition=Q(is_active=True, deleted_at__isnull=True),
                name="jira_binding_unique_active_remote_issue",
            ),
        ]


class JiraAutomationConsent(BaseModel):
    """Explicit self-consent for background work on one binding."""

    binding = models.ForeignKey(
        JiraIssueBinding,
        on_delete=models.CASCADE,
        related_name="automation_consents",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="jira_automation_consents",
    )
    scope_hash = models.CharField(max_length=128)
    scope = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "jira_automation_consents"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["binding", "owner"],
                condition=Q(revoked_at__isnull=True, deleted_at__isnull=True),
                name="jira_consent_unique_active_binding_owner",
            )
        ]


class JiraDraft(ProjectBaseModel):
    """An immutable reviewed payload for creating a Jira issue."""

    issue = models.ForeignKey(
        "db.Issue",
        on_delete=models.CASCADE,
        related_name="jira_drafts",
    )
    policy = models.ForeignKey(
        JiraProjectPolicy,
        on_delete=models.CASCADE,
        related_name="drafts",
    )
    summary = models.CharField(max_length=500)
    description_markdown = models.TextField()
    description_adf = models.JSONField(default=dict, blank=True)
    fields = models.JSONField(default=dict, blank=True)
    source_revision = models.CharField(max_length=128)
    schema_version = models.CharField(max_length=128, blank=True, default="")
    template_version = models.CharField(max_length=50)
    approval_hash = models.CharField(max_length=128, blank=True, default="")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_jira_drafts",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    validation = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "jira_drafts"
        ordering = ("-created_at",)


class JiraOperation(ProjectBaseModel):
    """Durable external intent and its readback receipt."""

    issue = models.ForeignKey(
        "db.Issue",
        on_delete=models.CASCADE,
        related_name="jira_operations",
    )
    binding = models.ForeignKey(
        JiraIssueBinding,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operations",
    )
    kind = models.CharField(max_length=40, choices=JiraOperationKind.choices)
    idempotency_key = models.CharField(max_length=200)
    payload_hash = models.CharField(max_length=128)
    origin = models.CharField(max_length=40, default="human")
    state = models.CharField(
        max_length=40,
        choices=JiraOperationState.choices,
        default=JiraOperationState.QUEUED,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="jira_operations",
    )
    credential_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="credential_owner_jira_operations",
    )
    consent_version = models.PositiveIntegerField(default=0)
    payload = models.JSONField(default=dict)
    result = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=100, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    correlation_id = models.CharField(max_length=64, db_index=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "jira_operations"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["project", "issue", "kind", "idempotency_key"],
                name="jira_operation_unique_project_issue_kind_key",
            )
        ]


class JiraSyncEvent(BaseModel):
    """Append-only, redacted integration audit."""

    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="jira_sync_events",
    )
    project = models.ForeignKey(
        "db.Project",
        on_delete=models.CASCADE,
        related_name="jira_sync_events",
        null=True,
        blank=True,
    )
    issue = models.ForeignKey(
        "db.Issue",
        on_delete=models.CASCADE,
        related_name="jira_sync_events",
        null=True,
        blank=True,
    )
    binding = models.ForeignKey(
        JiraIssueBinding,
        on_delete=models.SET_NULL,
        related_name="sync_events",
        null=True,
        blank=True,
    )
    operation = models.ForeignKey(
        JiraOperation,
        on_delete=models.SET_NULL,
        related_name="sync_events",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="jira_sync_events",
        null=True,
        blank=True,
    )
    authority_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="authority_jira_sync_events",
        null=True,
        blank=True,
    )
    jira_actor = models.CharField(max_length=255, blank=True, default="")
    action = models.CharField(max_length=100)
    detail = models.JSONField(default=dict, blank=True)
    external_id = models.CharField(max_length=255, blank=True, default="")
    fingerprint = models.CharField(max_length=128, blank=True, default="")

    class Meta:
        db_table = "jira_sync_events"
        ordering = ("-created_at",)
