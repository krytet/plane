# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import hashlib
import json
import os
import re
import socket
import subprocess
import time
from datetime import timedelta
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from django.conf import settings
from django.core.management import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from plane.db.models import APIToken, IssueChatBinding, ProjectMember, User, Workspace, WorkspaceMember
from plane.utils.codex_sync import TOKEN_LABEL, codex_sync_bot_username

CODEX_PROTOCOL_VERSION = "0.163.0-alpha.11"
TOKEN_LIFETIME = timedelta(days=90)
TOKEN_REFRESH_WINDOW = timedelta(days=30)
BOT_ROLE = 15
NODE_TIMEOUT_SECONDS = 30 * 60
SERVICE_CONFIG_POLL_SECONDS = 1.0
NODE_ENV_ALLOWLIST = frozenset(
    name.casefold()
    for name in (
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "WINDIR",
        "COMSPEC",
        "SystemDrive",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "TEMP",
        "TMP",
        "TZ",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERNAME",
        "USERDOMAIN",
        "NODE_EXTRA_CA_CERTS",
    )
)
THREAD_UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
CODEX_URL_RE = re.compile(
    r"^codex://threads/[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _repository_root():
    base_directory = Path(settings.BASE_DIR).resolve()
    for candidate in (base_directory, *base_directory.parents):
        if (candidate / "dev" / "chat-sync" / "src" / "cli.ts").is_file():
            return candidate
    if len(base_directory.parents) > 2:
        return base_directory.parents[2]
    return base_directory.parent


def _cli_path():
    return _repository_root() / "dev" / "chat-sync" / "src" / "cli.ts"


def _is_loopback_host(host):
    normalized = (host or "").strip().strip("[]").lower()
    if not normalized:
        return True
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        pass

    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)}
    except OSError:
        return False
    if not addresses:
        return False
    try:
        return all(ip_address(address.split("%", 1)[0]).is_loopback for address in addresses)
    except ValueError:
        return False


def _normalize_loopback_url(value):
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise CommandError("--plane-base-url must be a valid loopback HTTP(S) URL.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not _is_loopback_host(parsed.hostname)
    ):
        raise CommandError("--plane-base-url must target a loopback Plane instance.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CommandError("--plane-base-url must include a valid port.") from exc
    hostname = parsed.hostname.lower()
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), netloc, path, "", "", ""))


def _assert_local_database():
    database = settings.DATABASES["default"]
    if database.get("ENGINE", "").endswith("sqlite3"):
        return
    if not _is_loopback_host(database.get("HOST")):
        raise CommandError("sync_codex_chats only runs against a loopback database.")


def _assert_state_directory(value):
    state_directory = Path(value).expanduser().resolve()
    repository_root = _repository_root()
    if state_directory == repository_root or repository_root in state_directory.parents:
        raise CommandError("--state-dir must be outside the Plane repository.")
    return state_directory


def _assert_external_path(value, option):
    path = Path(value).expanduser().resolve()
    repository_root = _repository_root()
    if path == repository_root or repository_root in path.parents:
        raise CommandError(f"{option} must be outside the Plane repository.")
    return path


def _safe_child_environment(token=None):
    environment = {key: value for key, value in os.environ.items() if key.casefold() in NODE_ENV_ALLOWLIST}
    if token is not None:
        environment["PLANE_API_KEY"] = token
    return environment


class _ServiceConfigGuard:
    def __init__(self, path, digest):
        self.path = path
        self.digest = digest

    @classmethod
    def load(cls, path):
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise CommandError("The service configuration could not be read.") from exc
        try:
            config = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CommandError("The service configuration is invalid.") from exc
        if not isinstance(config, dict) or config.get("enabled") is not True:
            raise CommandError("The service configuration must have enabled=true.")
        return cls(path, hashlib.sha256(content).hexdigest())

    def check(self):
        try:
            content = self.path.read_bytes()
        except OSError:
            return "removed"
        if hashlib.sha256(content).hexdigest() == self.digest:
            return None
        try:
            config = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "changed"
        if isinstance(config, dict) and config.get("enabled") is False:
            return "disabled"
        return "changed"

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            reason = self.check()
            if reason is not None:
                return reason
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.check()
            time.sleep(min(SERVICE_CONFIG_POLL_SECONDS, remaining))


def _assert_codex_compatible(executable):
    try:
        result = subprocess.run(
            [executable, "--version"],
            shell=False,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=_safe_child_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError("The configured Codex executable could not be verified.") from exc

    if result.returncode != 0 or result.stdout.strip() != f"codex-cli {CODEX_PROTOCOL_VERSION}":
        raise CommandError(f"The configured Codex executable must report codex-cli {CODEX_PROTOCOL_VERSION}.")


def _active_codex_bindings(workspace):
    return (
        IssueChatBinding.objects.filter(
            workspace=workspace,
            provider=IssueChatBinding.Provider.CODEX,
            deleted_at__isnull=True,
            project__deleted_at__isnull=True,
            issue__deleted_at__isnull=True,
        )
        .select_related("project", "issue", "link")
        .order_by("id")
    )


def _target_from_binding(workspace, binding):
    thread_id = (binding.thread_id or "").strip().lower()
    if not THREAD_UUID_V7_RE.fullmatch(thread_id):
        return None

    native_url = f"codex://threads/{thread_id}"
    link = binding.link if binding.link is not None and binding.link.deleted_at is None else None
    url = (link.url or "").strip() if link is not None else native_url
    if not url:
        url = native_url
    if url.lower().startswith("codex:") and (not CODEX_URL_RE.fullmatch(url) or url.lower() != native_url):
        return None

    target = {
        "provider": "codex",
        "threadId": thread_id,
        "workspaceSlug": workspace.slug,
        "projectId": str(binding.project_id),
        "issueId": str(binding.issue_id),
        "url": url,
    }
    if link is not None and link.title:
        target["title"] = link.title
    return target


def _refresh_targets(workspace):
    bindings = list(_active_codex_bindings(workspace))
    targets = []
    skipped = 0
    for binding in bindings:
        target = _target_from_binding(workspace, binding)
        if target is None:
            skipped += 1
            continue
        targets.append(target)
    return targets, skipped, len(bindings)


def _config_payload(workspace, targets, codex_executable, plane_base_url, state_directory, interval):
    return {
        "enabled": True,
        "planeBaseUrl": plane_base_url,
        "stateFile": str(state_directory / "cursors.json"),
        "pollIntervalSeconds": interval,
        "codexExecutable": codex_executable,
        "progressMode": "structured_or_summary",
        "threads": targets,
    }


def _write_config(config_path, payload):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    try:
        with temporary_path.open("x", encoding="utf-8", newline="\n") as file_handle:
            json.dump(payload, file_handle, indent=2, ensure_ascii=False)
            file_handle.write("\n")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, config_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _write_status(status_path, workspace, interval, exit_code, thread_count):
    status_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = status_path.with_name(f".{status_path.name}.{os.getpid()}.tmp")
    payload = {
        "pid": os.getpid(),
        "updated_at": timezone.now().isoformat(),
        "workspace": workspace.slug,
        "intervalSeconds": interval,
        "exitCode": exit_code,
        "threadCount": thread_count,
    }
    try:
        with temporary_path.open("x", encoding="utf-8", newline="\n") as file_handle:
            json.dump(payload, file_handle, separators=(",", ":"))
            file_handle.write("\n")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, status_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _bot_identity(workspace):
    suffix = workspace.id.hex
    return (
        codex_sync_bot_username(workspace.id),
        f"plane-codex-progress-sync-{suffix}@local.invalid",
    )


def _ensure_local_bot(workspace, project_ids):
    username, email = _bot_identity(workspace)
    target_project_ids = sorted({str(project_id) for project_id in project_ids})

    with transaction.atomic():
        bot = User.objects.filter(username=username).first()
        if bot is None:
            bot = User(
                username=username,
                email=email,
                display_name=TOKEN_LABEL,
                first_name="Plane local Codex",
                last_name="progress sync",
                is_bot=True,
                is_active=True,
            )
            bot.set_unusable_password()
            bot.save()
        elif not bot.is_bot:
            raise CommandError("The deterministic sync bot username belongs to a non-bot account.")
        else:
            updates = []
            if not bot.is_active:
                bot.is_active = True
                updates.append("is_active")
            if bot.email != email:
                bot.email = email
                updates.append("email")
            if updates:
                bot.save(update_fields=[*updates, "updated_at"])

        workspace_member, _ = WorkspaceMember.objects.get_or_create(
            workspace=workspace,
            member=bot,
        )
        if workspace_member.role != BOT_ROLE or not workspace_member.is_active:
            workspace_member.role = BOT_ROLE
            workspace_member.is_active = True
            workspace_member.save(update_fields=["role", "is_active", "updated_at"])

        ProjectMember.objects.filter(workspace=workspace, member=bot).exclude(project_id__in=target_project_ids).update(
            is_active=False, updated_at=timezone.now()
        )

        for project_id in target_project_ids:
            project_member, _ = ProjectMember.objects.get_or_create(
                workspace=workspace,
                project_id=project_id,
                member=bot,
            )
            if project_member.role != BOT_ROLE or not project_member.is_active:
                project_member.role = BOT_ROLE
                project_member.is_active = True
                project_member.save(update_fields=["role", "is_active", "updated_at"])

    return bot


def _provision_bot_token(bot, workspace):
    now = timezone.now()
    target_expiry = now + TOKEN_LIFETIME
    tokens = APIToken.objects.filter(
        user=bot,
        workspace=workspace,
        label=TOKEN_LABEL,
        is_active=True,
    ).order_by("-created_at")

    token = None
    for candidate in tokens:
        if candidate.expired_at is None or candidate.expired_at > now:
            token = candidate
            break

    if token is None:
        APIToken.objects.filter(
            user=bot,
            workspace=workspace,
            label=TOKEN_LABEL,
        ).update(is_active=False, updated_at=now)
        token = APIToken.objects.create(
            label=TOKEN_LABEL,
            description="Local Codex progress synchronization for Plane",
            is_active=True,
            user=bot,
            user_type=1,
            workspace=workspace,
            expired_at=target_expiry,
        )
    elif token.expired_at is None or token.expired_at < now + TOKEN_REFRESH_WINDOW:
        token.expired_at = target_expiry
        token.save(update_fields=["expired_at", "updated_at"])

    return token.token


def _revoke_local_bot_access(workspace):
    username, _ = _bot_identity(workspace)
    with transaction.atomic():
        bot = User.objects.filter(username=username, is_bot=True).first()
        if bot is None:
            return False
        now = timezone.now()
        APIToken.objects.filter(
            user=bot,
            workspace=workspace,
            is_active=True,
        ).update(is_active=False, updated_at=now)
        WorkspaceMember.objects.filter(
            workspace=workspace,
            member=bot,
            is_active=True,
        ).update(is_active=False, updated_at=now)
        ProjectMember.objects.filter(
            workspace=workspace,
            member=bot,
            is_active=True,
        ).update(is_active=False, updated_at=now)
    return True


def _redact(value, token):
    return value.replace(token, "[redacted]") if token else value


def _run_node(config_path, token, dry_run):
    cli_path = _cli_path()
    if not cli_path.is_file():
        raise CommandError("The local chat-sync CLI is missing.")

    command = ["node", str(cli_path), "--config", str(config_path), "--once"]
    if dry_run:
        command.append("--dry-run")
    try:
        result = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            timeout=NODE_TIMEOUT_SECONDS,
            check=False,
            env=_safe_child_environment(token),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError("The local chat-sync process could not complete.") from exc

    stdout = _redact(result.stdout, token)
    stderr = _redact(result.stderr, token)
    fatal_codes = set()
    for line in (stdout + "\n" + stderr).splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "fatal" and isinstance(event.get("code"), str):
            fatal_codes.add(event["code"])
    return result.returncode, stdout, stderr, fatal_codes


class _AdvisoryFileLock:
    def __init__(self, path):
        self.path = path
        self.file_handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file_handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file_handle.write(b"\0")
            self.file_handle.flush()
        self.file_handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file_handle.close()
            self.file_handle = None
            raise CommandError("Another sync_codex_chats worker holds the state lock.") from exc
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.file_handle is None:
            return
        try:
            self.file_handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file_handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.file_handle.close()
            self.file_handle = None


class Command(BaseCommand):
    help = "Run the local Codex chat progress adapter using existing Plane chat bindings."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", required=True, help="Workspace slug")
        parser.add_argument("--codex-executable", required=True, help="Exact Codex CLI executable path")
        parser.add_argument(
            "--plane-base-url",
            required=True,
            help="Loopback Plane base URL, for example http://127.0.0.1:8059",
        )
        parser.add_argument("--state-dir", required=True, help="Local state directory outside the repository")
        parser.add_argument("--once", action="store_true", help="Run one cycle")
        parser.add_argument("--interval", type=int, default=30, help="Seconds between cycles")
        parser.add_argument("--dry-run", action="store_true", help="Read and summarize without Plane writes")
        parser.add_argument(
            "--service-config",
            help="External control file that requests a graceful stop when disabled or changed",
        )

    def handle(self, *args, **options):
        plane_base_url = _normalize_loopback_url(options["plane_base_url"])
        state_directory = _assert_state_directory(options["state_dir"])
        if options["interval"] < 5 or options["interval"] > 3_600:
            raise CommandError("--interval must be between 5 and 3600 seconds.")
        _assert_local_database()

        service_guard = None
        if options.get("service_config"):
            service_config_path = _assert_external_path(
                options["service_config"],
                "--service-config",
            )
            service_guard = _ServiceConfigGuard.load(service_config_path)

        workspace = Workspace.objects.filter(slug=options["workspace"], deleted_at__isnull=True).first()
        if workspace is None:
            raise CommandError("The requested workspace does not exist.")

        codex_executable = str(Path(options["codex_executable"]).expanduser())
        _assert_codex_compatible(codex_executable)

        config_path = state_directory / "config.json"
        status_path = state_directory / "status.json"
        lock_path = state_directory / "sync.lock"
        dry_run = options["dry_run"]

        with _AdvisoryFileLock(lock_path):
            while True:
                if service_guard is not None:
                    service_stop = service_guard.check()
                    if service_stop is not None:
                        self.stdout.write(
                            json.dumps(
                                {"event": "service_config", "code": service_stop},
                                separators=(",", ":"),
                            )
                        )
                        return

                targets, skipped, active_count = _refresh_targets(workspace)
                if skipped:
                    self.stderr.write(f"Skipped {skipped} binding(s) with unsupported thread identity.")
                if not targets:
                    exit_code = 1 if active_count else 0
                    if active_count == 0 and not dry_run:
                        _revoke_local_bot_access(workspace)
                    _write_status(status_path, workspace, options["interval"], exit_code, 0)
                    self.stdout.write(json.dumps({"event": "no_targets"}, separators=(",", ":")))
                    if options["once"]:
                        if exit_code:
                            raise CommandError("Active bindings could not be synchronized.")
                        return
                    if service_guard is not None:
                        service_stop = service_guard.wait(options["interval"])
                        if service_stop is not None:
                            self.stdout.write(
                                json.dumps(
                                    {"event": "service_config", "code": service_stop},
                                    separators=(",", ":"),
                                )
                            )
                            return
                    else:
                        time.sleep(options["interval"])
                    continue

                token = None
                if not dry_run:
                    project_ids = {target["projectId"] for target in targets}
                    bot = _ensure_local_bot(workspace, project_ids)
                    token = _provision_bot_token(bot, workspace)

                payload = _config_payload(
                    workspace,
                    targets,
                    codex_executable,
                    plane_base_url,
                    state_directory,
                    options["interval"],
                )
                _write_config(config_path, payload)

                return_code, stdout, stderr, fatal_codes = _run_node(config_path, token, dry_run)
                cycle_exit_code = return_code or (1 if skipped else 0)
                _write_status(
                    status_path,
                    workspace,
                    options["interval"],
                    cycle_exit_code,
                    len(targets),
                )
                if stdout:
                    self.stdout.write(stdout.rstrip())
                if stderr:
                    self.stderr.write(stderr.rstrip())

                if fatal_codes:
                    raise CommandError("Chat sync stopped fail-closed: " + ", ".join(sorted(fatal_codes)))
                if cycle_exit_code != 0:
                    self.stderr.write("Chat sync cycle completed with target errors.")
                    if options["once"]:
                        raise CommandError("Chat sync cycle completed with target errors.")

                if options["once"]:
                    return
                if service_guard is not None:
                    service_stop = service_guard.wait(options["interval"])
                    if service_stop is not None:
                        self.stdout.write(
                            json.dumps(
                                {"event": "service_config", "code": service_stop},
                                separators=(",", ":"),
                            )
                        )
                        return
                else:
                    time.sleep(options["interval"])
