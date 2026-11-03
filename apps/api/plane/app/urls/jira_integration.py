# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.urls import path

# Module imports
from plane.app.views import (
    JiraDraftApproveEndpoint,
    JiraDraftEndpoint,
    JiraDraftSubmitEndpoint,
    JiraDevelopmentPlanEndpoint,
    JiraIssueBindingEndpoint,
    JiraIssueConsentEndpoint,
    JiraOperationEndpoint,
    JiraPersonalConnectionEndpoint,
    JiraProjectPolicyEndpoint,
    JiraReadinessEndpoint,
)

urlpatterns = [
    path(
        "workspaces/<str:slug>/integrations/jira/me/",
        JiraPersonalConnectionEndpoint.as_view(),
        name="jira-personal-connection",
    ),
    path(
        "workspaces/<str:slug>/integrations/jira/readiness/",
        JiraReadinessEndpoint.as_view(),
        name="jira-readiness",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/integrations/jira/policy/",
        JiraProjectPolicyEndpoint.as_view(),
        name="jira-project-policy",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/issues/<uuid:issue_id>/jira/",
        JiraIssueBindingEndpoint.as_view(),
        name="jira-issue-binding",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/issues/<uuid:issue_id>/jira/drafts/",
        JiraDraftEndpoint.as_view(),
        name="jira-draft",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/issues/<uuid:issue_id>/jira/consent/",
        JiraIssueConsentEndpoint.as_view(),
        name="jira-issue-consent",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/jira/drafts/<uuid:draft_id>/approve/",
        JiraDraftApproveEndpoint.as_view(),
        name="jira-draft-approve",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/jira/drafts/<uuid:draft_id>/submit/",
        JiraDraftSubmitEndpoint.as_view(),
        name="jira-draft-submit",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/jira/operations/",
        JiraOperationEndpoint.as_view(),
        name="jira-operations",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/issues/<uuid:issue_id>/jira/development/",
        JiraDevelopmentPlanEndpoint.as_view(),
        name="jira-development-plan",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/jira/operations/<uuid:operation_id>/",
        JiraOperationEndpoint.as_view(),
        name="jira-operation",
    ),
]
