# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import hashlib

# Django imports
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.permissions import (
    ROLE,
    ProjectAdminPermission,
    ProjectEntityPermission,
    WorkspaceMemberPermission,
    allow_permission,
)
from plane.app.serializers import (
    JiraBindingInputSerializer,
    JiraConsentInputSerializer,
    JiraConsentSerializer,
    JiraCredentialInputSerializer,
    JiraDraftApproveSerializer,
    JiraDraftInputSerializer,
    JiraDraftSerializer,
    JiraDraftSubmitSerializer,
    JiraGrantSerializer,
    JiraIssueBindingSerializer,
    JiraOperationSerializer,
    JiraProjectPolicySerializer,
)
from plane.db.models import (
    Issue,
    JiraAutomationConsent,
    JiraCredentialHealth,
    JiraDraft,
    JiraIssueBinding,
    JiraOperation,
    JiraOperationKind,
    JiraOperationState,
    JiraProjectPolicy,
    JiraSiteRegistration,
    JiraSyncDirection,
    JiraSyncEvent,
    JiraUserGrant,
    Project,
    State,
    Workspace,
)
from plane.utils.jira_integration import (
    JiraApiError,
    JiraClient,
    JiraCredentials,
    actor_can_use_grant,
    build_english_draft,
    compute_automation_scope_hash,
    encrypt_credential,
    effective_direction,
    markdown_to_adf,
    normalize_site_url,
    validate_policy_input,
)
from plane.utils.jira_integration.errors import JiraIntegrationError
from plane.utils.jira_integration.operations import (
    complete_operation,
    create_operation,
    mark_operation_unknown,
)
from plane.utils.jira_integration.policy import JiraPolicyInput

from .base import BaseAPIView


def _integration_error(exc, *, field=None):
    payload = {"error": str(exc), "code": getattr(exc, "code", "jira_integration_error")}
    if field or getattr(exc, "field", None):
        payload["field"] = field or exc.field
    return Response(payload, status=getattr(exc, "status_code", status.HTTP_400_BAD_REQUEST))


def _grant_for_user(user, *, site=None, workspace=None):
    query = JiraUserGrant.objects.filter(user=user, deleted_at__isnull=True)
    if site is not None:
        query = query.filter(site=site)
    if workspace is not None:
        query = query.filter(site__workspace=workspace)
    return query.select_related("site").order_by("-created_at").first()


def _site_for_workspace(workspace, site_url):
    return JiraSiteRegistration.objects.filter(
        workspace=workspace,
        site_url=site_url,
        deleted_at__isnull=True,
    ).first()


def _readiness(workspace, project, user):
    grant = _grant_for_user(user, workspace=workspace)
    site = grant.site if grant else None
    policy = (
        JiraProjectPolicy.objects.filter(project=project, deleted_at__isnull=True).select_related("site").first()
        if project
        else None
    )
    blockers = []
    if grant is None:
        blockers.append(
            {
                "code": "personal_grant_missing",
                "responsible_role": "user",
                "action": "connect_personal_jira",
            }
        )
    if policy is None and project:
        blockers.append(
            {
                "code": "policy_missing",
                "responsible_role": "project_admin",
                "action": "configure_project_policy",
            }
        )
    if site is not None and not site.enabled:
        blockers.append(
            {
                "code": "workspace_site_disabled",
                "responsible_role": "workspace_admin",
                "action": "enable_jira_site",
            }
        )
    return {
        "allowed_sites": list(getattr(settings, "JIRA_ALLOWED_HOSTS", ["atlassian.net"])),
        "grant": JiraGrantSerializer(grant).data if grant else None,
        "site": {
            "id": str(site.id),
            "site_url": site.site_url,
            "enabled": site.enabled,
        }
        if site
        else None,
        "policy": JiraProjectPolicySerializer(policy).data if policy else None,
        "blockers": blockers,
        "ready": not blockers,
    }


def _parse_binding_remote(payload):
    fields = payload.get("fields") or {}
    project = fields.get("project") or {}
    issue_type = fields.get("issuetype") or {}
    status = fields.get("status") or {}
    issue_id = str(payload.get("id") or "").strip()
    key = str(payload.get("key") or "").strip()
    if not issue_id or not key:
        raise JiraIntegrationError("Jira returned an issue without an ID or key")
    return {
        "jira_issue_id": issue_id,
        "jira_issue_key": key,
        "jira_project_key": str(project.get("key") or ""),
        "jira_issue_type_id": str(issue_type.get("id") or ""),
        "jira_status_id": str(status.get("id") or ""),
        "jira_status_name": str(status.get("name") or ""),
        "remote_fingerprint": hashlib.sha256(
            f"{issue_id}:{key}:{status.get('id', '')}:{fields.get('updated', '')}".encode("utf-8")
        ).hexdigest(),
    }


def _require_create_metadata(client, policy, fields):
    metadata = client.get_create_fields(policy.jira_project_key, policy.jira_issue_type_id)
    projects = metadata.get("projects") or []
    issue_types = projects[0].get("issuetypes") if projects else []
    issue_type = issue_types[0] if issue_types else {}
    issue_fields = issue_type.get("fields") or {}
    missing = [
        field_key
        for field_key, field_schema in issue_fields.items()
        if field_schema.get("required") and field_key not in fields
    ]
    if missing:
        raise JiraIntegrationError(
            "Jira requires additional fields before this issue can be created",
            field="fields",
            details={"missing": missing},
        )


class JiraPersonalConnectionEndpoint(BaseAPIView):
    permission_classes = [WorkspaceMemberPermission]

    def get(self, request, slug):
        workspace = Workspace.objects.get(slug=slug)
        grant = _grant_for_user(request.user, workspace=workspace)
        if not grant:
            return Response(
                {
                    "connected": False,
                    "allowed_sites": list(getattr(settings, "JIRA_ALLOWED_HOSTS", ["atlassian.net"])),
                }
            )
        return Response({"connected": True, "grant": JiraGrantSerializer(grant).data})

    def post(self, request, slug):
        workspace = Workspace.objects.get(slug=slug)
        serializer = JiraCredentialInputSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        try:
            site_url = normalize_site_url(serializer.validated_data["site_url"])
            client = JiraClient(
                JiraCredentials(
                    site_url=site_url,
                    email=serializer.validated_data["email"],
                    api_token=serializer.validated_data["api_token"],
                )
            )
            identity = client.myself()
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except JiraApiError as exc:
            return Response(
                {"error": exc.message, "code": exc.code, "retryable": exc.retryable},
                status=status.HTTP_400_BAD_REQUEST if exc.status_code in (401, 403) else status.HTTP_502_BAD_GATEWAY,
            )
        account_id = str(identity.get("accountId") or "").strip()
        if not account_id:
            return Response(
                {"error": "Jira did not return an accountId", "code": "jira_account_missing"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        with transaction.atomic():
            existed = _grant_for_user(request.user, workspace=workspace)
            site, _ = JiraSiteRegistration.objects.get_or_create(
                workspace=workspace,
                site_url=site_url,
                defaults={"enabled": True, "verified_at": timezone.now()},
            )
            if not site.enabled:
                site.enabled = True
                site.verified_at = timezone.now()
                site.save(update_fields=["enabled", "verified_at", "updated_at"])
            grant = _grant_for_user(request.user, site=site)
            values = {
                "account_id": account_id,
                "display_name": str(identity.get("displayName") or ""),
                "email": str(identity.get("emailAddress") or serializer.validated_data["email"]),
                "encrypted_credential": encrypt_credential(serializer.validated_data["api_token"]),
                "credential_version": (grant.credential_version + 1) if grant else 1,
                "health": JiraCredentialHealth.HEALTHY,
                "last_verified_at": timezone.now(),
                "created_by": request.user,
            }
            if grant:
                for key, value in values.items():
                    setattr(grant, key, value)
                grant.save()
            else:
                grant = JiraUserGrant.objects.create(
                    site=site,
                    user=request.user,
                    **values,
                )
            JiraSyncEvent.objects.create(
                workspace=workspace,
                project=None,
                actor=request.user,
                authority_owner=request.user,
                action="personal_grant_connected",
                detail={"site": site.site_url, "account_id": account_id},
                external_id=f"jira-account:{account_id}",
            )
        return Response(
            {"connected": True, "grant": JiraGrantSerializer(grant).data},
            status=status.HTTP_201_CREATED if not existed else status.HTTP_200_OK,
        )

    def delete(self, request, slug):
        workspace = Workspace.objects.get(slug=slug)
        grant = _grant_for_user(request.user, workspace=workspace)
        if not grant:
            return Response({"error": "Jira is not connected for this user"}, status=status.HTTP_404_NOT_FOUND)
        now = timezone.now()
        JiraUserGrant.objects.filter(pk=grant.pk).update(deleted_at=now)
        JiraAutomationConsent.objects.filter(owner=request.user, revoked_at__isnull=True).update(revoked_at=now)
        JiraSyncEvent.objects.create(
            workspace=workspace,
            project=None,
            actor=request.user,
            authority_owner=request.user,
            action="personal_grant_disconnected",
            detail={"site": grant.site.site_url},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class JiraReadinessEndpoint(BaseAPIView):
    permission_classes = [WorkspaceMemberPermission]

    def get(self, request, slug):
        workspace = Workspace.objects.get(slug=slug)
        project_id = request.query_params.get("project_id")
        project = None
        if project_id:
            project = Project.objects.filter(id=project_id, workspace__slug=slug, deleted_at__isnull=True).first()
            if project is None:
                return Response({"error": "Project not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(_readiness(workspace, project, request.user))


class JiraProjectPolicyEndpoint(BaseAPIView):
    permission_classes = [ProjectAdminPermission]

    def get(self, request, slug, project_id):
        policy = JiraProjectPolicy.objects.filter(
            project__workspace__slug=slug,
            project_id=project_id,
            deleted_at__isnull=True,
        ).first()
        if not policy:
            return Response({"policy": None})
        return Response({"policy": JiraProjectPolicySerializer(policy).data})

    def put(self, request, slug, project_id):
        project = Project.objects.filter(
            id=project_id,
            workspace__slug=slug,
            deleted_at__isnull=True,
        ).first()
        if project is None:
            return Response({"error": "Project not found"}, status=status.HTTP_404_NOT_FOUND)
        site_id = request.data.get("site_id")
        site = None
        if site_id:
            site = JiraSiteRegistration.objects.filter(
                id=site_id,
                workspace=project.workspace,
                enabled=True,
                deleted_at__isnull=True,
            ).first()
        if site is None:
            grant = _grant_for_user(request.user, workspace=project.workspace)
            site = grant.site if grant else None
        if site is None:
            return Response(
                {
                    "error": "Connect a personal Jira account or provide an enabled site",
                    "code": "personal_grant_missing",
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        policy_input = JiraPolicyInput(
            jira_project_key=str(request.data.get("jira_project_key") or ""),
            jira_issue_type_id=str(request.data.get("jira_issue_type_id") or ""),
            direction=str(request.data.get("direction") or JiraSyncDirection.OFF),
            close_enabled=bool(request.data.get("close_enabled", False)),
            reopen_enabled=bool(request.data.get("reopen_enabled", False)),
            state_mappings=request.data.get("state_mappings") or {},
            board_id=str(request.data.get("board_id") or ""),
        )
        try:
            validate_policy_input(policy_input)
        except JiraIntegrationError as exc:
            return _integration_error(exc)
        state_ids = set(State.objects.filter(project=project, deleted_at__isnull=True).values_list("id", flat=True))
        for source_id, mapping in policy_input.state_mappings.items():
            if source_id not in {str(value) for value in state_ids}:
                return Response(
                    {"error": f"Plane state {source_id} does not belong to this project", "field": "state_mappings"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if mapping.get("plane_state_id") and str(mapping["plane_state_id"]) not in {
                str(value) for value in state_ids
            }:
                return Response(
                    {"error": f"Plane target state {mapping['plane_state_id']} does not belong to this project"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        expected_version = request.data.get("version")
        policy = JiraProjectPolicy.objects.filter(project=project, deleted_at__isnull=True).first()
        if policy and expected_version is not None and int(expected_version) != policy.version:
            return Response(
                {"error": "The Jira policy changed. Reload it before saving.", "code": "stale_policy"},
                status=status.HTTP_409_CONFLICT,
            )
        with transaction.atomic():
            if policy is None:
                policy = JiraProjectPolicy(project=project, workspace=project.workspace, site=site)
            else:
                policy.site = site
                policy.version += 1
            policy.jira_project_key = policy_input.jira_project_key.strip().upper()
            policy.jira_project_id = str(request.data.get("jira_project_id") or "")
            policy.jira_issue_type_id = policy_input.jira_issue_type_id
            policy.jira_issue_type_name = str(request.data.get("jira_issue_type_name") or "")
            policy.board_id = policy_input.board_id
            policy.direction = policy_input.direction
            policy.close_enabled = policy_input.close_enabled
            policy.reopen_enabled = policy_input.reopen_enabled
            policy.state_mappings = policy_input.state_mappings
            policy.template_version = str(request.data.get("template_version") or "jackrabbit-english-v1")
            policy.repository_base_ref = str(request.data.get("repository_base_ref") or "master")
            policy.repository_target_ref = str(request.data.get("repository_target_ref") or "staging")
            policy.branch_template = str(request.data.get("branch_template") or "{jira_key}-{slug}")
            policy.pr_title_template = str(request.data.get("pr_title_template") or "{jira_key}: {summary}")
            policy.enabled = bool(request.data.get("enabled", False)) and policy.direction != JiraSyncDirection.OFF
            policy.effective_at = timezone.now() if policy.enabled else policy.effective_at
            policy.save()
            JiraSyncEvent.objects.create(
                workspace=project.workspace,
                project=project,
                actor=request.user,
                authority_owner=request.user,
                action="project_policy_saved",
                detail={"version": policy.version, "enabled": policy.enabled, "direction": policy.direction},
            )
        return Response({"policy": JiraProjectPolicySerializer(policy).data})


class JiraIssueBindingEndpoint(BaseAPIView):
    permission_classes = [ProjectEntityPermission]

    def _issue(self, request, slug, project_id, issue_id):
        return (
            Issue.issue_objects.filter(
                id=issue_id,
                project_id=project_id,
                workspace__slug=slug,
            )
            .select_related("project", "state")
            .first()
        )

    def get(self, request, slug, project_id, issue_id):
        issue = self._issue(request, slug, project_id, issue_id)
        if issue is None:
            return Response({"error": "Issue not found"}, status=status.HTTP_404_NOT_FOUND)
        binding = (
            JiraIssueBinding.objects.filter(
                issue=issue,
                is_active=True,
                deleted_at__isnull=True,
            )
            .select_related("site")
            .first()
        )
        if binding is None:
            return Response({"linked": False})
        grant = _grant_for_user(request.user, site=binding.site)
        if not actor_can_use_grant(request.user, grant):
            return Response({"linked": True, "access_required": True, "sync_override": binding.sync_override})
        return Response({"linked": True, "binding": JiraIssueBindingSerializer(binding).data})

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id, issue_id):
        issue = self._issue(request, slug, project_id, issue_id)
        if issue is None:
            return Response({"error": "Issue not found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = JiraBindingInputSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        policy = (
            JiraProjectPolicy.objects.filter(
                project=issue.project,
                deleted_at__isnull=True,
            )
            .select_related("site")
            .first()
        )
        if policy is None:
            return Response({"error": "Configure the project Jira policy first", "code": "policy_missing"}, status=422)
        grant = _grant_for_user(request.user, site=policy.site, workspace=issue.project.workspace)
        if not actor_can_use_grant(request.user, grant):
            return Response(
                {"error": "Connect your Jira account before linking an issue", "code": "personal_grant_missing"},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            remote = JiraClient.from_grant(grant).get_issue(serializer.validated_data["issue_key"])
            remote_values = _parse_binding_remote(remote)
        except JiraApiError as exc:
            return Response({"error": exc.message, "code": exc.code}, status=exc.status_code or 502)
        if remote_values["jira_project_key"].upper() != policy.jira_project_key.upper():
            return Response(
                {
                    "error": "The Jira issue belongs to a different project than the project policy",
                    "code": "jira_project_mismatch",
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if (
            JiraIssueBinding.objects.filter(
                project=issue.project,
                site=policy.site,
                jira_issue_id=remote_values["jira_issue_id"],
                is_active=True,
                deleted_at__isnull=True,
            )
            .exclude(issue=issue)
            .exists()
        ):
            return Response(
                {"error": "That Jira issue is already linked to another Plane work item"},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            with transaction.atomic():
                binding, created = JiraIssueBinding.objects.get_or_create(
                    project=issue.project,
                    issue=issue,
                    is_active=True,
                    deleted_at__isnull=True,
                    defaults={
                        "workspace": issue.project.workspace,
                        "site": policy.site,
                        "sync_override": serializer.validated_data["sync_override"],
                        **remote_values,
                    },
                )
                if not created:
                    binding.site = policy.site
                    binding.sync_override = serializer.validated_data["sync_override"]
                    for key, value in remote_values.items():
                        setattr(binding, key, value)
                    binding.save()
                if created:
                    JiraSyncEvent.objects.create(
                        workspace=issue.project.workspace,
                        project=issue.project,
                        issue=issue,
                        binding=binding,
                        actor=request.user,
                        authority_owner=request.user,
                        action="jira_issue_linked",
                        detail={"jira_issue_key": remote_values["jira_issue_key"]},
                        external_id=remote_values["jira_issue_id"],
                    )
        except IntegrityError:
            return Response(
                {"error": "That Jira issue is already linked to another Plane work item"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            {"linked": True, "binding": JiraIssueBindingSerializer(binding).data},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def delete(self, request, slug, project_id, issue_id):
        issue = self._issue(request, slug, project_id, issue_id)
        if issue is None:
            return Response({"error": "Issue not found"}, status=status.HTTP_404_NOT_FOUND)
        binding = JiraIssueBinding.objects.filter(
            issue=issue,
            is_active=True,
            deleted_at__isnull=True,
        ).first()
        if binding is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        binding.is_active = False
        binding.generation += 1
        binding.save(update_fields=["is_active", "generation", "updated_at"])
        JiraSyncEvent.objects.create(
            workspace=issue.project.workspace,
            project=issue.project,
            issue=issue,
            binding=binding,
            actor=request.user,
            authority_owner=request.user,
            action="jira_issue_unlinked",
            detail={"jira_issue_key": binding.jira_issue_key},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class JiraIssueConsentEndpoint(BaseAPIView):
    permission_classes = [ProjectEntityPermission]

    def _binding(self, slug, project_id, issue_id):
        return (
            JiraIssueBinding.objects.filter(
                issue_id=issue_id,
                project_id=project_id,
                workspace__slug=slug,
                is_active=True,
                deleted_at__isnull=True,
            )
            .select_related("site", "issue", "project")
            .first()
        )

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def get(self, request, slug, project_id, issue_id):
        binding = self._binding(slug, project_id, issue_id)
        if binding is None:
            return Response({"error": "Jira issue binding not found"}, status=status.HTTP_404_NOT_FOUND)
        consent = JiraAutomationConsent.objects.filter(
            binding=binding,
            owner=request.user,
            revoked_at__isnull=True,
            deleted_at__isnull=True,
        ).first()
        return Response(
            {
                "binding_id": str(binding.id),
                "automation_owner": str(binding.automation_owner_id) if binding.automation_owner_id else None,
                "consent": JiraConsentSerializer(consent).data if consent else None,
            }
        )

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id, issue_id):
        binding = self._binding(slug, project_id, issue_id)
        if binding is None:
            return Response({"error": "Jira issue binding not found"}, status=status.HTTP_404_NOT_FOUND)
        policy = JiraProjectPolicy.objects.filter(
            project=binding.project,
            deleted_at__isnull=True,
        ).first()
        if policy is None:
            return Response({"error": "Project Jira policy is missing", "code": "policy_missing"}, status=422)
        grant = _grant_for_user(request.user, site=binding.site, workspace=binding.project.workspace)
        if not actor_can_use_grant(request.user, grant):
            return Response({"error": "Connect your Jira account first", "code": "personal_grant_missing"}, status=403)
        serializer = JiraConsentInputSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        scope = {
            "direction": effective_direction(binding, policy),
            "state_mappings": policy.state_mappings,
            "close_enabled": policy.close_enabled,
            "reopen_enabled": policy.reopen_enabled,
            "jira_project_key": policy.jira_project_key,
            "jira_issue_type_id": policy.jira_issue_type_id,
            "allowed_events": serializer.validated_data.get("allowed_events", []),
        }
        scope_hash = compute_automation_scope_hash(scope)
        with transaction.atomic():
            binding.automation_owner = request.user
            binding.automation_scope = scope
            binding.consent_version += 1
            binding.save(update_fields=["automation_owner", "automation_scope", "consent_version", "updated_at"])
            JiraAutomationConsent.objects.filter(
                binding=binding,
                owner=request.user,
                revoked_at__isnull=True,
            ).update(revoked_at=timezone.now())
            consent = JiraAutomationConsent.objects.create(
                binding=binding,
                owner=request.user,
                scope_hash=scope_hash,
                scope=scope,
                version=binding.consent_version,
                created_by=request.user,
            )
            JiraSyncEvent.objects.create(
                workspace=binding.project.workspace,
                project=binding.project,
                issue=binding.issue,
                binding=binding,
                actor=request.user,
                authority_owner=request.user,
                action="automation_consent_granted",
                detail={"scope_hash": scope_hash, "version": consent.version},
            )
        return Response(JiraConsentSerializer(consent).data, status=status.HTTP_201_CREATED)

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def delete(self, request, slug, project_id, issue_id):
        binding = self._binding(slug, project_id, issue_id)
        if binding is None:
            return Response({"error": "Jira issue binding not found"}, status=status.HTTP_404_NOT_FOUND)
        now = timezone.now()
        JiraAutomationConsent.objects.filter(
            binding=binding,
            owner=request.user,
            revoked_at__isnull=True,
        ).update(revoked_at=now)
        if binding.automation_owner_id == request.user.id:
            binding.automation_owner = None
            binding.consent_version += 1
            binding.save(update_fields=["automation_owner", "consent_version", "updated_at"])
        JiraSyncEvent.objects.create(
            workspace=binding.project.workspace,
            project=binding.project,
            issue=binding.issue,
            binding=binding,
            actor=request.user,
            authority_owner=request.user,
            action="automation_consent_revoked",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class JiraDraftEndpoint(BaseAPIView):
    permission_classes = [ProjectEntityPermission]

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id, issue_id):
        issue = (
            Issue.issue_objects.filter(
                id=issue_id,
                project_id=project_id,
                workspace__slug=slug,
            )
            .select_related("project")
            .first()
        )
        if issue is None:
            return Response({"error": "Issue not found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = JiraDraftInputSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        policy = JiraProjectPolicy.objects.filter(project=issue.project, deleted_at__isnull=True).first()
        if policy is None:
            return Response({"error": "Configure the project Jira policy first", "code": "policy_missing"}, status=422)
        source_revision = hashlib.sha256(
            f"{issue.name}:{issue.description_html}:{issue.updated_at.isoformat()}".encode("utf-8")
        ).hexdigest()
        summary = serializer.validated_data.get("summary") or issue.name
        description_html = serializer.validated_data.get("description_markdown") or issue.description_html
        draft_data = build_english_draft(
            summary=summary,
            description_html=description_html,
            extra_context=serializer.validated_data.get("extra_context", ""),
            source_revision=source_revision,
        )
        fields = serializer.validated_data.get("fields") or {}
        if not isinstance(fields, dict):
            return Response({"error": "fields must be an object"}, status=status.HTTP_400_BAD_REQUEST)
        draft = JiraDraft.objects.create(
            project=issue.project,
            workspace=issue.project.workspace,
            issue=issue,
            policy=policy,
            summary=draft_data.summary,
            description_markdown=draft_data.description_markdown,
            description_adf=markdown_to_adf(draft_data.description_markdown),
            fields=fields,
            source_revision=source_revision,
            schema_version=policy.jira_issue_type_id,
            template_version=policy.template_version,
            validation={"items": draft_data.validation, "valid": draft_data.is_valid},
            approval_hash=draft_data.approval_hash,
            created_by=request.user,
        )
        return Response(JiraDraftSerializer(draft).data, status=status.HTTP_201_CREATED)


class JiraDraftApproveEndpoint(BaseAPIView):
    permission_classes = [ProjectEntityPermission]

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id, draft_id):
        draft = JiraDraft.objects.filter(
            id=draft_id,
            project_id=project_id,
            workspace__slug=slug,
            deleted_at__isnull=True,
        ).first()
        if draft is None:
            return Response({"error": "Draft not found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = JiraDraftApproveSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        if serializer.validated_data["approval_hash"] != draft.approval_hash:
            return Response({"error": "Draft changed after review", "code": "stale_draft"}, status=409)
        if draft.validation.get("valid") is False:
            return Response({"error": "Draft has validation errors", "code": "draft_invalid"}, status=422)
        draft.approved_by = request.user
        draft.approved_at = timezone.now()
        draft.expires_at = timezone.now() + timezone.timedelta(minutes=30)
        draft.save(update_fields=["approved_by", "approved_at", "expires_at", "updated_at"])
        return Response(JiraDraftSerializer(draft).data)


class JiraDraftSubmitEndpoint(BaseAPIView):
    permission_classes = [ProjectEntityPermission]

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id, draft_id):
        draft = (
            JiraDraft.objects.filter(
                id=draft_id,
                project_id=project_id,
                workspace__slug=slug,
                deleted_at__isnull=True,
            )
            .select_related("issue", "policy", "project")
            .first()
        )
        if draft is None:
            return Response({"error": "Draft not found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = JiraDraftSubmitSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        if not draft.approved_by_id or draft.approved_by_id != request.user.id:
            return Response(
                {"error": "Approve the draft yourself before submitting", "code": "approval_required"},
                status=403,
            )
        if draft.expires_at and draft.expires_at < timezone.now():
            return Response({"error": "Draft approval expired", "code": "approval_expired"}, status=409)
        if serializer.validated_data["approval_hash"] != draft.approval_hash:
            return Response({"error": "Draft changed after review", "code": "stale_draft"}, status=409)
        policy = draft.policy
        if not policy.enabled or policy.direction == JiraSyncDirection.OFF:
            return Response(
                {"error": "Enable the project Jira policy before creating issues", "code": "policy_disabled"},
                status=422,
            )
        grant = _grant_for_user(request.user, site=policy.site, workspace=draft.project.workspace)
        if not actor_can_use_grant(request.user, grant):
            return Response({"error": "Reconnect your Jira account", "code": "personal_grant_missing"}, status=403)
        fields = dict(draft.fields or {})
        fields.update(serializer.validated_data.get("fields") or {})
        for protected_key in ("project", "issuetype", "summary", "description"):
            fields.pop(protected_key, None)
        fields.update(
            {
                "project": {"key": policy.jira_project_key},
                "issuetype": {"id": policy.jira_issue_type_id},
                "summary": draft.summary,
                "description": draft.description_adf,
            }
        )
        payload = {
            "draft_id": str(draft.id),
            "approval_hash": serializer.validated_data["approval_hash"],
            "fields": fields,
        }
        try:
            operation, created = create_operation(
                project=draft.project,
                issue=draft.issue,
                kind=JiraOperationKind.CREATE_ISSUE,
                idempotency_key=serializer.validated_data["idempotency_key"],
                payload=payload,
                actor=request.user,
                credential_owner=request.user,
                consent_version=0,
            )
        except JiraIntegrationError as exc:
            return _integration_error(exc)
        if not created:
            return Response(JiraOperationSerializer(operation).data, status=status.HTTP_200_OK)
        client = JiraClient.from_grant(grant)
        try:
            operation.state = JiraOperationState.DISPATCHING
            operation.attempts += 1
            operation.save(update_fields=["state", "attempts", "updated_at"])
            _require_create_metadata(client, policy, fields)
            remote = client.create_issue(fields)
            readback = client.get_issue(remote.get("key"))
            remote_values = _parse_binding_remote(readback)
            operation.state = JiraOperationState.VERIFYING
            operation.save(update_fields=["state", "updated_at"])
            with transaction.atomic():
                binding, _ = JiraIssueBinding.objects.get_or_create(
                    project=draft.project,
                    issue=draft.issue,
                    is_active=True,
                    deleted_at__isnull=True,
                    defaults={
                        "workspace": draft.project.workspace,
                        "site": policy.site,
                        **remote_values,
                    },
                )
                for key, value in remote_values.items():
                    setattr(binding, key, value)
                binding.last_verified_at = timezone.now()
                binding.save()
                complete_operation(
                    operation,
                    {
                        "jira_issue_id": remote_values["jira_issue_id"],
                        "jira_issue_key": remote_values["jira_issue_key"],
                        "binding_id": str(binding.id),
                    },
                )
            return Response(JiraOperationSerializer(operation).data, status=status.HTTP_201_CREATED)
        except JiraApiError as exc:
            if exc.code in {"jira_transport_error", "jira_invalid_response"}:
                mark_operation_unknown(operation, code=exc.code, message=exc.message)
                return Response(JiraOperationSerializer(operation).data, status=status.HTTP_202_ACCEPTED)
            operation.state = (
                JiraOperationState.BLOCKED_AUTH if exc.code == "jira_auth_error" else JiraOperationState.FAILED
            )
            operation.error_code = exc.code
            operation.error_message = exc.message[:2000]
            operation.save(update_fields=["state", "error_code", "error_message", "updated_at"])
            return Response(JiraOperationSerializer(operation).data, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except JiraIntegrationError as exc:
            operation.state = JiraOperationState.BLOCKED_POLICY
            operation.error_code = exc.code
            operation.error_message = str(exc)
            operation.save(update_fields=["state", "error_code", "error_message", "updated_at"])
            return Response(JiraOperationSerializer(operation).data, status=status.HTTP_422_UNPROCESSABLE_ENTITY)


class JiraOperationEndpoint(BaseAPIView):
    permission_classes = [ProjectEntityPermission]

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def get(self, request, slug, project_id, operation_id=None):
        query = JiraOperation.objects.filter(project_id=project_id, workspace__slug=slug)
        if operation_id:
            operation = query.filter(id=operation_id).first()
            if operation is None:
                return Response({"error": "Operation not found"}, status=status.HTTP_404_NOT_FOUND)
            return Response(JiraOperationSerializer(operation).data)
        issue_id = request.query_params.get("issue_id")
        if issue_id:
            query = query.filter(issue_id=issue_id)
        return Response(JiraOperationSerializer(query[:100], many=True).data)
