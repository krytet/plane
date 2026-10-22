# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Sequence
from uuid import UUID, uuid4

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from plane.db.models import FileAsset, Issue, Project, ProjectMember, State, StateGroup, User, WorkspaceMember
from plane.db.models.project import ROLE
from plane.utils.llm_complete_report import PDF_MIME_TYPE, PdfArtifact, render_llm_complete_pdf
from plane.utils.path_validator import sanitize_filename


LLM_COMPLETE_STATE_NAME = "LLM Complete"
LLM_COMPLETE_STATE_COLOR = "#F59E0B"
LLM_COMPLETE_EXTERNAL_SOURCE = "plane-llm-complete"
_ISSUE_STATE_UPDATE_FIELDS = ["state", "completed_at", "updated_at", "updated_by"]


class LlmCompleteError(Exception):
    code = "llm_complete_error"

    def __init__(self, message: str, *, asset_id: UUID | None = None):
        super().__init__(message)
        self.message = message
        self.asset_id = asset_id


class LlmCompleteScopeError(LlmCompleteError):
    code = "scope_denied"


class LlmCompleteConflict(LlmCompleteError):
    code = "conflict"


class LlmCompleteReconciliationRequired(LlmCompleteError):
    code = "reconciliation_required"


@dataclass(frozen=True)
class LlmCompleteScope:
    actor: User
    project: Project
    issue: Issue


@dataclass(frozen=True)
class LlmCompleteResult:
    scope: LlmCompleteScope
    artifact: PdfArtifact
    attachment: FileAsset | None
    state: State | None
    dry_run: bool
    attachment_reused: bool = False
    state_created: bool = False
    previous_state_id: UUID | None = None


def _has_finalize_permission(workspace_member, project_member) -> bool:
    return bool(
        workspace_member
        and getattr(workspace_member, "is_active", False)
        and workspace_member.role in {ROLE.ADMIN.value, ROLE.MEMBER.value}
        and project_member
        and getattr(project_member, "is_active", False)
        and project_member.role in {ROLE.ADMIN.value, ROLE.MEMBER.value}
    )


def resolve_llm_complete_scope(
    *,
    workspace_slug: str,
    project_id: UUID,
    issue_id: UUID,
    actor_id: UUID,
) -> LlmCompleteScope:
    actor = User.objects.filter(pk=actor_id, is_active=True).first()
    if actor is None:
        raise LlmCompleteScopeError("The operator is not an active user.")

    project = (
        Project.objects.filter(
            pk=project_id,
            workspace__slug=workspace_slug,
            archived_at__isnull=True,
        )
        .select_related("workspace")
        .first()
    )
    if project is None:
        raise LlmCompleteScopeError("The project was not found in the requested workspace.")

    workspace_member = WorkspaceMember.objects.filter(
        workspace_id=project.workspace_id,
        member_id=actor.id,
        is_active=True,
    ).first()
    project_member = ProjectMember.objects.filter(
        workspace_id=project.workspace_id,
        project_id=project.id,
        member_id=actor.id,
        is_active=True,
    ).first()
    if not _has_finalize_permission(workspace_member, project_member):
        raise LlmCompleteScopeError("The operator must be an active project admin or member.")

    issue = (
        Issue.issue_objects.filter(
            pk=issue_id,
            workspace_id=project.workspace_id,
            project_id=project.id,
        )
        .select_related("workspace", "project", "state")
        .first()
    )
    if issue is None:
        raise LlmCompleteScopeError("The work item was not found in the requested project.")
    return LlmCompleteScope(actor=actor, project=project, issue=issue)


def _assert_started_state(state: State | None) -> None:
    if state is not None and state.group != StateGroup.STARTED.value:
        raise LlmCompleteConflict(
            f"The existing {LLM_COMPLETE_STATE_NAME!r} state must use the STARTED group, not {state.group!r}."
        )


def _find_llm_complete_state(project: Project, *, for_update: bool = False) -> State | None:
    queryset = State.all_state_objects.filter(
        project_id=project.id,
        name__iexact=LLM_COMPLETE_STATE_NAME,
        deleted_at__isnull=True,
    )
    if for_update:
        queryset = queryset.select_for_update()
    states = list(queryset[:2])
    if len(states) > 1:
        raise LlmCompleteConflict(
            f"Multiple states match {LLM_COMPLETE_STATE_NAME!r}; resolve the duplicate before finalizing."
        )
    state = states[0] if states else None
    _assert_started_state(state)
    return state


def _report_filename(scope: LlmCompleteScope) -> str:
    return (
        sanitize_filename(f"LLM-Complete-{scope.project.identifier}-{scope.issue.sequence_id}.pdf")
        or "LLM-Complete.pdf"
    )


def _attachment_external_id(scope: LlmCompleteScope, artifact: PdfArtifact) -> str:
    return f"{scope.issue.id}:{artifact.sha256}"


def _attachment_values(scope: LlmCompleteScope, artifact: PdfArtifact, asset_key: str) -> dict:
    filename = _report_filename(scope)
    return {
        "attributes": {
            "name": filename,
            "type": PDF_MIME_TYPE,
            "size": artifact.length,
            "sha256": artifact.sha256,
        },
        "asset": asset_key,
        "workspace_id": scope.issue.workspace_id,
        "project_id": scope.issue.project_id,
        "issue_id": scope.issue.id,
        "entity_type": FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
        "external_source": LLM_COMPLETE_EXTERNAL_SOURCE,
        "external_id": _attachment_external_id(scope, artifact),
        "size": artifact.length,
        "is_uploaded": False,
        "created_by_id": scope.actor.id,
        "storage_metadata": {
            "sha256": artifact.sha256,
            "content_length": artifact.length,
            "content_type": PDF_MIME_TYPE,
            "status": "reserved",
            "initial_state_id": str(scope.issue.state_id) if scope.issue.state_id else None,
        },
    }


def _attachment_candidates(scope: LlmCompleteScope, artifact: PdfArtifact) -> list[FileAsset]:
    return list(
        FileAsset.objects.filter(
            workspace_id=scope.issue.workspace_id,
            project_id=scope.issue.project_id,
            issue_id=scope.issue.id,
            entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
            external_source=LLM_COMPLETE_EXTERNAL_SOURCE,
            external_id=_attachment_external_id(scope, artifact),
            is_deleted=False,
            deleted_at__isnull=True,
        ).order_by("created_at")[:2]
    )


def _reserve_or_reuse_attachment(
    scope: LlmCompleteScope,
    artifact: PdfArtifact,
) -> tuple[FileAsset, bool]:
    with transaction.atomic():
        locked_issue = (
            Issue.issue_objects.select_for_update(of=("self",))
            .filter(
                pk=scope.issue.id,
                workspace_id=scope.issue.workspace_id,
                project_id=scope.issue.project_id,
            )
            .only("id")
            .first()
        )
        if locked_issue is None:
            raise LlmCompleteScopeError("The work item is no longer available in the requested project.")

        candidates = _attachment_candidates(scope, artifact)
        if len(candidates) > 1:
            raise LlmCompleteReconciliationRequired(
                "Multiple reserved PDF attachments match this exact report; reconcile them before retrying."
            )
        if candidates:
            return candidates[0], True

        asset_key = f"{scope.issue.workspace_id}/{uuid4().hex}-{_report_filename(scope)}"
        try:
            with transaction.atomic():
                attachment = FileAsset(**_attachment_values(scope, artifact, asset_key))
                attachment.save(created_by_id=scope.actor.id)
        except Exception as exc:
            try:
                reconciled = _attachment_candidates(scope, artifact)
            except Exception:
                reconciled = []
            if len(reconciled) == 1:
                return reconciled[0], True
            raise LlmCompleteReconciliationRequired(
                "The FileAsset reservation may have been written. Inspect the issue's "
                f"external id {_attachment_external_id(scope, artifact)!r} before retrying."
            ) from exc
        return attachment, False


def _read_stored_pdf(attachment: FileAsset, artifact: PdfArtifact) -> bool:
    if attachment.is_deleted or not attachment.asset:
        return False
    try:
        storage = attachment.asset.storage
        if hasattr(storage, "s3_client") and hasattr(storage, "aws_storage_bucket_name"):
            response = storage.s3_client.get_object(
                Bucket=storage.aws_storage_bucket_name,
                Key=attachment.asset.name,
            )
            with response["Body"] as stored:
                content = stored.read(artifact.length + 1)
        else:
            with storage.open(attachment.asset.name, "rb") as stored:
                content = stored.read(artifact.length + 1)
    except Exception:
        return False
    return artifact.matches(content)


def _mark_attachment_uploaded(
    attachment: FileAsset,
    artifact: PdfArtifact,
    stored_name: str,
) -> None:
    attributes = dict(attachment.attributes or {})
    attributes.update(
        {
            "name": _report_filename_from_attributes(attachment),
            "type": PDF_MIME_TYPE,
            "size": artifact.length,
            "sha256": artifact.sha256,
            "readback_verified": True,
        }
    )
    metadata = {
        "sha256": artifact.sha256,
        "content_length": artifact.length,
        "content_type": PDF_MIME_TYPE,
        "status": "verified",
        "verified_at": timezone.now().isoformat(),
        "initial_state_id": (attachment.storage_metadata or {}).get("initial_state_id"),
    }
    try:
        updated = FileAsset.objects.filter(
            pk=attachment.pk,
            workspace_id=attachment.workspace_id,
            project_id=attachment.project_id,
            issue_id=attachment.issue_id,
        ).update(
            asset=stored_name,
            attributes=attributes,
            size=artifact.length,
            is_uploaded=True,
            storage_metadata=metadata,
            updated_at=timezone.now(),
        )
    except Exception as exc:
        try:
            current = FileAsset.all_objects.filter(pk=attachment.pk).first()
        except Exception:
            current = None
        if (
            current is not None
            and current.is_uploaded
            and current.asset.name == stored_name
            and (current.storage_metadata or {}).get("sha256") == artifact.sha256
        ):
            attachment.asset.name = current.asset.name
            attachment.attributes = current.attributes
            attachment.size = current.size
            attachment.is_uploaded = current.is_uploaded
            attachment.storage_metadata = current.storage_metadata
            return
        raise LlmCompleteReconciliationRequired(
            "The PDF readback succeeded, but updating its FileAsset record was uncertain. "
            "Reconcile the attachment before changing state.",
            asset_id=attachment.id,
        ) from exc

    if updated != 1:
        current = FileAsset.all_objects.filter(pk=attachment.pk).first()
        if (
            current is None
            or not current.is_uploaded
            or current.asset.name != stored_name
            or (current.storage_metadata or {}).get("sha256") != artifact.sha256
        ):
            raise LlmCompleteReconciliationRequired(
                "The FileAsset readback could not be committed durably.",
                asset_id=attachment.id,
            )

    attachment.asset.name = stored_name
    attachment.attributes = attributes
    attachment.size = artifact.length
    attachment.is_uploaded = True
    attachment.storage_metadata = metadata


def _report_filename_from_attributes(attachment: FileAsset) -> str:
    name = (attachment.attributes or {}).get("name")
    if isinstance(name, str) and name:
        return name
    return f"LLM-Complete-{attachment.id}.pdf"


def _store_or_reuse_attachment(
    scope: LlmCompleteScope,
    artifact: PdfArtifact,
) -> tuple[FileAsset, bool]:
    attachment, reused = _reserve_or_reuse_attachment(scope, artifact)
    storage = attachment.asset.storage
    stored_name = attachment.asset.name

    if reused:
        if not _read_stored_pdf(attachment, artifact):
            raise LlmCompleteReconciliationRequired(
                "A prior FileAsset reservation exists but its stored bytes do not match this PDF. "
                "Reconcile it before retrying; no upload was retried.",
                asset_id=attachment.id,
            )
        _mark_attachment_uploaded(attachment, artifact, stored_name)
        return attachment, True

    try:
        if hasattr(storage, "upload_file"):
            uploaded = storage.upload_file(BytesIO(artifact.data), stored_name, content_type=PDF_MIME_TYPE)
            if not uploaded:
                raise OSError("Storage did not confirm the PDF upload.")
            saved_name = stored_name
        else:
            saved_name = storage.save(stored_name, ContentFile(artifact.data))
    except Exception as exc:
        if _read_stored_pdf(attachment, artifact):
            _mark_attachment_uploaded(attachment, artifact, stored_name)
            return attachment, False
        raise LlmCompleteReconciliationRequired(
            "The PDF upload result is uncertain. Inspect the reserved FileAsset and storage object; "
            "the upload was not retried.",
            asset_id=attachment.id,
        ) from exc

    if not saved_name:
        raise LlmCompleteReconciliationRequired(
            "Storage did not return an object name after upload. Reconcile the reserved FileAsset.",
            asset_id=attachment.id,
        )
    stored_name = str(saved_name)
    attachment.asset.name = stored_name
    if not _read_stored_pdf(attachment, artifact):
        raise LlmCompleteReconciliationRequired(
            "Stored PDF bytes failed the hash, length, or %PDF- readback check. "
            "The upload was not retried and issue state was not changed.",
            asset_id=attachment.id,
        )

    _mark_attachment_uploaded(attachment, artifact, stored_name)
    return attachment, False


def _apply_llm_complete_state(
    scope: LlmCompleteScope,
    attachment: FileAsset,
    artifact: PdfArtifact,
) -> tuple[State, bool, UUID | None]:
    with transaction.atomic():
        project = Project.objects.select_for_update().filter(pk=scope.project.id).first()
        if project is None:
            raise LlmCompleteScopeError("The project is no longer available.")

        actor_is_active = User.objects.filter(pk=scope.actor.id, is_active=True).exists()
        workspace_member = WorkspaceMember.objects.filter(
            workspace_id=project.workspace_id,
            member_id=scope.actor.id,
            is_active=True,
        ).first()
        project_member = ProjectMember.objects.filter(
            workspace_id=project.workspace_id,
            project_id=project.id,
            member_id=scope.actor.id,
            is_active=True,
        ).first()
        if not actor_is_active or not _has_finalize_permission(workspace_member, project_member):
            raise LlmCompleteScopeError("The operator no longer has permission to finalize this project.")

        issue = (
            Issue.issue_objects.select_for_update(of=("self",))
            .filter(
                pk=scope.issue.id,
                workspace_id=project.workspace_id,
                project_id=project.id,
            )
            .select_related("state")
            .first()
        )
        if issue is None:
            raise LlmCompleteScopeError("The work item is no longer available in the requested project.")
        if issue.state_id != scope.issue.state_id:
            raise LlmCompleteConflict(
                "The work item state changed while the PDF was being prepared; no state was overwritten."
            )

        confirmed_asset = (
            FileAsset.objects.select_for_update(of=("self",))
            .filter(
                pk=attachment.pk,
                workspace_id=issue.workspace_id,
                project_id=issue.project_id,
                issue_id=issue.id,
                entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
                is_uploaded=True,
                is_deleted=False,
            )
            .first()
        )
        if (
            confirmed_asset is None
            or (confirmed_asset.storage_metadata or {}).get("sha256") != artifact.sha256
            or confirmed_asset.size != artifact.length
        ):
            raise LlmCompleteConflict("The verified PDF attachment changed before the state transition.")

        previous_state_id = issue.state_id
        previous_description_html = issue.description_html
        previous_description_json = issue.description_json

        state = _find_llm_complete_state(project, for_update=True)
        if state is None:
            raise LlmCompleteConflict(
                f"Create the project state {LLM_COMPLETE_STATE_NAME!r} as an admin before finalizing."
            )

        issue.state = state
        issue.completed_at = None
        issue.updated_by_id = scope.actor.id
        issue.save(update_fields=_ISSUE_STATE_UPDATE_FIELDS, disable_auto_set_user=True)
        issue.refresh_from_db(fields=["state_id", "description_html", "description_json", "completed_at"])
        if (
            issue.state_id != state.id
            or issue.completed_at is not None
            or issue.description_html != previous_description_html
            or issue.description_json != previous_description_json
        ):
            raise LlmCompleteConflict("The final state update did not preserve the expected state or description.")

        from plane.utils.issue_chat_workflow import record_verified_issue_chat_completion

        record_verified_issue_chat_completion(
            issue=issue,
            state=state,
            previous_state_id=previous_state_id,
            verified_at=timezone.now(),
        )

    return state, False, previous_state_id


def finalize_llm_complete(
    *,
    workspace_slug: str,
    project_id: UUID,
    issue_id: UUID,
    actor_id: UUID,
    screenshot_paths: Sequence[str | Path],
    task_description: str,
    progress: str,
    dry_run: bool = False,
) -> LlmCompleteResult:
    """Generate, upload, verify, and then move one work item to LLM Complete."""

    scope = resolve_llm_complete_scope(
        workspace_slug=workspace_slug,
        project_id=project_id,
        issue_id=issue_id,
        actor_id=actor_id,
    )
    existing_state = _find_llm_complete_state(scope.project)
    if existing_state is None:
        raise LlmCompleteConflict(
            f"Create the project state {LLM_COMPLETE_STATE_NAME!r} as an admin before finalizing."
        )
    if scope.issue.state and scope.issue.state.group == StateGroup.COMPLETED.value:
        raise LlmCompleteConflict("A human-completed work item cannot be moved back to LLM Complete.")
    artifact = render_llm_complete_pdf(
        workspace_name=scope.project.workspace.name,
        project_name=scope.project.name,
        issue_identifier=f"{scope.project.identifier}-{scope.issue.sequence_id}",
        issue_name=scope.issue.name,
        task_description=task_description,
        progress=progress,
        screenshot_paths=screenshot_paths,
    )
    if artifact.length > settings.FILE_SIZE_LIMIT:
        raise LlmCompleteConflict(
            f"The generated PDF is {artifact.length} bytes, above the {settings.FILE_SIZE_LIMIT}-byte attachment limit."
        )

    if dry_run:
        return LlmCompleteResult(
            scope=scope,
            artifact=artifact,
            attachment=None,
            state=existing_state,
            dry_run=True,
            state_created=False,
            previous_state_id=scope.issue.state_id,
        )

    attachment, attachment_reused = _store_or_reuse_attachment(scope, artifact)
    if attachment_reused:
        current_state_id = (
            Issue.issue_objects.filter(
                pk=scope.issue.id,
                workspace_id=scope.issue.workspace_id,
                project_id=scope.issue.project_id,
            )
            .values_list("state_id", flat=True)
            .first()
        )
        if current_state_id == existing_state.id:
            return LlmCompleteResult(
                scope=scope,
                artifact=artifact,
                attachment=attachment,
                state=existing_state,
                dry_run=False,
                attachment_reused=True,
                previous_state_id=current_state_id,
            )
        if (attachment.storage_metadata or {}).get("initial_state_id") != str(current_state_id):
            raise LlmCompleteConflict(
                "This PDF belongs to a prior finalization; the work item state has since changed."
            )
    state, state_created, previous_state_id = _apply_llm_complete_state(scope, attachment, artifact)
    return LlmCompleteResult(
        scope=scope,
        artifact=artifact,
        attachment=attachment,
        state=state,
        dry_run=False,
        attachment_reused=attachment_reused,
        state_created=state_created,
        previous_state_id=previous_state_id,
    )
