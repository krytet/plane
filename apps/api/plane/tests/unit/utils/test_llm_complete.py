# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from io import BytesIO
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import pytest

from plane.utils.llm_complete import (
    LLM_COMPLETE_EXTERNAL_SOURCE,
    LLM_COMPLETE_STATE_NAME,
    LlmCompleteConflict,
    LlmCompleteReconciliationRequired,
    _assert_started_state,
    _attachment_values,
    _has_finalize_permission,
    _read_stored_pdf,
    _store_or_reuse_attachment,
    finalize_llm_complete,
)
from plane.utils.llm_complete_report import PdfArtifact


def _scope():
    issue_id = uuid4()
    workspace_id = uuid4()
    project_id = uuid4()
    actor_id = uuid4()
    issue = SimpleNamespace(
        id=issue_id,
        workspace_id=workspace_id,
        project_id=project_id,
        sequence_id=12,
        state_id=uuid4(),
        state=SimpleNamespace(group="started"),
        name="Финальный отчёт",
    )
    project = SimpleNamespace(
        id=project_id,
        identifier="PLN",
        name="Plane",
        workspace=SimpleNamespace(name="Workspace"),
    )
    return SimpleNamespace(
        actor=SimpleNamespace(id=actor_id),
        project=project,
        issue=issue,
    )


class _MemoryStorage:
    def __init__(self, data=b""):
        self.data = data
        self.save_calls = 0
        self.save_error = None

    def open(self, name, mode):
        if self.save_error is not None and not self.data:
            raise OSError("missing")
        return BytesIO(self.data)

    def save(self, name, content):
        self.save_calls += 1
        self.data = content.read()
        if self.save_error is not None:
            raise self.save_error
        return name


class _NativeS3Storage:
    def __init__(self):
        self.data = None
        self.upload_calls = 0
        self.aws_storage_bucket_name = "local-test"
        self.s3_client = self

    def upload_file(self, file_obj, object_name, content_type=None):
        self.upload_calls += 1
        assert content_type == "application/pdf"
        self.data = file_obj.read()
        return True

    def get_object(self, Bucket, Key):
        assert Bucket == self.aws_storage_bucket_name
        assert Key == "workspace/report.pdf"
        if self.data is None:
            raise OSError("missing")
        return {"Body": BytesIO(self.data)}


def _attachment(storage):
    return SimpleNamespace(
        id=uuid4(),
        asset=SimpleNamespace(name="workspace/report.pdf", storage=storage),
        attributes={"name": "LLM-Complete-PLN-12.pdf", "type": "application/pdf"},
        workspace_id=uuid4(),
        project_id=uuid4(),
        issue_id=uuid4(),
        is_deleted=False,
    )


@pytest.mark.unit
def test_finalize_requires_project_member_role():
    workspace_member = SimpleNamespace(is_active=True, role=15)

    assert _has_finalize_permission(
        workspace_member,
        SimpleNamespace(is_active=True, role=15),
    )
    assert _has_finalize_permission(
        workspace_member,
        SimpleNamespace(is_active=True, role=20),
    )
    assert not _has_finalize_permission(
        workspace_member,
        SimpleNamespace(is_active=True, role=5),
    )
    assert not _has_finalize_permission(
        SimpleNamespace(is_active=False, role=20),
        SimpleNamespace(is_active=True, role=20),
    )
    assert not _has_finalize_permission(
        SimpleNamespace(is_active=True, role=5),
        SimpleNamespace(is_active=True, role=20),
    )


@pytest.mark.unit
def test_completed_llm_complete_state_is_rejected():
    _assert_started_state(SimpleNamespace(group="started"))

    with pytest.raises(LlmCompleteConflict, match="STARTED"):
        _assert_started_state(SimpleNamespace(group="completed"))


@pytest.mark.unit
def test_attachment_payload_uses_project_scoped_issue_attachment_protocol():
    scope = _scope()
    artifact = PdfArtifact.from_bytes(b"%PDF-1.7\nreport")

    values = _attachment_values(scope, artifact, "workspace-key/report.pdf")

    assert values["workspace_id"] == scope.issue.workspace_id
    assert values["project_id"] == scope.issue.project_id
    assert values["issue_id"] == scope.issue.id
    assert values["entity_type"] == "ISSUE_ATTACHMENT"
    assert values["external_source"] == LLM_COMPLETE_EXTERNAL_SOURCE
    assert values["external_id"] == f"{scope.issue.id}:{artifact.sha256}"
    assert values["attributes"]["type"] == "application/pdf"
    assert values["attributes"]["sha256"] == artifact.sha256
    assert values["is_uploaded"] is False
    assert values["created_by_id"] == scope.actor.id


@pytest.mark.unit
def test_readback_checks_pdf_prefix_length_and_hash():
    artifact = PdfArtifact.from_bytes(b"%PDF-1.7\nreport")
    attachment = _attachment(_MemoryStorage(artifact.data))

    assert _read_stored_pdf(attachment, artifact)
    assert not _read_stored_pdf(_attachment(_MemoryStorage(b"%PDF-1.7\nother")), artifact)
    assert not _read_stored_pdf(_attachment(_MemoryStorage(b"NOTPDF\n")), artifact)
    assert not _read_stored_pdf(_attachment(_MemoryStorage(b"")), artifact)


@pytest.mark.unit
def test_native_s3_upload_uses_supported_client_and_verifies_readback():
    scope = _scope()
    artifact = PdfArtifact.from_bytes(b"%PDF-1.7\nreport")
    storage = _NativeS3Storage()
    attachment = _attachment(storage)

    with (
        mock.patch(
            "plane.utils.llm_complete._reserve_or_reuse_attachment",
            return_value=(attachment, False),
        ),
        mock.patch("plane.utils.llm_complete._mark_attachment_uploaded") as mark_uploaded,
    ):
        result, reused = _store_or_reuse_attachment(scope, artifact)

    assert result is attachment
    assert reused is False
    assert storage.upload_calls == 1
    assert _read_stored_pdf(attachment, artifact)
    mark_uploaded.assert_called_once()


@pytest.mark.unit
def test_existing_attachment_mismatch_requires_reconciliation_without_upload_retry():
    scope = _scope()
    artifact = PdfArtifact.from_bytes(b"%PDF-1.7\nreport")
    storage = _MemoryStorage(b"%PDF-1.7\nother")
    attachment = _attachment(storage)

    with (
        mock.patch(
            "plane.utils.llm_complete._reserve_or_reuse_attachment",
            return_value=(attachment, True),
        ),
        pytest.raises(LlmCompleteReconciliationRequired, match="no upload was retried"),
    ):
        _store_or_reuse_attachment(scope, artifact)

    assert storage.save_calls == 0


@pytest.mark.unit
def test_uncertain_upload_reconciles_matching_readback_and_does_not_retry():
    scope = _scope()
    artifact = PdfArtifact.from_bytes(b"%PDF-1.7\nreport")
    storage = _MemoryStorage()
    storage.save_error = OSError("connection dropped")
    attachment = _attachment(storage)

    with (
        mock.patch(
            "plane.utils.llm_complete._reserve_or_reuse_attachment",
            return_value=(attachment, False),
        ),
        mock.patch("plane.utils.llm_complete._mark_attachment_uploaded") as mark_uploaded,
    ):
        result, reused = _store_or_reuse_attachment(scope, artifact)

    assert result is attachment
    assert reused is False
    assert storage.save_calls == 1
    mark_uploaded.assert_called_once()


@pytest.mark.unit
def test_finalize_runs_state_change_only_after_upload_readback():
    scope = _scope()
    artifact = PdfArtifact.from_bytes(b"%PDF-1.7\nreport")
    attachment = SimpleNamespace(id=uuid4(), attributes={"name": "report.pdf"})
    state = SimpleNamespace(id=uuid4(), name=LLM_COMPLETE_STATE_NAME, group="started")
    events = []

    def store(*args):
        events.append("upload")
        return attachment, False

    def apply(*args):
        events.append("state")
        return state, False, scope.issue.state_id

    with (
        mock.patch("plane.utils.llm_complete.resolve_llm_complete_scope", return_value=scope),
        mock.patch("plane.utils.llm_complete._find_llm_complete_state", return_value=state),
        mock.patch("plane.utils.llm_complete.render_llm_complete_pdf", return_value=artifact),
        mock.patch("plane.utils.llm_complete._store_or_reuse_attachment", side_effect=store),
        mock.patch("plane.utils.llm_complete._apply_llm_complete_state", side_effect=apply),
    ):
        result = finalize_llm_complete(
            workspace_slug="workspace",
            project_id=scope.project.id,
            issue_id=scope.issue.id,
            actor_id=scope.actor.id,
            screenshot_paths=["screen.png"],
            task_description="Русская задача",
            progress="Русский прогресс",
        )

    assert events == ["upload", "state"]
    assert result.attachment is attachment
    assert result.state is state


@pytest.mark.unit
def test_finalize_never_changes_state_after_upload_failure():
    scope = _scope()
    artifact = PdfArtifact.from_bytes(b"%PDF-1.7\nreport")
    state = SimpleNamespace(id=uuid4(), name=LLM_COMPLETE_STATE_NAME, group="started")

    with (
        mock.patch("plane.utils.llm_complete.resolve_llm_complete_scope", return_value=scope),
        mock.patch("plane.utils.llm_complete._find_llm_complete_state", return_value=state),
        mock.patch("plane.utils.llm_complete.render_llm_complete_pdf", return_value=artifact),
        mock.patch(
            "plane.utils.llm_complete._store_or_reuse_attachment",
            side_effect=LlmCompleteReconciliationRequired("uncertain", asset_id=uuid4()),
        ),
        mock.patch("plane.utils.llm_complete._apply_llm_complete_state") as apply_state,
        pytest.raises(LlmCompleteReconciliationRequired),
    ):
        finalize_llm_complete(
            workspace_slug="workspace",
            project_id=scope.project.id,
            issue_id=scope.issue.id,
            actor_id=scope.actor.id,
            screenshot_paths=["screen.png"],
            task_description="Русская задача",
            progress="Русский прогресс",
        )

    apply_state.assert_not_called()


@pytest.mark.unit
def test_replay_after_human_state_change_does_not_revert_state():
    scope = _scope()
    scope.issue.state = SimpleNamespace(group="started")
    artifact = PdfArtifact.from_bytes(b"%PDF-1.7\nreport")
    state = SimpleNamespace(id=uuid4(), name=LLM_COMPLETE_STATE_NAME, group="started")
    attachment = SimpleNamespace(storage_metadata={"initial_state_id": str(scope.issue.state_id)})
    changed_state_id = uuid4()

    with (
        mock.patch("plane.utils.llm_complete.resolve_llm_complete_scope", return_value=scope),
        mock.patch("plane.utils.llm_complete._find_llm_complete_state", return_value=state),
        mock.patch("plane.utils.llm_complete.render_llm_complete_pdf", return_value=artifact),
        mock.patch(
            "plane.utils.llm_complete._store_or_reuse_attachment",
            return_value=(attachment, True),
        ),
        mock.patch("plane.utils.llm_complete.Issue") as issue_model,
        mock.patch("plane.utils.llm_complete._apply_llm_complete_state") as apply_state,
        pytest.raises(LlmCompleteConflict, match="state has since changed"),
    ):
        issue_model.issue_objects.filter.return_value.values_list.return_value.first.return_value = changed_state_id
        finalize_llm_complete(
            workspace_slug="workspace",
            project_id=scope.project.id,
            issue_id=scope.issue.id,
            actor_id=scope.actor.id,
            screenshot_paths=["screen.png"],
            task_description="Задача",
            progress="Прогресс",
        )

    apply_state.assert_not_called()
