# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import hashlib
import uuid

# Django imports
from django.db import IntegrityError, transaction

# Module imports
from plane.db.models import (
    JiraOperation,
    JiraOperationState,
    JiraSyncEvent,
)

from .errors import JiraIntegrationError
from .policy import canonical_json


def operation_payload_hash(payload):
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def create_operation(
    *,
    project,
    issue,
    kind,
    idempotency_key,
    payload,
    actor,
    credential_owner,
    binding=None,
    consent_version=0,
    origin="human",
):
    if not idempotency_key or len(idempotency_key) > 200:
        raise JiraIntegrationError("A valid Idempotency-Key is required", field="idempotency_key")
    digest = operation_payload_hash(payload)
    correlation_id = uuid.uuid4().hex
    try:
        with transaction.atomic():
            operation, created = JiraOperation.objects.get_or_create(
                project=project,
                issue=issue,
                kind=kind,
                idempotency_key=idempotency_key,
                defaults={
                    "workspace": project.workspace,
                    "binding": binding,
                    "payload_hash": digest,
                    "payload": payload,
                    "actor": actor,
                    "credential_owner": credential_owner,
                    "consent_version": consent_version,
                    "origin": origin,
                    "correlation_id": correlation_id,
                    "state": JiraOperationState.QUEUED,
                },
            )
            if not created and operation.payload_hash != digest:
                raise JiraIntegrationError(
                    "The idempotency key was already used with different content",
                    field="idempotency_key",
                )
    except IntegrityError:
        operation = JiraOperation.objects.get(
            project=project,
            issue=issue,
            kind=kind,
            idempotency_key=idempotency_key,
        )
        if operation.payload_hash != digest:
            raise JiraIntegrationError("The idempotency key was already used with different content")
        created = False
    if created:
        JiraSyncEvent.objects.create(
            project=project,
            workspace=project.workspace,
            issue=issue,
            binding=binding,
            operation=operation,
            actor=actor,
            authority_owner=credential_owner,
            action="operation_queued",
            detail={"kind": kind, "state": operation.state},
        )
    return operation, created


def mark_operation_unknown(operation, *, code, message):
    operation.state = JiraOperationState.OUTCOME_UNKNOWN
    operation.error_code = code
    operation.error_message = message[:2000]
    operation.next_attempt_at = None
    operation.save(update_fields=["state", "error_code", "error_message", "next_attempt_at", "updated_at"])
    JiraSyncEvent.objects.create(
        project=operation.project,
        workspace=operation.project.workspace,
        issue=operation.issue,
        binding=operation.binding,
        operation=operation,
        actor=operation.actor,
        authority_owner=operation.credential_owner,
        action="operation_outcome_unknown",
        detail={"code": code},
    )


def complete_operation(operation, result):
    operation.state = JiraOperationState.SUCCEEDED
    operation.result = result
    operation.error_code = ""
    operation.error_message = ""
    operation.save(update_fields=["state", "result", "error_code", "error_message", "updated_at"])
    JiraSyncEvent.objects.create(
        project=operation.project,
        workspace=operation.project.workspace,
        issue=operation.issue,
        binding=operation.binding,
        operation=operation,
        actor=operation.actor,
        authority_owner=operation.credential_owner,
        action="operation_verified",
        detail={"kind": operation.kind, "result": result},
    )
