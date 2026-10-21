# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import FileAsset, Issue, Project, ProjectMember, User, Workspace, WorkspaceMember


def _user(prefix):
    suffix = uuid4().hex[:8]
    user = User.objects.create(email=f"{prefix}-{suffix}@plane.so", username=f"{prefix}_{suffix}")
    user.set_password("test-password")
    user.save()
    return user


def _workspace(owner):
    workspace = Workspace.objects.create(
        name="Files workspace",
        owner=owner,
        slug=f"files-{uuid4().hex[:8]}",
    )
    WorkspaceMember.objects.create(workspace=workspace, member=owner, role=20)
    return workspace


def _project(workspace, owner, identifier):
    project = Project.objects.create(
        name=f"{identifier} project",
        identifier=identifier,
        workspace=workspace,
        created_by=owner,
    )
    ProjectMember.objects.create(project=project, member=owner, workspace=workspace, role=20)
    return project


def _issue(project, name):
    return Issue.objects.create(name=name, project=project, workspace=project.workspace)


def _asset(workspace, project, user, issue=None, **overrides):
    values = {
        "attributes": {"name": "evidence.pdf", "type": "application/pdf"},
        "asset": f"{workspace.id}/{uuid4().hex}-evidence.pdf",
        "size": 1024,
        "workspace": workspace,
        "project": project,
        "issue": issue,
        "user": user,
        "created_by": user,
        "entity_type": FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
        "is_uploaded": True,
    }
    values.update(overrides)
    return FileAsset.objects.create(**values)


def _url(workspace, project):
    return f"/api/workspaces/{workspace.slug}/projects/{project.id}/files/"


@pytest.fixture
def files_tenant(db):
    owner = _user("file-owner")
    workspace = _workspace(owner)
    project = _project(workspace, owner, "FILES")
    issue = _issue(project, "Attachment issue")
    client = APIClient()
    client.force_login(owner)
    return {
        "owner": owner,
        "workspace": workspace,
        "project": project,
        "issue": issue,
        "client": client,
    }


@pytest.mark.contract
@pytest.mark.django_db
def test_project_file_overview_lists_only_uploaded_active_issue_attachments(files_tenant):
    tenant = files_tenant
    expected = _asset(tenant["workspace"], tenant["project"], tenant["owner"], tenant["issue"])
    second_issue = _issue(tenant["project"], "Second issue")
    second = _asset(tenant["workspace"], tenant["project"], tenant["owner"], second_issue)

    _asset(tenant["workspace"], tenant["project"], tenant["owner"], tenant["issue"], is_uploaded=False)
    _asset(tenant["workspace"], tenant["project"], tenant["owner"], tenant["issue"], is_deleted=True)
    _asset(
        tenant["workspace"],
        tenant["project"],
        tenant["owner"],
        tenant["issue"],
        deleted_at=timezone.now(),
    )
    _asset(
        tenant["workspace"],
        tenant["project"],
        tenant["owner"],
        tenant["issue"],
        entity_type=FileAsset.EntityTypeContext.ISSUE_DESCRIPTION,
    )
    _asset(tenant["workspace"], tenant["project"], tenant["owner"], None)

    other_project = _project(tenant["workspace"], tenant["owner"], "OTHER")
    other_issue = _issue(other_project, "Other issue")
    _asset(tenant["workspace"], other_project, tenant["owner"], other_issue)

    response = tenant["client"].get(_url(tenant["workspace"], tenant["project"]))

    assert response.status_code == status.HTTP_200_OK, response.content
    assert response.data["total_count"] == 2
    assert response.data["count"] == 2
    result_ids = {str(item["id"]) for item in response.data["results"]}
    assert result_ids == {str(expected.id), str(second.id)}

    by_id = {str(item["id"]): item for item in response.data["results"]}
    item = by_id[str(expected.id)]
    assert item["name"] == "evidence.pdf"
    assert item["type"] == "application/pdf"
    assert str(item["issue_id"]) == str(tenant["issue"].id)
    assert item["issue_identifier"] == f"FILES-{tenant['issue'].sequence_id}"
    assert item["project_identifier"] == "FILES"
    assert item["url"].startswith(f"/api/assets/v2/workspaces/{tenant['workspace'].slug}/projects/")


@pytest.mark.contract
@pytest.mark.django_db
def test_project_file_overview_requires_membership_in_the_url_project(files_tenant):
    outsider = _user("file-outsider")
    WorkspaceMember.objects.create(workspace=files_tenant["workspace"], member=outsider, role=20)
    client = APIClient()
    client.force_login(outsider)

    response = client.get(_url(files_tenant["workspace"], files_tenant["project"]))

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.contract
@pytest.mark.django_db
def test_project_file_overview_denies_project_guests_even_with_full_feature_access(files_tenant):
    files_tenant["project"].guest_view_all_features = True
    files_tenant["project"].save(update_fields=["guest_view_all_features"])
    _asset(files_tenant["workspace"], files_tenant["project"], files_tenant["owner"], files_tenant["issue"])

    guest = _user("file-guest")
    WorkspaceMember.objects.create(workspace=files_tenant["workspace"], member=guest, role=5)
    ProjectMember.objects.create(
        project=files_tenant["project"],
        workspace=files_tenant["workspace"],
        member=guest,
        role=5,
    )
    client = APIClient()
    client.force_login(guest)

    response = client.get(_url(files_tenant["workspace"], files_tenant["project"]))

    assert response.status_code == status.HTTP_403_FORBIDDEN
