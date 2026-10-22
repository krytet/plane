#!/usr/bin/env python3
"""Spool metadata-only Codex lifecycle observations for the Plane chat sync daemon."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

MAX_INPUT_BYTES = 64 * 1024
POST_TOOL_HEARTBEAT_SECONDS = 30
HOOK_OWNER = "plane-codex-sync"
EVENT_STATES = {
    "UserPromptSubmit": "running",
    "PermissionRequest": "needs_action",
    "PostToolUse": "running",
    "Stop": "answer_available",
    "Interrupt": "needs_action",
}
TERMINAL_EVENTS = frozenset({"Interrupt"})
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _uuid(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64 or not UUID_RE.fullmatch(value.strip()):
        return None
    return str(uuid.UUID(value.strip())).lower()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _event_fingerprint(event: str, thread_id: str, turn_id: str, now: datetime, payload: dict[str, Any]) -> str:
    identity = f"{event}:{thread_id}:{turn_id}:{_timestamp(now)}"
    if event in {"PermissionRequest", "PostToolUse"}:
        discriminator = None
        for key in ("tool_use_id", "request_id"):
            value = payload.get(key)
            if isinstance(value, str) and 0 < len(value) <= 128:
                discriminator = f"{key}:{value}"
                break
        if discriminator is None:
            tool_name = payload.get("tool_name")
            if isinstance(tool_name, str) and 0 < len(tool_name) <= 128:
                discriminator = f"tool_name:{tool_name}"
        if discriminator is not None:
            identity = f"{identity}:{hashlib.sha256(discriminator.encode('utf-8')).hexdigest()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"plane-codex-sync:{identity}"))


def observation_from_payload(payload: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """Return a bounded lifecycle record without retaining payload text."""
    if not isinstance(payload, dict):
        return None

    event = payload.get("hook_event_name")
    state = EVENT_STATES.get(event) if isinstance(event, str) else None
    thread_id = _uuid(payload.get("session_id"))
    turn_id = _uuid(payload.get("turn_id"))
    if state is None or thread_id is None or turn_id is None:
        return None

    observed_at = now or _utc_now()
    return {
        "version": 1,
        "id": _event_fingerprint(event, thread_id, turn_id, observed_at, payload),
        "source": "codex_hook",
        "event": event,
        "state": state,
        "terminal": event in TERMINAL_EVENTS,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "observed_at": _timestamp(observed_at),
    }


def read_payload(stream: IO[bytes]) -> Any | None:
    """Read at most MAX_INPUT_BYTES and reject malformed or oversized JSON."""
    try:
        raw = stream.read(MAX_INPUT_BYTES + 1)
        if not raw or len(raw) > MAX_INPUT_BYTES:
            return None
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


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


def write_observation(spool_dir: Path, observation: dict[str, Any]) -> None:
    """Write one observation with fsync and same-directory atomic rename."""
    spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    event_id = observation["id"]
    target = spool_dir / f"{event_id}.json"
    temporary = spool_dir / f".{event_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    content = json.dumps(observation, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"

    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(spool_dir)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def observe(payload: Any, spool_dir: Path, now: datetime | None = None) -> bool:
    """Validate and spool one observation. Invalid input is a successful no-op."""
    observed_at = now or _utc_now()
    observation = observation_from_payload(payload, observed_at)
    if observation is None:
        return False
    if observation["event"] == "Stop":
        try:
            _write_resume_marker(
                spool_dir,
                observation["thread_id"],
                observation["turn_id"],
                observed_at,
            )
        except BaseException:
            pass
    resume_after_stop = observation["event"] == "PostToolUse" and _resume_marker_exists(
        spool_dir,
        observation["thread_id"],
        observation["turn_id"],
    )
    if observation["event"] == "PostToolUse":
        if _posttool_rate_limited(
            spool_dir,
            observation["thread_id"],
            observation["turn_id"],
            observed_at,
            force=resume_after_stop,
        ):
            return False
    write_observation(spool_dir, observation)
    if resume_after_stop:
        _clear_resume_marker(spool_dir, observation["thread_id"], observation["turn_id"])
    return True


def _resume_marker(spool_dir: Path, thread_id: str, turn_id: str) -> Path:
    return spool_dir / ".state" / f"resume-{thread_id}-{turn_id}.json"


def _write_resume_marker(spool_dir: Path, thread_id: str, turn_id: str, now: datetime) -> None:
    path = _resume_marker(spool_dir, thread_id, turn_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    content = json.dumps({"observed_at": _timestamp(now)}, separators=(",", ":")).encode("ascii")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _resume_marker_exists(spool_dir: Path, thread_id: str, turn_id: str) -> bool:
    try:
        return _resume_marker(spool_dir, thread_id, turn_id).is_file()
    except OSError:
        return False


def _clear_resume_marker(spool_dir: Path, thread_id: str, turn_id: str) -> None:
    try:
        _resume_marker(spool_dir, thread_id, turn_id).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _posttool_rate_limited(
    spool_dir: Path,
    thread_id: str,
    turn_id: str,
    now: datetime,
    *,
    force: bool = False,
) -> bool:
    marker_directory = spool_dir / ".state"
    marker_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker_path = marker_directory / f"posttool-{thread_id}-{turn_id}.json"
    descriptor = os.open(marker_path, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(descriptor, "r+b")
    locked = False
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            return True

        handle.seek(0)
        raw = handle.read()
        try:
            previous = json.loads(raw.decode("utf-8")).get("observed_at")
            previous_at = datetime.fromisoformat(str(previous).replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            previous_at = None
        current_at = now.astimezone(timezone.utc)
        if previous_at is not None and not force:
            if previous_at.tzinfo is None:
                previous_at = previous_at.replace(tzinfo=timezone.utc)
            if 0 <= (current_at - previous_at.astimezone(timezone.utc)).total_seconds() < POST_TOOL_HEARTBEAT_SECONDS:
                return True

        content = json.dumps({"observed_at": _timestamp(current_at)}, separators=(",", ":")).encode("ascii")
        handle.seek(0)
        handle.truncate()
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        return False
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _option(argv: list[str], name: str) -> str | None:
    for index, argument in enumerate(argv):
        if argument == name and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith(f"{name}="):
            return argument.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None, stdin: IO[bytes] | None = None) -> int:
    """Command-hook entrypoint. It never emits output or blocks Codex on failure."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    spool_value = _option(arguments, "--spool-dir")
    if not spool_value:
        return 0

    spool_dir = Path(spool_value).expanduser()
    if not spool_dir.is_absolute():
        return 0

    payload = read_payload(stdin or sys.stdin.buffer)
    if payload is None:
        return 0

    try:
        observe(payload, spool_dir.resolve())
    except BaseException:
        pass
    return 0


def _silent_entrypoint() -> None:
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            original_stdout = sys.stdout
            original_stderr = sys.stderr
            try:
                sys.stdout = sink
                sys.stderr = sink
                main()
            except BaseException:
                pass
            finally:
                sys.stdout = original_stdout
                sys.stderr = original_stderr
    except BaseException:
        pass


if __name__ == "__main__":
    _silent_entrypoint()
