# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from io import StringIO
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import pytest
from django.core.management import call_command


@pytest.mark.unit
def test_finalize_command_reports_verified_project_scoped_result():
    project_id = uuid4()
    issue_id = uuid4()
    state_id = uuid4()
    attachment_id = uuid4()
    result = SimpleNamespace(
        dry_run=False,
        scope=SimpleNamespace(
            project=SimpleNamespace(
                id=project_id,
                workspace=SimpleNamespace(slug="workspace"),
            ),
            issue=SimpleNamespace(id=issue_id),
        ),
        previous_state_id=uuid4(),
        state=SimpleNamespace(
            id=state_id,
            name="LLM Complete",
            group="started",
        ),
        state_created=True,
        attachment=SimpleNamespace(
            id=attachment_id,
            attributes={"name": "LLM-Complete-PLN-12.pdf"},
        ),
        attachment_reused=False,
        artifact=SimpleNamespace(
            sha256="a" * 64,
            length=1234,
        ),
    )
    stdout = StringIO()

    with mock.patch(
        "plane.db.management.commands.finalize_llm_complete.finalize_llm_complete",
        return_value=result,
    ) as finalize:
        call_command(
            "finalize_llm_complete",
            "--workspace",
            "workspace",
            "--project-id",
            str(project_id),
            "--issue-id",
            str(issue_id),
            "--operator-id",
            str(uuid4()),
            "--screenshot",
            "screen.png",
            "--task-description",
            "Русская задача",
            "--progress",
            "Русский прогресс",
            stdout=stdout,
        )

    assert '"group": "started"' in stdout.getvalue()
    assert '"readback_verified": true' in stdout.getvalue()
    finalize.assert_called_once()
