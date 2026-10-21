# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from __future__ import annotations

import json
from uuid import UUID

from django.core.management import BaseCommand, CommandError

from plane.utils.llm_complete import LlmCompleteError, finalize_llm_complete
from plane.utils.llm_complete_report import LlmCompleteReportError


class Command(BaseCommand):
    help = (
        "Explicitly finalize one project work item with a verified Russian LLM Complete PDF. "
        "The work item is moved to the STARTED 'LLM Complete' state only after attachment readback verification."
    )

    def add_arguments(self, parser):
        parser.add_argument("--workspace", required=True, help="Workspace slug")
        parser.add_argument("--project-id", required=True, type=UUID, help="Project UUID")
        parser.add_argument("--issue-id", required=True, type=UUID, help="Work item UUID")
        parser.add_argument("--operator-id", required=True, type=UUID, help="Active project admin/member user UUID")
        parser.add_argument(
            "--screenshot",
            dest="screenshots",
            action="append",
            required=True,
            help="Screenshot PNG/JPEG/WebP path; repeat for multiple screenshots",
        )
        parser.add_argument("--task-description", required=True, help="Russian task description")
        parser.add_argument("--progress", required=True, help="Russian progress summary")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate inputs, permissions, state configuration, and render the PDF without writes",
        )

    def handle(self, *args, **options):
        try:
            result = finalize_llm_complete(
                workspace_slug=options["workspace"],
                project_id=options["project_id"],
                issue_id=options["issue_id"],
                actor_id=options["operator_id"],
                screenshot_paths=options["screenshots"],
                task_description=options["task_description"],
                progress=options["progress"],
                dry_run=options["dry_run"],
            )
        except (LlmCompleteError, LlmCompleteReportError) as exc:
            code = getattr(exc, "code", "invalid_report")
            asset_id = getattr(exc, "asset_id", None)
            suffix = f" asset_id={asset_id}" if asset_id else ""
            raise CommandError(f"{code}: {exc}{suffix}") from exc

        payload = {
            "dry_run": result.dry_run,
            "workspace": result.scope.project.workspace.slug,
            "project_id": str(result.scope.project.id),
            "issue_id": str(result.scope.issue.id),
            "previous_state_id": str(result.previous_state_id) if result.previous_state_id else None,
            "state": (
                {
                    "id": str(result.state.id),
                    "name": result.state.name,
                    "group": result.state.group,
                    "created": result.state_created,
                }
                if result.state
                else None
            ),
            "attachment": (
                {
                    "id": str(result.attachment.id),
                    "name": result.attachment.attributes.get("name"),
                    "reused": result.attachment_reused,
                }
                if result.attachment
                else None
            ),
            "pdf": {
                "sha256": result.artifact.sha256,
                "length": result.artifact.length,
                "prefix": "%PDF-",
                "readback_verified": not result.dry_run,
            },
        }
        self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
