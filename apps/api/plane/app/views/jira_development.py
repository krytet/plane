# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.permissions import ROLE, ProjectEntityPermission, allow_permission
from plane.db.models import Issue, JiraIssueBinding, JiraProjectPolicy
from plane.utils.jira_integration.development import build_development_plan

from .base import BaseAPIView


class JiraDevelopmentPlanEndpoint(BaseAPIView):
    permission_classes = [ProjectEntityPermission]

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def get(self, request, slug, project_id, issue_id):
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
            return Response({"error": "Link a Jira issue before preparing development work"}, status=409)
        policy = JiraProjectPolicy.objects.filter(project=issue.project, deleted_at__isnull=True).first()
        if policy is None:
            return Response({"error": "Project Jira policy is missing"}, status=422)
        return Response(build_development_plan(issue=issue, binding=binding, policy=policy))

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id, issue_id):
        # A plan is not an execution receipt. The existing Jackrabbit adapter
        # consumes the typed plan after its own repository/review gates.
        return Response(
            {
                "error": "No development executor is configured for this workspace",
                "code": "development_executor_not_configured",
            },
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )
