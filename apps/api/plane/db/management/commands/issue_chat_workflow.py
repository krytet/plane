# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from __future__ import annotations

import json
from uuid import UUID

from django.core.management import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from plane.utils.issue_chat_workflow import (
    IssueChatWorkflowError,
    configure_issue_chat_workflow,
    get_issue_chat_workflow_diagnostics,
)


class Command(BaseCommand):
    help = (
        "Read-only diagnostics or explicit per-project configuration for the opt-in "
        "chat workflow. Configuration never rewrites historical issue states."
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("diagnostics", "configure"))
        parser.add_argument("--workspace", required=True, help="Exact workspace slug")
        parser.add_argument("--project-id", required=True, type=UUID, help="Exact project UUID")
        parser.add_argument("--operator-id", type=UUID, help="Active project admin/member user UUID")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--enable", action="store_true", help="Explicitly enable or revise the policy")
        mode.add_argument("--disable", action="store_true", help="Explicitly disable the policy")
        parser.add_argument("--source-state-id", type=UUID, help="Human source state, normally In Progress")
        parser.add_argument("--running-state-id", type=UUID, help="Automatic running state, normally LLM Work")
        parser.add_argument(
            "--attention-state-id",
            type=UUID,
            help="Attention state, normally Requires attention",
        )
        parser.add_argument(
            "--manual-hold-state-id",
            type=UUID,
            help="Manually protected state, normally Come back later",
        )
        parser.add_argument(
            "--completion-state-id",
            type=UUID,
            help="The project's exact STARTED LLM Complete state",
        )
        parser.add_argument(
            "--todo-state-id",
            type=UUID,
            help="Optional UNSTARTED Todo source; only a fresh running observation may leave it",
        )
        parser.add_argument(
            "--stale-running-ttl-seconds",
            type=int,
            default=3600,
            help="Age after which a running observation is stale (default: 3600)",
        )

    def _configure(self, options):
        if not options["operator_id"]:
            raise CommandError("--operator-id is required for configure.")
        if not options["enable"] and not options["disable"]:
            raise CommandError("configure requires an explicit --enable or --disable.")
        if options["enable"]:
            required = {
                "--source-state-id": options["source_state_id"],
                "--running-state-id": options["running_state_id"],
                "--attention-state-id": options["attention_state_id"],
                "--manual-hold-state-id": options["manual_hold_state_id"],
                "--completion-state-id": options["completion_state_id"],
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise CommandError(f"Explicit state mapping is required: {', '.join(missing)}.")

        result = configure_issue_chat_workflow(
            workspace_slug=options["workspace"],
            project_id=options["project_id"],
            operator_id=options["operator_id"],
            enabled=options["enable"],
            source_state_id=options["source_state_id"],
            running_state_id=options["running_state_id"],
            attention_state_id=options["attention_state_id"],
            manual_hold_state_id=options["manual_hold_state_id"],
            completion_state_id=options["completion_state_id"],
            todo_state_id=options["todo_state_id"],
            stale_running_ttl_seconds=options["stale_running_ttl_seconds"],
        )
        policy = result.policy
        return {
            "mode": "configure",
            "writes_performed": True,
            "created": result.created,
            "project": {
                "id": str(policy.project_id),
                "workspace": policy.workspace.slug,
            },
            "policy": {
                "id": str(policy.id),
                "enabled": policy.enabled,
                "version": policy.version,
                "effective_at": policy.effective_at,
                "stale_running_ttl_seconds": policy.stale_running_ttl_seconds,
                "states": {
                    "source": {
                        "id": str(policy.source_state_id),
                        "name": policy.source_state.name,
                        "group": policy.source_state.group,
                    },
                    "running": {
                        "id": str(policy.running_state_id),
                        "name": policy.running_state.name,
                        "group": policy.running_state.group,
                    },
                    "attention": {
                        "id": str(policy.attention_state_id),
                        "name": policy.attention_state.name,
                        "group": policy.attention_state.group,
                    },
                    "manual_hold": {
                        "id": str(policy.manual_hold_state_id),
                        "name": policy.manual_hold_state.name,
                        "group": policy.manual_hold_state.group,
                    },
                    "completion": {
                        "id": str(policy.completion_state_id),
                        "name": policy.completion_state.name,
                        "group": policy.completion_state.group,
                    },
                    "todo": (
                        {
                            "id": str(policy.todo_state_id),
                            "name": policy.todo_state.name,
                            "group": policy.todo_state.group,
                        }
                        if policy.todo_state_id
                        else None
                    ),
                },
            },
            "historical_issues_mutated": 0,
            "adopted_completion_issues": result.adopted_completion_issues,
        }

    def handle(self, *args, **options):
        try:
            if options["action"] == "diagnostics":
                payload = get_issue_chat_workflow_diagnostics(
                    workspace_slug=options["workspace"],
                    project_id=options["project_id"],
                )
            else:
                payload = self._configure(options)
        except IssueChatWorkflowError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            json.dumps(
                payload,
                cls=DjangoJSONEncoder,
                ensure_ascii=False,
                indent=2,
            )
        )
