#!/usr/bin/env python3
"""Install the Plane chat-sync lifecycle hook without replacing existing hooks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENTS = (
    "UserPromptSubmit",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
    "Interrupt",
)
HOOK_OWNER = "plane-codex-sync"
HOOK_FILENAME = "plane_chat_sync_hook.py"
STATUS_MESSAGE = "Recording Plane chat status"


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _default_service_config() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data or not local_app_data.strip():
        raise ValueError("LOCALAPPDATA is required to locate the plane-chat-sync service configuration")
    return Path(local_app_data).expanduser() / "plane-chat-sync" / "service.json"


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _command(interpreter: Path, script: Path, spool_dir: Path) -> str:
    arguments = [
        str(interpreter),
        str(script),
        "--spool-dir",
        str(spool_dir),
        "--hook-owner",
        HOOK_OWNER,
    ]
    if os.name == "nt":
        return subprocess.list2cmdline(arguments)
    return " ".join(_shell_quote(argument) for argument in arguments)


def _shell_quote(value: str) -> str:
    if value and all(character.isalnum() or character in "/._-:" for character in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _managed_handler(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    for field in ("command", "commandWindows"):
        command = handler.get(field)
        if isinstance(command, str) and f"--hook-owner {HOOK_OWNER}" in command:
            return True
    return False


def _handler(interpreter: Path, script: Path, spool_dir: Path) -> dict[str, Any]:
    command = _command(interpreter, script, spool_dir)
    return {
        "type": "command",
        "command": command,
        "commandWindows": command,
        "timeout": 3,
        "statusMessage": STATUS_MESSAGE,
    }


def merge_hooks_config(
    config: dict[str, Any],
    interpreter: Path,
    script: Path,
    spool_dir: Path,
) -> tuple[dict[str, Any], bool]:
    """Add one managed handler per event while preserving every unmanaged entry."""
    merged = json.loads(json.dumps(config))
    hooks = merged.get("hooks")
    if hooks is None:
        hooks = {}
        merged["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json must contain an object-valued hooks field")

    expected = _handler(interpreter, script, spool_dir)
    changed = False
    for event in EVENTS:
        groups = hooks.get(event)
        if groups is None:
            groups = []
            hooks[event] = groups
        if not isinstance(groups, list):
            raise ValueError(f"hooks.json event {event} must be an array")

        preserved_groups: list[Any] = []
        managed_count = 0
        kept_exact = False
        for group in groups:
            if not isinstance(group, dict):
                preserved_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                preserved_groups.append(group)
                continue

            if (
                not kept_exact
                and group.get("matcher") is None
                and len(handlers) == 1
                and handlers[0] == expected
                and _managed_handler(handlers[0])
            ):
                managed_count += 1
                kept_exact = True
                preserved_groups.append(group)
                continue

            preserved_handlers = []
            for handler in handlers:
                if _managed_handler(handler):
                    managed_count += 1
                else:
                    preserved_handlers.append(handler)

            if preserved_handlers:
                updated_group = dict(group)
                updated_group["hooks"] = preserved_handlers
                preserved_groups.append(updated_group)
            elif not handlers:
                preserved_groups.append(group)

        if managed_count == 1 and kept_exact and preserved_groups == groups:
            continue

        if not kept_exact:
            preserved_groups.append({"hooks": [expected]})
        hooks[event] = preserved_groups
        changed = True

    return merged, changed


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return raw


def resolve_spool_dir(
    *,
    service_config: Path | None,
    spool_dir: Path | None,
) -> Path:
    repository = _repository_root()
    explicit = spool_dir.expanduser().resolve() if spool_dir else None
    if explicit and _is_within(explicit, repository):
        raise ValueError("--spool-dir must stay outside the Plane repository")

    service_path = service_config.expanduser().resolve() if service_config else None
    if service_path and service_path.is_file():
        service = _read_json(service_path)
        state_value = service.get("stateDir")
        if (
            not isinstance(state_value, str)
            or not state_value.strip()
            or not Path(state_value).expanduser().is_absolute()
        ):
            raise ValueError("service.json requires an absolute stateDir")
        state_dir = Path(state_value).expanduser().resolve()
        if _is_within(state_dir, repository):
            raise ValueError("service.json stateDir must stay outside the Plane repository")
        derived = state_dir / "hook-spool"
        if explicit and explicit != derived:
            raise ValueError("--spool-dir must match service.json stateDir/hook-spool")
        return derived

    if explicit:
        return explicit
    raise ValueError("Provide --service-config with stateDir or an explicit --spool-dir")


def _fsync_directory(directory: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = None
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _backup(path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = path.with_name(f"{path.name}.backup-{timestamp}")
    shutil.copy2(path, candidate)
    return candidate


def install(
    *,
    hooks_path: Path,
    install_dir: Path,
    spool_dir: Path | None,
    service_config: Path | None = None,
    interpreter: Path,
    apply: bool,
) -> dict[str, Any]:
    source = Path(__file__).with_name("hook_handler.py").resolve()
    hooks_path = hooks_path.expanduser().resolve()
    install_dir = install_dir.expanduser().resolve()
    spool_dir = resolve_spool_dir(service_config=service_config, spool_dir=spool_dir)
    interpreter = interpreter.expanduser().resolve()

    if not interpreter.is_file():
        raise ValueError("--interpreter must be an existing executable file")

    config = _read_json(hooks_path) if hooks_path.is_file() else {"hooks": {}}
    script_path = install_dir / HOOK_FILENAME
    merged, config_changed = merge_hooks_config(config, interpreter, script_path, spool_dir)
    serialized = (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    script_bytes = source.read_bytes()
    script_changed = not script_path.is_file() or script_path.read_bytes() != script_bytes
    config_bytes = hooks_path.read_bytes() if hooks_path.is_file() else None
    hooks_changed = config_changed or config_bytes != serialized

    result: dict[str, Any] = {
        "apply": apply,
        "hooks_path": str(hooks_path),
        "script_path": str(script_path),
        "spool_dir": str(spool_dir),
        "config_changed": hooks_changed,
        "script_changed": script_changed,
        "backup_path": None,
    }
    if not apply:
        return result

    if script_changed:
        if script_path.is_file():
            _backup(script_path)
        _atomic_write(script_path, script_bytes, 0o600)
    if hooks_changed:
        if hooks_path.is_file():
            result["backup_path"] = str(_backup(hooks_path))
        _atomic_write(hooks_path, serialized, 0o600)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write files; without this flag only print a plan")
    parser.add_argument("--hooks-json", type=Path, default=_default_codex_home() / "hooks.json")
    parser.add_argument("--install-dir", type=Path, default=_default_codex_home() / "hooks")
    parser.add_argument("--service-config", type=Path, default=_default_service_config())
    parser.add_argument("--spool-dir", type=Path)
    parser.add_argument("--interpreter", type=Path, default=Path(sys.executable))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = install(
            hooks_path=arguments.hooks_json,
            install_dir=arguments.install_dir,
            spool_dir=arguments.spool_dir,
            service_config=arguments.service_config,
            interpreter=arguments.interpreter,
            apply=arguments.apply,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    mode = "apply" if arguments.apply else "dry-run"
    print(f"Plane chat-sync hook {mode}:")
    print(f"  script: {result['script_path']}")
    print(f"  hooks:  {result['hooks_path']}")
    print(f"  spool:  {result['spool_dir']}")
    print(f"  changed: hooks={str(result['config_changed']).lower()} script={str(result['script_changed']).lower()}")
    if result["backup_path"]:
        print(f"  backup: {result['backup_path']}")
    if not arguments.apply:
        print("No files were changed. Re-run with --apply.")
    else:
        print("Open /hooks once to review and trust the added hook definitions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
