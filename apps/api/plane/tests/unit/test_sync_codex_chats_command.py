# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import io
import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import pytest
from django.core.management import call_command, get_commands
from django.utils import timezone

from plane.db.management.commands import sync_codex_chats
from plane.db.models import (
    APIToken,
    Issue,
    IssueChatBinding,
    IssueLink,
    Project,
    ProjectMember,
    State,
    User,
    Workspace,
    WorkspaceMember,
)


def _user(prefix):
    suffix = uuid4().hex[:8]
    return User.objects.create(
        email=f"{prefix}-{suffix}@plane.local",
        username=f"{prefix}_{suffix}",
    )


def _thread_id():
    value = uuid4()
    return f"{value.hex[:8]}-{value.hex[8:12]}-7{value.hex[13:16]}-8{value.hex[17:20]}-{value.hex[20:32]}"


def _tenant():
    owner = _user("owner")
    workspace = Workspace.objects.create(
        name="Local workspace",
        owner=owner,
        slug=f"local-{uuid4().hex[:8]}",
    )
    WorkspaceMember.objects.create(workspace=workspace, member=owner, role=20)
    project = Project.objects.create(
        name="Local project",
        identifier=f"LOC{uuid4().hex[:5].upper()}",
        workspace=workspace,
        created_by=owner,
    )
    ProjectMember.objects.create(
        workspace=workspace,
        project=project,
        member=owner,
        role=20,
    )
    state, _ = State.objects.get_or_create(
        project=project,
        name="In Progress",
        defaults={"color": "#F59E0B", "group": "started"},
    )
    issue = Issue.objects.create(
        name="Bound Codex work",
        project=project,
        workspace=workspace,
        state=state,
        description_html="<p>Manual text stays visible.</p>",
    )
    thread_id = _thread_id()
    link = IssueLink.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        url=f"codex://threads/{thread_id}",
        title="Local Codex task",
        metadata={
            "kind": "chat",
            "provider": "codex",
            "thread_id": thread_id,
            "preserved": "keep-me",
        },
        created_by=owner,
    )
    binding = IssueChatBinding.objects.create(
        workspace=workspace,
        project=project,
        issue=issue,
        provider=IssueChatBinding.Provider.CODEX,
        thread_id=thread_id,
        link=link,
        created_by=owner,
    )
    return {
        "owner": owner,
        "workspace": workspace,
        "project": project,
        "issue": issue,
        "link": link,
        "binding": binding,
        "thread_id": thread_id,
    }


def _write_service_config(path, enabled=True):
    path.write_text(
        json.dumps(
            {
                "enabled": enabled,
                "workspace": "personal",
            }
        ),
        encoding="utf-8",
    )


def test_management_command_is_discoverable():
    assert get_commands()["sync_codex_chats"] == "plane.db"


@pytest.mark.unit
@pytest.mark.django_db
def test_dry_run_generates_config_from_existing_bindings_without_credentials(tmp_path):
    tenant = _tenant()
    original_metadata = dict(tenant["link"].metadata)
    IssueChatBinding.objects.create(
        workspace=tenant["workspace"],
        project=tenant["project"],
        issue=tenant["issue"],
        provider=IssueChatBinding.Provider.CHATGPT,
        thread_id="chatgpt-thread",
        created_by=tenant["owner"],
    )
    binding_count = IssueChatBinding.objects.count()
    captured = {}
    spool_dir = tmp_path / "hook-spool"
    spool_dir.mkdir()
    hook_id = str(uuid4())
    (spool_dir / f"{hook_id}.json").write_text(
        json.dumps(
            {
                "version": 1,
                "id": hook_id,
                "source": "codex_hook",
                "event": "UserPromptSubmit",
                "state": "running",
                "terminal": False,
                "thread_id": tenant["thread_id"],
                "turn_id": str(uuid4()),
                "observed_at": "2026-10-22T12:00:00.000000Z",
            }
        ),
        encoding="utf-8",
    )

    def fake_run(config_path, token, dry_run):
        assert token is None
        assert dry_run is True
        captured["config"] = json.loads(Path(config_path).read_text(encoding="utf-8"))
        return 0, '{"event":"cycle_complete"}\n', "", set()

    stdout = io.StringIO()
    with (
        mock.patch.object(sync_codex_chats, "_assert_local_database"),
        mock.patch.object(sync_codex_chats, "_assert_codex_compatible"),
        mock.patch.object(sync_codex_chats, "_run_node", side_effect=fake_run),
    ):
        call_command(
            "sync_codex_chats",
            workspace=tenant["workspace"].slug,
            codex_executable="mock-codex",
            plane_base_url="http://127.0.0.1:8059",
            state_dir=str(tmp_path),
            once=True,
            dry_run=True,
            stdout=stdout,
        )

    assert "cycle_complete" in stdout.getvalue()
    assert APIToken.objects.count() == 0
    assert User.objects.filter(is_bot=True).count() == 0
    assert IssueChatBinding.objects.count() == binding_count
    assert tenant["link"].metadata == original_metadata

    config = captured["config"]
    assert config["planeBaseUrl"] == "http://127.0.0.1:8059"
    assert config["hookSpoolDir"] == str(tmp_path / "hook-spool")
    assert config["progressMode"] == "structured_or_summary"
    assert len(config["threads"]) == 1
    assert config["threads"][0]["threadId"] == tenant["thread_id"]
    assert config["threads"][0]["url"] == f"codex://threads/{tenant['thread_id']}"
    assert "token" not in Path(tmp_path / "config.json").read_text(encoding="utf-8").lower()
    heartbeat = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert {
        "pid",
        "updated_at",
        "workspace",
        "intervalSeconds",
        "exitCode",
        "threadCount",
        "commandTag",
        "stateDir",
        "hookSpoolDir",
        "hookPendingCount",
        "hookPendingTruncated",
        "hookNewestPendingEventAt",
        "hookNewestPendingEvent",
        "hookNewestPendingState",
        "hookLastAppliedAt",
        "hookLastAppliedEvent",
        "hookLastAppliedState",
    }.issubset(heartbeat)
    assert heartbeat["commandTag"] == "sync_codex_chats"
    assert heartbeat["stateDir"] == str(tmp_path)
    assert heartbeat["hookSpoolDir"] == str(spool_dir)
    assert heartbeat["hookPendingCount"] == 1
    assert heartbeat["hookPendingTruncated"] is False
    assert heartbeat["hookNewestPendingEventAt"] == "2026-10-22T12:00:00+00:00"
    assert heartbeat["hookNewestPendingEvent"] == "UserPromptSubmit"
    assert heartbeat["hookNewestPendingState"] == "running"
    assert heartbeat["hookLastAppliedAt"] is None
    assert heartbeat["hookLastAppliedEvent"] is None
    assert heartbeat["hookLastAppliedState"] is None
    if "processStartedAt" in heartbeat:
        assert heartbeat["processStartedAt"].endswith("+00:00")
    assert heartbeat["workspace"] == tenant["workspace"].slug
    assert heartbeat["intervalSeconds"] == 30
    assert heartbeat["exitCode"] == 0
    assert heartbeat["threadCount"] == 1


@pytest.mark.unit
@pytest.mark.django_db
def test_zero_binding_cycle_writes_healthy_heartbeat(tmp_path):
    owner = _user("empty-owner")
    workspace = Workspace.objects.create(
        name="Empty workspace",
        owner=owner,
        slug=f"empty-{uuid4().hex[:8]}",
    )

    with (
        mock.patch.object(sync_codex_chats, "_assert_local_database"),
        mock.patch.object(sync_codex_chats, "_assert_codex_compatible"),
        mock.patch.object(sync_codex_chats, "_run_node") as run_node,
    ):
        call_command(
            "sync_codex_chats",
            workspace=workspace.slug,
            codex_executable="mock-codex",
            plane_base_url="http://127.0.0.1:8059",
            state_dir=str(tmp_path),
            once=True,
            dry_run=True,
            stdout=io.StringIO(),
        )

    run_node.assert_not_called()
    heartbeat = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert heartbeat["exitCode"] == 0
    assert heartbeat["threadCount"] == 0


@pytest.mark.unit
@pytest.mark.django_db
def test_zero_binding_cycle_revokes_idle_bot_access(tmp_path):
    tenant = _tenant()
    bot = sync_codex_chats._ensure_local_bot(tenant["workspace"], {tenant["project"].id})
    sync_codex_chats._provision_bot_token(bot, tenant["workspace"])
    IssueChatBinding.objects.filter(pk=tenant["binding"].pk).update(deleted_at=timezone.now())

    with (
        mock.patch.object(sync_codex_chats, "_assert_local_database"),
        mock.patch.object(sync_codex_chats, "_assert_codex_compatible"),
        mock.patch.object(sync_codex_chats, "_run_node") as run_node,
    ):
        call_command(
            "sync_codex_chats",
            workspace=tenant["workspace"].slug,
            codex_executable="mock-codex",
            plane_base_url="http://127.0.0.1:8059",
            state_dir=str(tmp_path),
            once=True,
            stdout=io.StringIO(),
        )

    run_node.assert_not_called()
    assert not APIToken.objects.filter(user=bot, is_active=True).exists()
    assert not WorkspaceMember.objects.filter(workspace=tenant["workspace"], member=bot, is_active=True).exists()
    assert not ProjectMember.objects.filter(workspace=tenant["workspace"], member=bot, is_active=True).exists()
    heartbeat = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert heartbeat["exitCode"] == 0
    assert heartbeat["threadCount"] == 0


@pytest.mark.unit
@pytest.mark.django_db
def test_all_active_bindings_skipped_marks_heartbeat_failed(tmp_path):
    tenant = _tenant()
    IssueChatBinding.objects.filter(pk=tenant["binding"].pk).update(thread_id="unsupported-thread")

    with (
        mock.patch.object(sync_codex_chats, "_assert_local_database"),
        mock.patch.object(sync_codex_chats, "_assert_codex_compatible"),
        mock.patch.object(sync_codex_chats, "_run_node") as run_node,
        pytest.raises(sync_codex_chats.CommandError, match="could not be synchronized"),
    ):
        call_command(
            "sync_codex_chats",
            workspace=tenant["workspace"].slug,
            codex_executable="mock-codex",
            plane_base_url="http://127.0.0.1:8059",
            state_dir=str(tmp_path),
            once=True,
            dry_run=True,
            stdout=io.StringIO(),
        )

    run_node.assert_not_called()
    heartbeat = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert heartbeat["exitCode"] == 1
    assert heartbeat["threadCount"] == 0


@pytest.mark.unit
@pytest.mark.django_db
def test_bot_access_and_token_are_scoped_and_idempotent():
    tenant = _tenant()
    project_ids = {str(tenant["project"].id)}

    first_bot = sync_codex_chats._ensure_local_bot(tenant["workspace"], project_ids)
    second_bot = sync_codex_chats._ensure_local_bot(tenant["workspace"], project_ids)
    first_token = sync_codex_chats._provision_bot_token(first_bot, tenant["workspace"])
    second_token = sync_codex_chats._provision_bot_token(second_bot, tenant["workspace"])

    assert first_bot.id == second_bot.id
    assert first_bot.is_bot is True
    assert first_bot.is_active is True
    assert first_bot.has_usable_password() is False
    assert first_token == second_token

    workspace_membership = WorkspaceMember.objects.get(workspace=tenant["workspace"], member=first_bot)
    project_membership = ProjectMember.objects.get(
        workspace=tenant["workspace"],
        project=tenant["project"],
        member=first_bot,
    )
    assert workspace_membership.role == 15
    assert project_membership.role == 15
    assert not APIToken.objects.filter(user=tenant["owner"]).exists()

    token = APIToken.objects.get(user=first_bot, label=sync_codex_chats.TOKEN_LABEL)
    assert token.workspace_id == tenant["workspace"].id
    assert token.user_type == 1
    assert token.expired_at is not None
    assert token.expired_at > timezone.now() + timedelta(days=80)

    stale_project = Project.objects.create(
        name="Stale project",
        identifier=f"OLD{uuid4().hex[:5].upper()}",
        workspace=tenant["workspace"],
        created_by=tenant["owner"],
    )
    ProjectMember.objects.create(
        workspace=tenant["workspace"],
        project=stale_project,
        member=first_bot,
        role=15,
    )
    sync_codex_chats._ensure_local_bot(tenant["workspace"], project_ids)
    assert not ProjectMember.objects.get(
        workspace=tenant["workspace"],
        project=stale_project,
        member=first_bot,
    ).is_active


@pytest.mark.unit
@pytest.mark.django_db
def test_executable_preflight_failure_happens_before_credential_writes(tmp_path):
    tenant = _tenant()
    with (
        mock.patch.object(sync_codex_chats, "_assert_local_database"),
        mock.patch.object(
            sync_codex_chats,
            "_assert_codex_compatible",
            side_effect=sync_codex_chats.CommandError("wrong executable"),
        ),
        pytest.raises(sync_codex_chats.CommandError, match="wrong executable"),
    ):
        call_command(
            "sync_codex_chats",
            workspace=tenant["workspace"].slug,
            codex_executable="wrong-codex",
            plane_base_url="http://127.0.0.1:8059",
            state_dir=str(tmp_path),
            once=True,
        )

    assert User.objects.filter(is_bot=True).count() == 0
    assert APIToken.objects.count() == 0
    assert not (tmp_path / "config.json").exists()


@pytest.mark.unit
def test_run_node_passes_token_only_to_child_environment_and_redacts_output(tmp_path):
    token = "plane_api_" + uuid4().hex
    config_path = tmp_path / "config.json"
    config_path.write_text('{"enabled":true}\n', encoding="utf-8")
    completed = subprocess.CompletedProcess(
        args=["node"],
        returncode=0,
        stdout=f'{{"event":"test","value":"{token}"}}\n',
        stderr=f"echo {token}\n",
    )

    injected_secrets = {
        "AWS_SECRET_ACCESS_KEY": "aws-secret-value",
        "DATABASE_URL": "postgresql://plane:db-password@127.0.0.1/plane",
        "OPENROUTER_API_KEY": "openrouter-secret",
        "CODEX_THREAD_ID": "thread-secret",
        "NODE_EXTRA_CA_CERTS": "C:\\certs\\local.pem",
    }
    with (
        mock.patch.dict(os.environ, injected_secrets),
        mock.patch.object(sync_codex_chats, "_cli_path", return_value=config_path),
        mock.patch.object(sync_codex_chats.subprocess, "run", return_value=completed) as run,
    ):
        return_code, stdout, stderr, fatal_codes = sync_codex_chats._run_node(
            config_path,
            token,
            dry_run=False,
        )

    assert return_code == 0
    assert fatal_codes == set()
    assert token not in stdout
    assert token not in stderr
    child_environment = run.call_args.kwargs["env"]
    assert child_environment["PLANE_API_KEY"] == token
    assert child_environment["NODE_EXTRA_CA_CERTS"] == injected_secrets["NODE_EXTRA_CA_CERTS"]
    for key in ("AWS_SECRET_ACCESS_KEY", "DATABASE_URL", "OPENROUTER_API_KEY", "CODEX_THREAD_ID"):
        assert key not in child_environment
    assert token not in " ".join(run.call_args.args[0])


@pytest.mark.unit
def test_local_only_url_and_state_directory_boundaries(tmp_path):
    assert sync_codex_chats._normalize_loopback_url("http://LOCALHOST:8059/") == ("http://localhost:8059")
    with pytest.raises(sync_codex_chats.CommandError):
        sync_codex_chats._normalize_loopback_url("http://192.0.2.10:8059")
    with pytest.raises(sync_codex_chats.CommandError):
        sync_codex_chats._assert_state_directory(sync_codex_chats._repository_root() / "state")
    assert sync_codex_chats._assert_state_directory(tmp_path) == tmp_path.resolve()


@pytest.mark.unit
@pytest.mark.django_db
def test_disabled_service_config_fails_before_credential_or_codex_work(tmp_path):
    tenant = _tenant()
    service_path = tmp_path / "service.json"
    _write_service_config(service_path, enabled=False)

    with (
        mock.patch.object(sync_codex_chats, "_assert_local_database"),
        mock.patch.object(sync_codex_chats, "_assert_codex_compatible") as preflight,
        pytest.raises(sync_codex_chats.CommandError, match="enabled=true"),
    ):
        call_command(
            "sync_codex_chats",
            workspace=tenant["workspace"].slug,
            codex_executable="mock-codex",
            plane_base_url="http://127.0.0.1:8059",
            state_dir=str(tmp_path),
            service_config=str(service_path),
            once=True,
        )

    preflight.assert_not_called()
    assert not User.objects.filter(is_bot=True).exists()
    assert not APIToken.objects.exists()


@pytest.mark.unit
@pytest.mark.django_db
@pytest.mark.parametrize("change", ["disabled", "removed", "changed"])
def test_service_config_change_stops_before_next_cycle(tmp_path, change):
    tenant = _tenant()
    service_path = tmp_path / "service.json"
    _write_service_config(service_path)
    run_count = 0

    def fake_run(_config_path, token, dry_run):
        nonlocal run_count
        run_count += 1
        assert token is None
        assert dry_run is True
        if change == "disabled":
            _write_service_config(service_path, enabled=False)
        elif change == "removed":
            service_path.unlink()
        else:
            service_path.write_text(service_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return 0, '{"event":"cycle_complete"}\n', "", set()

    stdout = io.StringIO()
    with (
        mock.patch.object(sync_codex_chats, "_assert_local_database"),
        mock.patch.object(sync_codex_chats, "_assert_codex_compatible"),
        mock.patch.object(sync_codex_chats, "_run_node", side_effect=fake_run),
    ):
        call_command(
            "sync_codex_chats",
            workspace=tenant["workspace"].slug,
            codex_executable="mock-codex",
            plane_base_url="http://127.0.0.1:8059",
            state_dir=str(tmp_path),
            service_config=str(service_path),
            interval=5,
            dry_run=True,
            stdout=stdout,
        )

    assert run_count == 1
    assert f'"code":"{change}"' in stdout.getvalue()


@pytest.mark.unit
def test_service_guard_checks_during_interruptible_wait(tmp_path):
    service_path = tmp_path / "service.json"
    _write_service_config(service_path)
    guard = sync_codex_chats._ServiceConfigGuard.load(service_path)

    def disable_during_sleep(_seconds):
        _write_service_config(service_path, enabled=False)

    with mock.patch.object(sync_codex_chats.time, "sleep", side_effect=disable_during_sleep) as sleep:
        assert guard.wait(30) == "disabled"

    sleep.assert_called_once()


@pytest.mark.unit
def test_status_reports_bounded_hook_diagnostics_without_event_payloads(tmp_path):
    spool_dir = tmp_path / "hook-spool"
    spool_dir.mkdir()
    hook_id = str(uuid4())
    (spool_dir / f"{hook_id}.json").write_text(
        json.dumps(
            {
                "version": 1,
                "id": hook_id,
                "source": "codex_hook",
                "event": "Stop",
                "state": "answer_available",
                "terminal": True,
                "thread_id": str(uuid4()),
                "turn_id": str(uuid4()),
                "observed_at": "2026-10-22T12:00:00.000000Z",
                "prompt": "must-not-appear",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "cursors.json").write_text(
        json.dumps(
            {
                "version": 1,
                "bindings": {
                    "codex:test": {
                        "cursor": 1,
                        "hook": {
                            "lastObservedAt": "2026-10-22T11:59:00.000000Z",
                            "lastEvent": "PostToolUse",
                            "lastState": "running",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    sync_codex_chats._write_status(
        tmp_path / "status.json",
        SimpleNamespace(slug="personal"),
        30,
        0,
        1,
        tmp_path,
    )
    content = (tmp_path / "status.json").read_text(encoding="utf-8")
    heartbeat = json.loads(content)

    assert heartbeat["hookSpoolDir"] == str(spool_dir)
    assert heartbeat["hookPendingCount"] == 1
    assert heartbeat["hookPendingTruncated"] is False
    assert heartbeat["hookNewestPendingEventAt"] == "2026-10-22T12:00:00+00:00"
    assert heartbeat["hookNewestPendingEvent"] == "Stop"
    assert heartbeat["hookNewestPendingState"] == "answer_available"
    assert heartbeat["hookLastAppliedAt"] == "2026-10-22T11:59:00+00:00"
    assert heartbeat["hookLastAppliedEvent"] == "PostToolUse"
    assert heartbeat["hookLastAppliedState"] == "running"
    assert "must-not-appear" not in content
