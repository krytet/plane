# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Read-only work item fields sent through ``IntakeIssueViewSet.partial_update`` are
ignored and not recorded as activity."""

import pytest
from rest_framework import status

from plane.celery import app as celery_app
from plane.db.models import Intake, IntakeIssue, Issue, IssueActivity, IssueChatBinding, Project, ProjectMember, State
from plane.utils.issue_chat import (
    progress_block_hash,
    progress_block_id,
    render_progress_block,
)


@pytest.fixture(autouse=True)
def celery_eager():
    """Run the issue activity task in-process; there is no broker in the test stack."""
    original_always_eager = celery_app.conf.task_always_eager
    original_eager_propagates = celery_app.conf.task_eager_propagates
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    yield
    celery_app.conf.task_always_eager = original_always_eager
    celery_app.conf.task_eager_propagates = original_eager_propagates


@pytest.fixture
def intake_issue(db, workspace, create_user):
    project = Project.objects.create(
        name="Intake Project", identifier="IP", workspace=workspace, created_by=create_user, intake_view=True
    )
    ProjectMember.objects.create(project=project, member=create_user, role=20, is_active=True)
    State.objects.create(
        name="Triage", color="#000000", group="triage", default=True, project=project, workspace=workspace
    )
    intake = Intake.objects.create(name="Intake", project=project, workspace=workspace, is_default=True)
    issue = Issue.objects.create(name="Intake Issue", project=project, workspace=workspace, created_by=create_user)
    IntakeIssue.objects.create(intake=intake, issue=issue, project=project, workspace=workspace)
    return issue


@pytest.mark.contract
@pytest.mark.django_db
def test_intake_update_ignores_archived_at_and_records_no_archive_activity(session_client, workspace, intake_issue):
    response = session_client.patch(
        f"/api/workspaces/{workspace.slug}/projects/{intake_issue.project_id}/intake-issues/{intake_issue.id}/",
        {"issue": {"name": "Renamed", "archived_at": "2020-01-01"}},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK, response.data
    issue = Issue.all_objects.get(pk=intake_issue.id)
    assert issue.archived_at is None
    assert issue.name == "Renamed"
    activities = IssueActivity.objects.filter(issue_id=intake_issue.id)
    assert activities.filter(field="name").exists()
    assert not activities.filter(field="archived_at").exists()


@pytest.mark.contract
@pytest.mark.django_db
def test_intake_update_preserves_generated_html_and_json_nodes(session_client, workspace, intake_issue):
    binding = IssueChatBinding.objects.create(
        workspace=workspace,
        project=intake_issue.project,
        issue=intake_issue,
        provider="codex",
        thread_id="intake-preservation",
        progress={"done": ["Trusted done"], "now": ["Trusted now"], "waiting": []},
    )
    block = render_progress_block(binding.id, binding.progress)
    intake_issue.description_html = f"<p>Original intake text.</p>{block}"
    intake_issue.description_json = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Original intake text."}]},
            {
                "type": "chatProgress",
                "attrs": {"id": progress_block_id(binding.id), "progress": binding.progress},
            },
        ],
    }
    intake_issue.save(update_fields=["description_html", "description_json"])
    IssueChatBinding.objects.filter(pk=binding.pk).update(generated_block_hash=progress_block_hash(block))

    response = session_client.patch(
        f"/api/workspaces/{workspace.slug}/projects/{intake_issue.project_id}/intake-issues/{intake_issue.id}/",
        {
            "issue": {
                "name": "Edited intake",
                "description_html": "<p>Edited manual text.</p>",
                "description_json": {
                    "type": "doc",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Edited manual text."}]}
                    ],
                },
            }
        },
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK, response.data
    intake_issue.refresh_from_db()
    assert "Edited manual text." in intake_issue.description_html
    assert "Trusted now" in intake_issue.description_html
    assert intake_issue.description_html.count('data-node-type="chat-progress"') == 1
    json_nodes = [
        node
        for node in intake_issue.description_json.get("content", [])
        if node.get("type") == "chatProgress"
    ]
    assert len(json_nodes) == 1
    assert json_nodes[0]["attrs"]["id"] == progress_block_id(binding.id)
    assert json_nodes[0]["attrs"]["progress"]["now"] == ["Trusted now"]


@pytest.mark.contract
@pytest.mark.django_db
def test_json_only_intake_update_preserves_latest_managed_progress(session_client, workspace, intake_issue):
    binding = IssueChatBinding.objects.create(
        workspace=workspace,
        project=intake_issue.project,
        issue=intake_issue,
        provider="codex",
        thread_id="intake-json-only",
        progress={"done": ["Trusted JSON done"], "now": ["Trusted JSON now"], "waiting": []},
    )
    block = render_progress_block(binding.id, binding.progress)
    intake_issue.description_html = f"<p>Original intake text.</p>{block}"
    intake_issue.description_json = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Original intake text."}]},
            {
                "type": "chatProgress",
                "attrs": {"id": progress_block_id(binding.id), "progress": binding.progress},
            },
        ],
    }
    intake_issue.save(update_fields=["description_html", "description_json"])
    IssueChatBinding.objects.filter(pk=binding.pk).update(generated_block_hash=progress_block_hash(block))

    stale_json = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Edited JSON only."}]},
            {
                "type": "chatProgress",
                "attrs": {
                    "id": progress_block_id(binding.id),
                    "progress": {"done": [], "now": ["Forged JSON now"], "waiting": []},
                },
            },
        ],
    }
    response = session_client.patch(
        f"/api/workspaces/{workspace.slug}/projects/{intake_issue.project_id}/intake-issues/{intake_issue.id}/",
        {"issue": {"description_json": stale_json}},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK, response.data
    intake_issue.refresh_from_db()
    assert "Trusted JSON now" in intake_issue.description_html
    json_nodes = [
        node
        for node in intake_issue.description_json.get("content", [])
        if node.get("type") == "chatProgress"
    ]
    assert len(json_nodes) == 1
    assert json_nodes[0]["attrs"]["progress"]["now"] == ["Trusted JSON now"]
    assert "Forged JSON now" not in str(intake_issue.description_json)
