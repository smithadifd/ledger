"""Each session's model, recorded beside the database by the plugin's hooks.

Claude Code can switch a session's model at any time, and only a few hook
events may write the session's environment file, so ``LEDGER_MODEL`` cannot
follow a switch. The plugin's SessionStart and PostModelSwitch hooks run
``ledger resume --hook-model``, which records the session's current model in
one small file per session, and every CLI write reads its own session's file
when it builds its context.

Where: with ``P`` the resolved database path, the folder is
``P.parent / (P.name + ".session-models")`` (named for the database file, so
two databases in one folder keep separate records), and the file for session
``S`` is ``sha256(S).hexdigest() + ".json"`` in it.

What: one UTF-8 JSON object of at most 4096 bytes with exactly the keys
``session``, ``state``, ``model``, ``event`` and ``seq``. ``state`` is one of:

- ``"model"``, with ``model`` a usable name (a non-empty string of at most
  256 UTF-8 bytes with no control character): writes stamp that model;
- ``"unknown"``, with ``model`` null: the session switched to a model the hook
  could not name, so writes stamp no model, not ``LEDGER_MODEL``;
- ``"env"``, with ``model`` null: a session start that named no model, so
  writes stamp ``LEDGER_MODEL``.

``event`` is ``SessionStart`` or ``PostModelSwitch``, and ``seq`` a
non-negative integer that orders hook runs (a monotonic clock reading).

Reading (:func:`resolve_model`) never raises, warns, prints, creates or
changes anything. A record that is missing, unreadable, malformed, oversized,
a symbolic link, not a regular file, or for another session is ignored and
the write falls back to ``LEDGER_MODEL``.

Writing (:func:`record_hook_model`) is a compare-and-swap under an exclusive
``flock`` on ``<hash>.lock``: a PostModelSwitch record lands only when its
``seq`` is newer than the stored one, a SessionStart record always lands. The
new content goes to a temporary file that is then renamed over the record,
so a reader sees the old record or the new one, never a mix. The whole write
must finish within two seconds of the hook's start, or it writes nothing.

Platforms: the record needs ``fcntl.flock`` and opens that never follow a
symbolic link, relative to a folder descriptor. Where any of these is missing
(Windows), the module still imports, so every other command works; writes
stamp ``LEDGER_MODEL`` and the hook refuses with ``hook_model_unsupported``.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
import unicodedata
import warnings
from pathlib import Path
from typing import Any

from ledger.paths import resolve_db_path

try:
    import fcntl
except ImportError:  # not POSIX (Windows): see SUPPORTED
    fcntl = None  # type: ignore[assignment]

FOLDER_SUFFIX = ".session-models"
MAX_RECORD_BYTES = 4096
MAX_MODEL_BYTES = 256
# The hook's budget, from its first statement: Claude Code waits at most five
# seconds for a PostModelSwitch hook before it sends the next request.
HOOK_BUDGET_NS = 2_000_000_000
STATES = ("model", "unknown", "env")
EVENTS = ("SessionStart", "PostModelSwitch")
RECORD_KEYS = frozenset({"session", "state", "model", "event", "seq"})
_LOCK_POLL_SECONDS = 0.01
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# Whether this platform can keep the record safely: flock, opens that never
# follow a symbolic link, and opens relative to a folder descriptor. Without
# them the reader reads nothing and the hook refuses.
SUPPORTED = bool(
    fcntl is not None
    and _NOFOLLOW
    and _DIRECTORY
    and _NONBLOCK
    and {os.open, os.unlink, os.rename} <= os.supports_dir_fd
)
_FOLDER_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC


class HookModelRefusal(Exception):
    """The hook wrote nothing new; ``code`` names why."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def stamp() -> int:
    """The ordering key and budget clock: monotonic nanoseconds."""
    clock = getattr(time, "clock_gettime_ns", None)
    if clock is None:  # not POSIX: the hook refuses there, after this stamp
        return time.monotonic_ns()
    return clock(time.CLOCK_MONOTONIC)


def session_models_dir(resolved_db: Path) -> Path:
    return resolved_db.parent / (resolved_db.name + FOLDER_SUFFIX)


def session_key(session: str) -> str:
    return hashlib.sha256(session.encode("utf-8")).hexdigest()


def record_name(session: str) -> str:
    return session_key(session) + ".json"


def lock_name(session: str) -> str:
    return session_key(session) + ".lock"


def _is_control(ch: str) -> bool:
    # C0, DEL and C1 are category Cc; U+2028 and U+2029 are Zl and Zp.
    return unicodedata.category(ch) in ("Cc", "Zl", "Zp")


def usable_model(value: object) -> str | None:
    """``value`` when it is a usable model name, else None."""
    if type(value) is not str or value == "" or any(_is_control(ch) for ch in value):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from a JSON escape
        return None
    if len(encoded) > MAX_MODEL_BYTES:
        return None
    return value


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def parse_record(data: bytes, session: str) -> dict[str, Any] | None:
    """The record in ``data`` when it is valid and for ``session``, else None."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        obj = json.loads(text, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        return None
    if type(obj) is not dict:
        return None
    if obj.get("session") != session:
        return None
    if set(obj) != RECORD_KEYS:
        return None
    seq = obj["seq"]
    if type(seq) is not int or seq < 0:
        return None
    if obj["event"] not in EVENTS:
        return None
    state, model = obj["state"], obj["model"]
    if state == "model":
        if usable_model(model) is None:
            return None
    elif state in ("unknown", "env"):
        if model is not None:
            return None
    else:
        return None
    return obj


def _read_at(dir_fd: int, session: str) -> dict[str, Any] | None:
    try:
        fd = os.open(record_name(session), _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        chunks = []
        size = 0
        while size <= MAX_RECORD_BYTES:
            chunk = os.read(fd, MAX_RECORD_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > MAX_RECORD_BYTES:
        return None
    return parse_record(data, session)


def read_session_model(resolved_db: Path, session: str) -> dict[str, Any] | None:
    """``session``'s valid record beside ``resolved_db``, else None."""
    try:
        dir_fd = os.open(session_models_dir(resolved_db), _FOLDER_FLAGS)
    except OSError:
        return None
    try:
        return _read_at(dir_fd, session)
    finally:
        os.close(dir_fd)


def resolve_model(db: str | Path, session: str | None, env_model: str | None) -> str | None:
    """The model a write by ``session`` to ``db`` stamps.

    The session's own record beside the database wins; with none (or none
    usable) it is ``env_model``; an ``"unknown"`` record stamps no model.
    On a platform that cannot keep the record it is always ``env_model``.
    """
    if session is None or not SUPPORTED:
        return env_model
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            resolved = resolve_db_path(db)
    except Exception:
        # The database open refuses this path properly later, if the verb
        # opens it at all.
        return env_model
    record = read_session_model(resolved, session)
    if record is None:
        return env_model
    if record["state"] == "model":
        return record["model"]
    if record["state"] == "unknown":
        return None
    return env_model


def record_bytes(session: str, state: str, model: str | None, event: str, seq: int) -> bytes:
    return json.dumps(
        {"session": session, "state": state, "model": model, "event": event, "seq": seq},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")


def _open_folder(resolved_db: Path) -> int:
    if not resolved_db.parent.is_dir():
        raise HookModelRefusal(
            "hook_model_no_folder", "the database's folder does not exist"
        )
    folder = session_models_dir(resolved_db)
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise HookModelRefusal(
            "hook_model_write_failed", f"cannot create the session-models folder: {exc.strerror}"
        ) from exc
    try:
        return os.open(folder, _FOLDER_FLAGS)
    except OSError as exc:
        raise HookModelRefusal(
            "hook_model_unsafe_folder",
            "the session-models folder is a symbolic link or not a folder",
        ) from exc


def _lock(dir_fd: int, session: str, deadline: int) -> int:
    fd = os.open(
        lock_name(session),
        os.O_RDWR | os.O_CREAT | _NOFOLLOW | _NONBLOCK | _CLOEXEC,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HookModelRefusal("hook_model_write_failed", "the lock file is not a regular file")
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if stamp() >= deadline:
                    raise HookModelRefusal(
                        "hook_model_timeout", "the model record's lock was busy past the budget"
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
    except BaseException:
        os.close(fd)
        raise


def _publish(dir_fd: int, session: str, content: bytes, deadline: int) -> None:
    tmp = f".{session_key(session)}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600,
        dir_fd=dir_fd,
    )
    published = False
    try:
        try:
            view = memoryview(content)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        if stamp() >= deadline:
            raise HookModelRefusal(
                "hook_model_timeout", "the model record was not published within the budget"
            )
        os.replace(tmp, record_name(session), src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        published = True
    finally:
        if not published:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass


def record_hook_model(
    db: str | Path,
    session: str,
    state: str,
    model: str | None,
    event: str,
    seq: int,
) -> bool:
    """Record ``session``'s model beside ``db``; False when ``seq`` is stale.

    A SessionStart record always lands; a PostModelSwitch record lands only
    when no valid record for the session holds a ``seq`` at least as new.
    Raises :class:`HookModelRefusal` (nothing new written) when the folder is
    missing or unsafe, the write fails, the two-second budget from ``seq``
    runs out, or the platform cannot keep the record. Never opens the
    database.
    """
    if not SUPPORTED:
        raise HookModelRefusal(
            "hook_model_unsupported",
            "this platform cannot lock the model record or open it without"
            " following symbolic links",
        )
    deadline = seq + HOOK_BUDGET_NS
    content = record_bytes(session, state, model, event, seq)
    if len(content) > MAX_RECORD_BYTES:
        raise HookModelRefusal("hook_model_bad_input", "session_id is too long to record")
    resolved = resolve_db_path(db)
    dir_fd = _open_folder(resolved)
    try:
        try:
            lock_fd = _lock(dir_fd, session, deadline)
        except OSError as exc:
            raise HookModelRefusal(
                "hook_model_write_failed", f"cannot lock the model record: {exc.strerror}"
            ) from exc
        try:
            if event != "SessionStart":
                current = _read_at(dir_fd, session)
                if current is not None and seq <= current["seq"]:
                    return False
            _publish(dir_fd, session, content, deadline)
            return True
        except OSError as exc:
            raise HookModelRefusal(
                "hook_model_write_failed", f"cannot write the model record: {exc.strerror}"
            ) from exc
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
    finally:
        os.close(dir_fd)
