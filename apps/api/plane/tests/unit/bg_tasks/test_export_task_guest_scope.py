# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""issue_export_task: guests only export issues they created unless guest_view_all_features."""

import json
import zipfile
from unittest.mock import patch
from uuid import uuid4

import pytest

from plane.bgtasks.export_task import issue_export_task
from plane.db.models import ExporterHistory, Issue, IssueLink, Project, ProjectMember, User, WorkspaceMember
from plane.utils.issue_chat import progress_block_id, render_progress_block
from plane.utils.exporters.schemas.issue import IssueExportSchema

pytestmark = pytest.mark.unit


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(name="Export", identifier="EXP", workspace=workspace, created_by=create_user)
    ProjectMember.objects.create(project=project, member=create_user, workspace=workspace, role=20)
    return project


@pytest.fixture
def make_member(db, workspace, project):
    def make(project_role):
        unique_id = uuid4().hex[:8]
        user = User.objects.create(email=f"user-{unique_id}@plane.so", username=f"user_{unique_id}")
        WorkspaceMember.objects.create(workspace=workspace, member=user, role=15)
        ProjectMember.objects.create(project=project, member=user, workspace=workspace, role=project_role)
        return user

    return make


def _make_issue(name, project, author):
    issue = Issue(name=name, project=project, workspace=project.workspace)
    issue.save(created_by_id=author.id)
    return issue


def _exported_rows(workspace, project, user):
    exporter = ExporterHistory.objects.create(
        workspace=workspace, project=[project.id], initiated_by=user, provider="json", type="issue_exports"
    )
    with patch("plane.bgtasks.export_task.upload_to_s3") as upload:
        issue_export_task(
            provider="json",
            workspace_id=workspace.id,
            project_ids=[str(project.id)],
            token_id=exporter.token,
            multiple=False,
            slug=workspace.slug,
        )
    assert upload.called, ExporterHistory.objects.get(pk=exporter.pk).reason
    with zipfile.ZipFile(upload.call_args.args[0]) as zf:
        return [row for f in zf.namelist() for row in json.loads(zf.read(f))]


def _exported_names(workspace, project, user):
    return {row["name"] for row in _exported_rows(workspace, project, user)}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "project_role, guest_view_all_features, expected",
    [
        (5, False, {"own"}),
        (5, True, {"own", "other"}),
        (15, False, {"own", "other"}),
    ],
)
def test_export_respects_guest_scope(
    workspace, project, create_user, make_member, project_role, guest_view_all_features, expected
):
    Project.objects.filter(pk=project.pk).update(guest_view_all_features=guest_view_all_features)
    user = make_member(project_role)
    _make_issue("own", project, user)
    _make_issue("other", project, create_user)

    assert _exported_names(workspace, project, user) == expected


@pytest.mark.django_db
@pytest.mark.parametrize("project_role, expected_managed_text", [(5, False), (15, True)])
def test_export_redacts_generated_description_for_project_guests(
    workspace,
    project,
    make_member,
    project_role,
    expected_managed_text,
):
    Project.objects.filter(pk=project.pk).update(guest_view_all_features=True)
    user = make_member(project_role)
    issue = _make_issue("described", project, user)
    binding_id = uuid4()
    block = render_progress_block(
        binding_id,
        {"done": ["Managed completion"], "now": ["Managed current work"], "waiting": []},
    )
    issue.description_html = f"<p>Manual export description.</p>{block}"
    issue.save()
    IssueLink.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        url="https://chatgpt.com/codex/tasks/export-private",
        title="Managed chat",
        metadata={"kind": "chat", "chat_binding_id": str(binding_id)},
    )
    IssueLink.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        url="https://example.com/export-regular",
        title="Regular export link",
        metadata={"source": "manual"},
    )

    rows = _exported_rows(workspace, project, user)

    assert len(rows) == 1
    assert "Manual export description." in rows[0]["description"]
    assert ('id="' + progress_block_id(binding_id) + '"') not in rows[0]["description"]
    assert ("Managed completion" in rows[0]["description"]) is expected_managed_text
    exported_link_urls = {link["url"] for link in rows[0]["links"]}
    assert "https://example.com/export-regular" in exported_link_urls
    assert ("https://chatgpt.com/codex/tasks/export-private" in exported_link_urls) is expected_managed_text


@pytest.mark.django_db
def test_issue_export_schema_redacts_only_restricted_projects(workspace, project, create_user):
    issue = _make_issue("schema-described", project, create_user)
    block = render_progress_block(
        uuid4(),
        {"done": ["Schema completion"], "now": [], "waiting": []},
    )
    issue.description_html = f"<p>Manual schema description.</p>{block}"
    issue.save()
    IssueLink.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        url="https://chatgpt.com/codex/tasks/schema-private",
        metadata={"kind": "chat", "chat_binding_id": str(uuid4())},
    )
    IssueLink.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        url="https://example.com/schema-regular",
        metadata={"source": "manual"},
    )

    restricted = IssueExportSchema(
        context={"redact_chat_progress_project_ids": {str(project.id)}}
    ).serialize(issue)
    privileged = IssueExportSchema(context={"redact_chat_progress_project_ids": set()}).serialize(issue)

    assert restricted["description"] == "Manual schema description."
    assert restricted["link"] == ["https://example.com/schema-regular"]
    assert set(privileged["link"]) == {
        "https://chatgpt.com/codex/tasks/schema-private",
        "https://example.com/schema-regular",
    }
    assert "Schema completion" in privileged["description"]
