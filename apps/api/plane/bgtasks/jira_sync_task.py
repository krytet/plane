# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party imports
from celery import shared_task

# Module imports
from plane.db.models import JiraIssueBinding, JiraOperation
from plane.utils.jira_integration.sync import (
    dispatch_operation,
    poll_binding,
    process_pending_operations,
)


@shared_task
def dispatch_jira_operation(operation_id):
    operation = (
        JiraOperation.objects.filter(id=operation_id)
        .select_related(
            "binding",
            "binding__site",
            "binding__issue",
            "project",
            "actor",
            "credential_owner",
        )
        .first()
    )
    if operation:
        return dispatch_operation(operation).state
    return "not_found"


@shared_task
def process_jira_operations(limit=50):
    return [operation.state for operation in process_pending_operations(limit=limit)]


@shared_task
def poll_jira_binding(binding_id):
    binding = (
        JiraIssueBinding.objects.filter(id=binding_id, is_active=True, deleted_at__isnull=True)
        .select_related(
            "site",
            "project",
            "issue",
            "automation_owner",
        )
        .first()
    )
    return poll_binding(binding) if binding else {"applied": False, "reason": "binding_missing"}
