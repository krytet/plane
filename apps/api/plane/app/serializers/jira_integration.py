# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party imports
from rest_framework import serializers

# Module imports
from plane.db.models import (
    JiraAutomationConsent,
    JiraDraft,
    JiraIssueBinding,
    JiraOperation,
    JiraProjectPolicy,
)


class JiraCredentialInputSerializer(serializers.Serializer):
    site_url = serializers.URLField(max_length=500)
    email = serializers.EmailField(max_length=320)
    api_token = serializers.CharField(max_length=2000, trim_whitespace=False, write_only=True)


class JiraGrantSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    site_id = serializers.UUIDField(source="site.id")
    site_url = serializers.URLField(source="site.site_url")
    account_id = serializers.CharField()
    display_name = serializers.CharField()
    email = serializers.EmailField()
    auth_type = serializers.CharField()
    health = serializers.CharField()
    credential_version = serializers.IntegerField()
    last_verified_at = serializers.DateTimeField(allow_null=True)


class JiraProjectPolicySerializer(serializers.ModelSerializer):
    site_id = serializers.UUIDField(source="site.id", read_only=True)

    class Meta:
        model = JiraProjectPolicy
        fields = [
            "id",
            "site_id",
            "jira_project_key",
            "jira_project_id",
            "jira_issue_type_id",
            "jira_issue_type_name",
            "board_id",
            "direction",
            "close_enabled",
            "reopen_enabled",
            "state_mappings",
            "template_version",
            "repository_base_ref",
            "repository_target_ref",
            "branch_template",
            "pr_title_template",
            "enabled",
            "version",
            "effective_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "version", "effective_at", "created_at", "updated_at"]


class JiraIssueBindingSerializer(serializers.ModelSerializer):
    jira_url = serializers.SerializerMethodField()

    class Meta:
        model = JiraIssueBinding
        fields = [
            "id",
            "jira_issue_key",
            "jira_project_key",
            "jira_status_id",
            "jira_status_name",
            "jira_url",
            "sync_override",
            "automation_owner",
            "consent_version",
            "last_verified_at",
            "last_error",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_jira_url(self, obj):
        return f"{obj.site.site_url}/browse/{obj.jira_issue_key}"


class JiraBindingInputSerializer(serializers.Serializer):
    issue_key = serializers.CharField(max_length=64)
    sync_override = serializers.ChoiceField(
        choices=["inherit", "off", "plane_to_jira", "jira_to_plane", "bidirectional"],
        default="inherit",
    )


class JiraDraftSerializer(serializers.ModelSerializer):
    class Meta:
        model = JiraDraft
        fields = [
            "id",
            "summary",
            "description_markdown",
            "fields",
            "source_revision",
            "template_version",
            "approval_hash",
            "approved_at",
            "expires_at",
            "validation",
            "created_at",
        ]
        read_only_fields = fields


class JiraDraftInputSerializer(serializers.Serializer):
    summary = serializers.CharField(max_length=500, required=False, allow_blank=True)
    description_markdown = serializers.CharField(required=False, allow_blank=True)
    fields = serializers.JSONField(required=False)
    extra_context = serializers.CharField(required=False, allow_blank=True)


class JiraDraftApproveSerializer(serializers.Serializer):
    approval_hash = serializers.CharField(max_length=128)


class JiraDraftSubmitSerializer(serializers.Serializer):
    approval_hash = serializers.CharField(max_length=128)
    fields = serializers.JSONField(required=False)
    idempotency_key = serializers.CharField(max_length=200)


class JiraConsentInputSerializer(serializers.Serializer):
    allowed_events = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        default=list,
    )


class JiraConsentSerializer(serializers.ModelSerializer):
    class Meta:
        model = JiraAutomationConsent
        fields = ["id", "scope_hash", "scope", "version", "granted_at", "revoked_at"]
        read_only_fields = fields


class JiraOperationSerializer(serializers.ModelSerializer):
    class Meta:
        model = JiraOperation
        fields = [
            "id",
            "kind",
            "idempotency_key",
            "origin",
            "state",
            "error_code",
            "error_message",
            "attempts",
            "correlation_id",
            "result",
            "next_attempt_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
