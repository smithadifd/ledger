"""Attach, show or verify an item's documents: content-addressed records.

A document is one ``doc.attached`` event on an item. ``doc add`` reads the
file's bytes once and computes their sha256 and size. It stores the bytes
inline (``content_b64``, at most ``DOC_INLINE_LIMIT``), or, with
``--by-path``, a stored path (``path``) to a file of at most
``DOC_PATH_LIMIT`` bytes under the database's document root. Every event
holds exactly one of the two.

The document root is the directory beside the database file named as the
database file plus ``.documents`` (``work.db.documents`` beside
``work.db``), so each database has its own, and the database, its sidecars,
``config.json`` and the search index stay outside it. The ledger never
creates, writes, moves or deletes anything under it; the caller puts files
there. A stored path is relative to the root, never absolute
(``ledger.schema.doc_path_problem`` is its grammar).

Every open of a file goes through one walk (``_walk``): each directory
component is opened ``O_DIRECTORY | O_NOFOLLOW`` relative to the descriptor
before it, and the leaf ``O_NOFOLLOW | O_NONBLOCK`` relative to its
parent's, so a symlink at any component, or one swapped in while the walk
runs, is refused rather than followed, and a FIFO cannot stall the open. The
leaf descriptor must be a regular file, checked before any byte is read.
Nothing else about the leaf is checked: a file with a second hard link is a
regular file, and is read like any other. A platform with no no-follow,
directory-only or descriptor-relative open is refused before any open.

``doc add FILE`` (inline) walks FILE as given, never normalised, from ``/``
or from the working directory; see ``_walk_plan`` for the grammar. ``doc add
FILE --by-path`` first places FILE under the root, component by component
(``_place_under_root``), and stores the components after the root. It then
opens the root from the database's directory without following a symlink at
the root, walks the stored path from there, and reads the bytes from that
walk's leaf. For a relative FILE it also walks FILE from the working
directory and refuses unless both walks reached the same file. Each
component of the stored path must also appear byte for byte in its parent
directory's entry list, read from the parent's descriptor, so a name a
case-insensitive volume would open under another spelling is refused rather
than stored in a second spelling.

``doc show --content`` and ``doc verify`` read an inline document's stored
bytes. They read a by-path document by walking its stored path from the root
of the database they opened, on every call, and hashing the bytes that walk
read; nothing from an earlier walk or add is reused. An absent entry is
``missing``, any other walk failure ``refused``, bytes that are not the
stored size and sha256 ``mismatch``. ``doc show`` without ``--content``, and
the context card, read events only. All three are reads and run without
``LEDGER_HARNESS``; ``doc add`` is the one write and refuses without it, as
every write verb does.

``doc add`` attaches whatever bytes FILE holds and does not vouch for where
they came from: it runs as the caller's operating-system user, so it can
attach any file that user can read, by its own path, through a hard link or
through a copy. What it guarantees is that the stored sha256 is the hash of
the bytes it read, and that no document's bytes are printed, or reported
``ok``, unless they were read in that call and hash to the stored sha256
and size. FILE given as a single hyphen reads standard input instead (inline
only); that is a convenience for piping generated text, not a safer way to
attach a file. Every filename and stored path is escaped before it reaches a
message, so a name holding a terminal control sequence is never printed raw.

Before anything is written, ``doc add`` scans the bytes it read for
credential shapes (``ledger.credscan``), inline and by path alike, and for a
by-path add also the UTF-8 bytes of the stored path, before any file is
opened. A hit in either is refused ``doc_credential_shape`` unless
``--allow-credential-shapes`` is given, which writes the event with
``credential_override: true``. The scan runs before the idempotency check,
so repeating an overridden add without the flag is refused, not reported
``unchanged``. No message carries the matched bytes: a hit in the stored
path is named by its shape and the words "in the path", and that refusal
does not name FILE.

Adding the same item, role and sha256 again writes nothing and reports
``unchanged``, whatever the storage; the idempotency key is derived from
those three, so a retried add lands once. The same bytes under another role
are a second document.

``attach_document`` is the only writer of ``doc.attached``: the store
reserves the kind, so ``ledger append`` and ``append-many`` refuse it and
every stored sha256 is one the ledger computed from the bytes it read.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ledger.credscan import describe as describe_credentials
from ledger.credscan import scan as scan_credentials
from ledger.credscan import shape_names
from ledger.restore import inline_document_bytes
from ledger.schema import (
    DOC_CREDENTIAL_OVERRIDE,
    DOC_DEFAULT_MIME,
    DOC_INLINE_LIMIT,
    DOC_KIND,
    DOC_PATH_LIMIT,
    DOC_ROLES,
    doc_path_problem,
)
from ledger.store import Event, Ledger
from ledger.verbs import (
    EXIT_INTEGRITY,
    EXIT_NOT_FOUND,
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    actor_kwargs,
    open_db,
)
from ledger.verbs.events import text_cell

ACTIONS = ("add", "show", "verify")
MARKDOWN_MIME = "text/markdown"
VERIFY_FAILED = "doc_verify_failed"
CREDENTIAL_SHAPE = "doc_credential_shape"
OUTSIDE_ROOT = "doc_outside_root"
DOC_MISMATCH = "doc_mismatch"
DOC_MISSING = "doc_missing"
DOC_REFUSED = "doc_refused"
# FILE exactly this reads standard input; a file of that name is ``./-``.
STDIN = "-"
STATUS_OK = "ok"
STATUS_MISMATCH = "mismatch"
STATUS_MISSING = "missing"
STATUS_REFUSED = "refused"
STORAGE_INLINE = "inline"
STORAGE_PATH = "path"
# The document root is the database file's name plus this, beside it.
DOCUMENTS_SUFFIX = ".documents"
_CHUNK = 1 << 16
_EVENT_ID = re.compile(r"[1-9][0-9]{0,18}")
_MAX_EVENT_ID = 2**63 - 1
_ADD_ONLY = ("file", "role", "title", "mime", "allow_credential_shapes", "by_path")
# The store appends doc.attached only for a caller holding this
# (attach_document).
_DOC_WRITER_CAPABILITY = object()


def document_key(item_id: str, role: str, sha256: str) -> str:
    """The idempotency key of a document: its item, role and sha256."""
    return f"doc:{item_id}:{role}:{sha256}"


_NOT_REGULAR = "not a readable regular file"
_THROUGH_SYMLINK = "its path goes through a symlink, which is refused; pass the real path"
_NO_NOFOLLOW = "this platform cannot open a path without following symlinks"
_NO_FD_LISTING = "this platform cannot list a directory from its descriptor"


def _unreadable(name: str, reason: str) -> CliError:
    return CliError(
        f"cannot read {text_cell(name)}: {reason}",
        code="doc_unreadable",
        exit_code=EXIT_REFUSED,
    )


# ---- the document root ----------------------------------------------------------


@dataclass(frozen=True)
class DocumentRoot:
    """A database's document root.

    ``home`` is the physical directory holding the database (the parent
    ``resolve_db_path`` returns) and ``name`` the root's own name in it.
    ``spelled_home`` is that directory as the database path was spelled
    (``~`` expanded and made absolute, not resolved), or None when the
    caller has no spelling to offer; it is used only to place an absolute
    FILE under the root at add, never to open anything.
    """

    home: str
    name: str
    spelled_home: str | None = None

    @property
    def path(self) -> str:
        """The physical root, as a message names it."""
        return os.path.join(self.home, self.name)

    def prefixes(self, absolute: bool) -> tuple[list[str], ...]:
        """The root's components, in each spelling FILE may use: the physical
        root, and for an absolute FILE also the root as the database was
        spelled."""
        physical = [*_components(self.home), self.name]
        if not absolute or self.spelled_home is None:
            return (physical,)
        return (physical, [*_components(self.spelled_home), self.name])


def document_root(db_path: Path | str, spelled_db: str | None = None) -> DocumentRoot:
    """The document root of the database at ``db_path``, the path
    ``resolve_db_path`` returned (its parent is physical). ``spelled_db`` is
    the database path as the caller gave it, for ``doc add``'s placement of
    an absolute FILE."""
    resolved = Path(db_path)
    spelled_home = None
    if spelled_db is not None:
        given = os.path.expanduser(os.fspath(spelled_db))
        spelled_home = os.path.dirname(os.path.abspath(given))
    return DocumentRoot(str(resolved.parent), resolved.name + DOCUMENTS_SUFFIX, spelled_home)


def _components(path: str) -> list[str]:
    """``path`` split on ``/``, its empty and ``.`` components dropped."""
    return [part for part in path.split("/") if part not in ("", ".")]


# ---- the walk ---------------------------------------------------------------------


def _walk_plan(name: str) -> tuple[bool, list[str], str]:
    """``(absolute, directory components, leaf)`` for FILE as given.

    FILE is split on ``/`` and never normalised. Empty components (a
    leading ``/`` or a repeated one, as in ``//a.md`` or ``a//b.md``) and
    ``.`` components are skipped. Refused ``doc_unreadable``: an empty FILE;
    a ``..`` component anywhere; and a FILE whose last component is empty or
    ``.`` (it ends in ``/`` or ``/.``, so it names a directory).
    """
    if name == "":
        raise CliError(
            "cannot read FILE: it is empty", code="doc_unreadable", exit_code=EXIT_REFUSED
        )
    parts = name.split("/")
    if ".." in parts:
        raise _unreadable(
            name, "its path holds a '..' component, which is refused; pass the real path"
        )
    leaf = parts[-1]
    if leaf in ("", "."):
        raise _unreadable(name, "it names a directory, not a file")
    directories = [part for part in parts[:-1] if part not in ("", ".")]
    return name.startswith("/"), directories, leaf


def _no_follow_flags() -> tuple[int, int] | None:
    """``(O_NOFOLLOW, O_DIRECTORY)``, or None when the platform lacks either
    flag or cannot open relative to a directory descriptor."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory or os.open not in os.supports_dir_fd:
        return None
    return nofollow, directory


def _is_symlink(part: str, dir_fd: int) -> bool:
    """Whether ``part`` under ``dir_fd`` is a symlink. Chooses a refusal's
    wording only; the refusal itself is the failed no-follow open."""
    try:
        return stat.S_ISLNK(os.stat(part, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except (OSError, ValueError, NotImplementedError):
        return False


class _WalkFailure(Exception):
    """A walk that did not reach a regular file. ``reason`` words it for a
    message; ``errno`` is the failed open's (None for a leaf that opened but
    is not a regular file, and for a name the platform cannot open).
    ``missing`` is true only for ENOENT: an absent entry."""

    def __init__(self, reason: str, error: int | None):
        super().__init__(reason)
        self.reason = reason
        self.errno = error

    @property
    def missing(self) -> bool:
        return self.errno == errno.ENOENT


def _open_component(part: str, flags: int, dir_fd: int) -> int:
    try:
        return os.open(part, flags, dir_fd=dir_fd)
    except OSError as exc:
        error = exc.errno
    except ValueError:
        error = None
    raise _WalkFailure(_THROUGH_SYMLINK if _is_symlink(part, dir_fd) else _NOT_REGULAR, error)


def _check_spelling(part: str, dir_fd: int) -> None:
    """Refuse unless ``part`` is, byte for byte, an entry of the directory
    open at ``dir_fd``, as listed from that descriptor.

    On a case-insensitive (or normalisation-insensitive) volume an open
    succeeds for a name spelled otherwise than the entry on disk, which
    would give one file two stored spellings. The listing is the disk's own
    spelling, so a name that is not in it is refused, never rewritten. A
    platform that cannot list a directory from its descriptor is refused
    too. Raises ``_WalkFailure`` (never ``missing``)."""
    if os.listdir not in os.supports_fd:
        raise _WalkFailure(_NO_FD_LISTING, None)
    try:
        names = os.listdir(dir_fd)
    except (OSError, ValueError):
        raise _WalkFailure(_NOT_REGULAR, None) from None
    if part not in names:
        raise _WalkFailure(
            f"its spelling of {text_cell(part)} differs from the name on disk; "
            "pass the name as it is spelled on disk",
            None,
        )


def _leaf_refusal(fd: int) -> str | None:
    """Why the walk's leaf is refused, or None: the ``fstat`` of the leaf
    descriptor must show a regular file. Nothing else is checked."""
    try:
        info = os.fstat(fd)
    except OSError:
        return _NOT_REGULAR
    if not stat.S_ISREG(info.st_mode):
        return _NOT_REGULAR
    return None


def _walk(
    base_fd: int,
    directories: Sequence[str],
    leaf: str,
    flags: tuple[int, int],
    *,
    exact_spelling: bool = False,
) -> int:
    """An open descriptor on ``leaf``, reached from ``base_fd`` through
    ``directories`` without following a symlink at any of them or at the
    leaf; raises ``_WalkFailure`` otherwise.

    The one walk every open of a document file uses. Each directory is opened
    ``O_RDONLY | O_DIRECTORY | O_NOFOLLOW`` relative to the descriptor
    before it, and the leaf ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK`` relative
    to its parent's; each parent is closed once its child is open, and every
    descriptor the walk opened is closed on every failure. ``base_fd`` is
    the caller's and is never closed here. The leaf descriptor's ``fstat``
    must show a regular file before it is returned (``_leaf_refusal``); no
    byte is read here. With ``exact_spelling`` (``doc add --by-path``), each
    component once opened must also appear byte for byte in its parent's
    entry list (``_check_spelling``), else the walk fails.
    """
    nofollow, directory = flags
    fd = base_fd
    try:
        for part in directories:
            child = _open_component(part, os.O_RDONLY | directory | nofollow, fd)
            if exact_spelling:
                try:
                    _check_spelling(part, fd)
                except _WalkFailure:
                    os.close(child)
                    raise
            if fd != base_fd:
                os.close(fd)
            fd = child
        leaf_fd = _open_component(
            leaf, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0), fd
        )
        verified = False
        try:
            refusal = _leaf_refusal(leaf_fd)
            if refusal is not None:
                raise _WalkFailure(refusal, None)
            if exact_spelling:
                _check_spelling(leaf, fd)
            verified = True
            return leaf_fd
        finally:
            if not verified:
                os.close(leaf_fd)
    finally:
        if fd != base_fd:
            os.close(fd)


def _open_root(root: DocumentRoot, flags: tuple[int, int]) -> int:
    """An open descriptor on the document root: the home opened by its
    absolute path ``O_RDONLY | O_DIRECTORY`` (trusted, as the working
    directory is for a relative FILE), then the root opened from the home's
    descriptor ``O_RDONLY | O_DIRECTORY | O_NOFOLLOW``, so a symlinked root
    is refused. Raises ``_WalkFailure``."""
    nofollow, directory = flags
    try:
        home_fd = os.open(root.home, os.O_RDONLY | directory)
    except OSError as exc:
        raise _WalkFailure(_NOT_REGULAR, exc.errno) from None
    except ValueError:
        raise _WalkFailure(_NOT_REGULAR, None) from None
    try:
        return _open_component(root.name, os.O_RDONLY | directory | nofollow, home_fd)
    finally:
        os.close(home_fd)


def _open_stored(
    root: DocumentRoot, stored: str, flags: tuple[int, int], *, exact_spelling: bool = False
) -> int:
    """An open descriptor on a stored path's leaf, walked from the root's
    descriptor (``_walk``, with ``exact_spelling`` at add). Raises
    ``_WalkFailure``."""
    parts = stored.split("/")
    root_fd = _open_root(root, flags)
    try:
        return _walk(root_fd, parts[:-1], parts[-1], flags, exact_spelling=exact_spelling)
    finally:
        os.close(root_fd)


def _require_flags(name: str) -> tuple[int, int]:
    flags = _no_follow_flags()
    if flags is None:
        raise _unreadable(name, _NO_NOFOLLOW)
    return flags


def _open_by_walk(name: str) -> int:
    """An open descriptor on the leaf of FILE, reached without following a
    symlink at any component.

    The walk starts from a descriptor on ``/`` (absolute FILE) or ``.``
    (relative FILE); the starting directory itself is not checked. The
    grammar of FILE is ``_walk_plan``'s, and the walk is ``_walk``'s. A
    platform with no no-follow, directory-only or descriptor-relative open
    is refused before any open. Every failure is ``doc_unreadable``.
    """
    absolute, directories, leaf = _walk_plan(name)
    flags = _require_flags(name)
    try:
        fd = os.open("/" if absolute else ".", os.O_RDONLY | flags[1])
    except OSError as exc:
        raise _unreadable(name, _NOT_REGULAR) from exc
    try:
        return _walk(fd, directories, leaf, flags)
    except _WalkFailure as failure:
        raise _unreadable(name, failure.reason) from None
    finally:
        os.close(fd)


def _read_at_most(fd: int, limit: int) -> bytes:
    """What ``fd`` reads, stopping once it holds more than ``limit`` bytes."""
    data = bytearray()
    while len(data) <= limit:
        chunk = os.read(fd, min(_CHUNK, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _read_descriptor(name: str, fd: int, limit: int) -> bytes:
    """At most ``limit`` plus one bytes from ``fd``; a read error is
    ``doc_unreadable`` naming FILE escaped."""
    try:
        return _read_at_most(fd, limit)
    except OSError as exc:
        raise CliError(
            f"cannot read {text_cell(name)}: {text_cell(exc.strerror or str(exc))}",
            code="doc_unreadable",
            exit_code=EXIT_REFUSED,
        ) from exc


def _display_name(name: str) -> str:
    """FILE as a message names it: escaped, and ``standard input`` for ``-``."""
    return "standard input" if name == STDIN else text_cell(name)


def _read_stdin() -> bytes:
    """Standard input's binary stream, read until it ends or holds more than
    ``DOC_INLINE_LIMIT`` bytes; over the limit is a usage error."""
    stream = getattr(sys.stdin, "buffer", None) if sys.stdin is not None else None
    if stream is None:
        raise CliError(
            "cannot read standard input: it is not a binary stream",
            code="doc_unreadable",
            exit_code=EXIT_REFUSED,
        )
    data = bytearray()
    try:
        while len(data) <= DOC_INLINE_LIMIT:
            chunk = stream.read(DOC_INLINE_LIMIT + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        raise CliError(
            f"cannot read standard input: {text_cell(reason)}",
            code="doc_unreadable",
            exit_code=EXIT_REFUSED,
        ) from exc
    if len(data) > DOC_INLINE_LIMIT:
        raise UsageError(
            f"standard input is over the {DOC_INLINE_LIMIT}-byte inline limit; "
            "standard input has no path, so it cannot be stored by path"
        )
    return bytes(data)


def _read_input(name: str) -> bytes:
    """The bytes of the file named on the command line, read once, for an
    inline document.

    FILE exactly ``-`` reads standard input (``_read_stdin``); a file whose
    name is a single hyphen is passed as ``./-``. Any other FILE is opened
    by ``_open_by_walk``, so refused ``doc_unreadable`` when any component of
    the path as given is a symlink or ``..``, when it ends in ``/``, when it
    is empty, and when it is not a readable regular file (missing, a
    directory, a FIFO); the walk checks the leaf before any byte is read.
    The bytes are read from the walk's leaf descriptor. A file over
    ``DOC_INLINE_LIMIT`` is a usage error that names ``--by-path``. The name
    is escaped in every refusal.
    """
    if name == STDIN:
        return _read_stdin()
    fd = _open_by_walk(name)
    try:
        data = _read_descriptor(name, fd, DOC_INLINE_LIMIT)
    finally:
        os.close(fd)
    if len(data) > DOC_INLINE_LIMIT:
        raise UsageError(
            f"{text_cell(name)} is over the {DOC_INLINE_LIMIT}-byte inline limit; "
            "put it under the document root and pass --by-path to store it by path"
        )
    return data


# ---- adding by path ---------------------------------------------------------------


def _place_under_root(
    name: str, root: DocumentRoot, absolute: bool, components: list[str]
) -> list[str]:
    """FILE's components after the document root, matched component by
    component and character for character, never by string prefix.

    An absolute FILE's components must begin with the root's in one of its
    two spellings (``DocumentRoot.prefixes``); a relative FILE's, after the
    working directory's (physical, as the operating system reports it), must
    begin with the physical root's. Anything else is refused
    ``doc_outside_root``, and FILE naming the root itself ``doc_unreadable``
    (it names a directory). Nothing is opened here.
    """
    if absolute:
        full = components
    else:
        try:
            cwd = os.getcwd()
        except OSError as exc:
            raise _unreadable(name, "the working directory cannot be read") from exc
        full = [*_components(cwd), *components]
    for prefix in root.prefixes(absolute):
        if full[: len(prefix)] == prefix:
            rest = full[len(prefix):]
            if not rest:
                raise _unreadable(name, "it names the document root, a directory, not a file")
            return rest
    raise CliError(
        f"cannot add {text_cell(name)} by path: it is not under the document root "
        f"{text_cell(root.path)}; a by-path document must be a file under the "
        "directory beside the database named after it with the .documents suffix",
        code=OUTSIDE_ROOT,
        exit_code=EXIT_REFUSED,
    )


def _path_credentials(stored: str) -> tuple:
    """The credential shapes (``ledger.credscan``) in a stored path's UTF-8
    bytes."""
    return scan_credentials(stored.encode("utf-8"))


def _read_by_path(
    name: str, root: DocumentRoot, *, allow_credential_shapes: bool = False
) -> tuple[str, bytes]:
    """``(stored path, bytes)`` for ``doc add FILE --by-path``.

    In order: FILE's grammar (``_walk_plan``; a name that is not UTF-8 has
    no stored spelling and is refused too), its placement under the root
    (``_place_under_root``), the credential scan of the stored path, and the
    stored path's grammar; all before any open. A credential shape in the
    stored path is refused ``doc_credential_shape`` unless
    ``allow_credential_shapes`` is set; the refusal names the shapes and says
    they are in the path, and names neither FILE nor the path. A platform
    with no no-follow opens is refused next. A relative FILE is then walked
    from the working directory (descriptor A). The stored path is walked
    from the root's descriptor (descriptor B), each component checked
    against the spelling on disk (``_check_spelling``); for a relative FILE,
    A and B must be the same file (``st_dev`` and ``st_ino``), else it
    changed while it was being added. The bytes are read from B, at most
    ``DOC_PATH_LIMIT`` plus one; over the limit is a usage error. Every walk
    failure is ``doc_unreadable`` naming FILE escaped, and every descriptor
    is closed on every path.
    """
    if name == STDIN:
        raise UsageError("doc add --by-path needs a FILE: standard input has no path to store")
    absolute, directories, leaf = _walk_plan(name)
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise _unreadable(name, "its name is not UTF-8, so it has no stored spelling") from None
    stored = "/".join(_place_under_root(name, root, absolute, [*directories, leaf]))
    path_hits = _path_credentials(stored)
    if path_hits and not allow_credential_shapes:
        raise CliError(
            "the path to store holds a credential shape "
            f"({shape_names(path_hits)} in the path); the path is not shown, and "
            "nothing was written. Pass --allow-credential-shapes to attach it anyway",
            code=CREDENTIAL_SHAPE,
            exit_code=EXIT_REFUSED,
        )
    problem = doc_path_problem(stored)
    if problem is not None:
        raise _unreadable(name, f"its path under the document root {problem}")
    flags = _require_flags(name)
    given_fd = None if absolute else _open_by_walk(name)
    try:
        try:
            fd = _open_stored(root, stored, flags, exact_spelling=True)
        except _WalkFailure as failure:
            raise _unreadable(name, failure.reason) from None
        try:
            if given_fd is not None:
                given, walked = os.fstat(given_fd), os.fstat(fd)
                if (given.st_dev, given.st_ino) != (walked.st_dev, walked.st_ino):
                    raise _unreadable(name, "it changed while it was being added")
                os.close(given_fd)
                given_fd = None
            data = _read_descriptor(name, fd, DOC_PATH_LIMIT)
        finally:
            os.close(fd)
    finally:
        if given_fd is not None:
            os.close(given_fd)
    if len(data) > DOC_PATH_LIMIT:
        raise UsageError(f"{text_cell(name)} is over the {DOC_PATH_LIMIT}-byte by-path limit")
    return stored, data


def default_mime(name: str) -> str:
    return MARKDOWN_MIME if name.lower().endswith(".md") else DOC_DEFAULT_MIME


def attach_document(
    lg: Ledger,
    item_id: str,
    name: str,
    *,
    role: str,
    mime: str | None = None,
    title: str | None = None,
    allow_credential_shapes: bool = False,
    by_path: bool = False,
    spelled_db: str | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> tuple[Event, bool]:
    """Read ``name``, hash it, and attach it to ``item_id``: its bytes
    inline, or with ``by_path`` its stored path under ``lg``'s document root
    (``spelled_db`` is the database path as the caller spelled it; see
    ``_read_by_path``).

    Returns ``(event, created)``: the new event and True, or the stored
    document with the same item, role and sha256, whatever its storage, and
    False (nothing is written). The item check and the duplicate check run
    inside the write transaction.

    The bytes read are scanned for credential shapes first, before the
    duplicate check and before any write, whatever the storage; a by-path
    add also scans the stored path's UTF-8 bytes, before any open
    (``_read_by_path``). A hit in the bytes is refused
    ``doc_credential_shape``, naming FILE and each shape with its byte
    offset but never the matched bytes; a hit in the stored path is refused
    with the same code, naming the shapes as in the path and not naming FILE
    or the path. With ``allow_credential_shapes`` neither is refused and the
    event carries ``credential_override: true``. The flag on bytes and a
    path with no hit writes no marker.
    """
    stored: str | None = None
    path_hits: tuple = ()
    if by_path:
        stored, data = _read_by_path(
            name,
            document_root(lg.path, spelled_db),
            allow_credential_shapes=allow_credential_shapes,
        )
        path_hits = _path_credentials(stored)
    else:
        data = _read_input(name)
    hits = scan_credentials(data)
    if hits and not allow_credential_shapes:
        raise CliError(
            f"{_display_name(name)} holds a credential shape "
            f"({describe_credentials(hits)}); nothing was written. Pass "
            "--allow-credential-shapes to attach it anyway",
            code=CREDENTIAL_SHAPE,
            exit_code=EXIT_REFUSED,
        )
    sha256 = hashlib.sha256(data).hexdigest()
    payload: dict[str, Any] = {
        "role": role,
        "sha256": sha256,
        "size": len(data),
        "mime": mime if mime is not None else default_mime(name),
    }
    if stored is not None:
        payload["path"] = stored
    else:
        payload["content_b64"] = base64.b64encode(data).decode("ascii")
    if title is not None:
        payload["title"] = title
    if hits or path_hits:
        payload[DOC_CREDENTIAL_OVERRIDE] = True
    with lg.transaction():
        if lg.item(item_id) is None:
            raise CliError(
                f"item not found: {item_id}", code="not_found", exit_code=EXIT_NOT_FOUND
            )
        for event in lg.events(item_id=item_id, kind=DOC_KIND):
            if event.payload.get("role") == role and event.payload.get("sha256") == sha256:
                return event, False
        event = lg._append_doc_event(
            _DOC_WRITER_CAPABILITY,
            kind=DOC_KIND,
            payload=payload,
            item_id=item_id,
            idempotency_key=document_key(item_id, role, sha256),
            actor=actor,
            harness=harness,
            model=model,
            session=session,
        )
    return event, True


# ---- reading ------------------------------------------------------------------------


def _is_by_path(event: Event) -> bool:
    return "path" in event.payload


def document_row(event: Event) -> dict[str, Any]:
    """One document as ``doc show --json`` prints it."""
    payload = event.payload
    by_path = _is_by_path(event)
    return {
        "event_id": event.id,
        "item_id": event.item_id,
        "role": payload.get("role"),
        "title": payload.get("title"),
        "mime": payload.get("mime"),
        "size": payload.get("size"),
        "sha256": payload.get("sha256"),
        "storage": STORAGE_PATH if by_path else STORAGE_INLINE,
        "path": payload.get("path") if by_path else None,
        "credential_override": payload.get(DOC_CREDENTIAL_OVERRIDE) is True,
    }


def _inline_status(event: Event) -> tuple[str, bytes | None]:
    """``ok`` and the stored inline bytes when they hash to the stored
    sha256 and size; ``mismatch`` and None when they do not, or do not
    decode."""
    payload = event.payload
    try:
        data = inline_document_bytes(payload)
    except ValueError:
        return STATUS_MISMATCH, None
    if hashlib.sha256(data).hexdigest() != payload.get("sha256"):
        return STATUS_MISMATCH, None
    if len(data) != payload.get("size"):
        return STATUS_MISMATCH, None
    return STATUS_OK, data


def _path_status(event: Event, root: DocumentRoot) -> tuple[str, bytes | None]:
    """A by-path document's status and, only when ``ok``, the bytes this
    call read: its stored path walked from ``root`` now, never cached.

    ``missing`` when an open failed with ENOENT (the root, a directory or
    the leaf is absent); ``refused`` for any other walk failure (a symlink
    at the root or below it, a non-regular leaf such as a FIFO, a platform
    with no no-follow opens), a read error, or a stored path or size the
    schema would refuse; ``mismatch`` when the bytes read (at most the
    stored size plus one) are not exactly the stored size or do not hash to
    the stored sha256.
    """
    payload = event.payload
    stored = payload.get("path")
    size = payload.get("size")
    if doc_path_problem(stored) is not None or type(size) is not int or size < 0:
        return STATUS_REFUSED, None
    flags = _no_follow_flags()
    if flags is None:
        return STATUS_REFUSED, None
    try:
        fd = _open_stored(root, stored, flags)
    except _WalkFailure as failure:
        return (STATUS_MISSING if failure.missing else STATUS_REFUSED), None
    try:
        data = _read_at_most(fd, size)
    except OSError:
        return STATUS_REFUSED, None
    finally:
        os.close(fd)
    if len(data) != size or hashlib.sha256(data).hexdigest() != payload.get("sha256"):
        return STATUS_MISMATCH, None
    return STATUS_OK, data


def document_status(event: Event, root: DocumentRoot) -> tuple[str, bytes | None]:
    """``(status, bytes)``: the bytes only when the status is ``ok``. An
    inline document is checked against its stored bytes; a by-path one
    against the file its stored path reaches under ``root`` in this call."""
    if _is_by_path(event):
        return _path_status(event, root)
    return _inline_status(event)


def verify_status(event: Event, root: DocumentRoot) -> str:
    """``ok``, ``mismatch``, ``missing`` or ``refused`` (``document_status``)."""
    return document_status(event, root)[0]


def verified_content(event: Event, root: DocumentRoot) -> bytes:
    """The document's bytes, only after they hash to its stored sha256 and
    size; otherwise refused (exit 4) and no bytes are returned:
    ``doc_mismatch``, ``doc_missing`` or ``doc_refused`` by status."""
    status, data = document_status(event, root)
    if status == STATUS_OK and data is not None:
        return data
    if not _is_by_path(event):
        raise CliError(
            f"document {event.id}: its inline bytes do not hash to the stored "
            "sha256; nothing printed",
            code=DOC_MISMATCH,
            exit_code=EXIT_INTEGRITY,
        )
    where = (
        f"document {event.id}: its stored path {text_cell(event.payload.get('path'))} "
        f"under {text_cell(root.path)}"
    )
    if status == STATUS_MISSING:
        message, code = f"{where} is missing; nothing printed", DOC_MISSING
    elif status == STATUS_REFUSED:
        message, code = (
            f"{where} was refused: it goes through a symlink, is not a regular "
            "file, or cannot be opened without following symlinks; nothing printed",
            DOC_REFUSED,
        )
    else:
        message, code = (
            f"{where} does not hold bytes that hash to the stored sha256 and size; "
            "nothing printed",
            DOC_MISMATCH,
        )
    raise CliError(message, code=code, exit_code=EXIT_INTEGRITY)


# ---- the verb -------------------------------------------------------------------------


def add_arguments(parser) -> None:
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("item", nargs="?", help="the item (verify: optional)")
    parser.add_argument(
        "file",
        nargs="?",
        help=f"add: the file to attach (inline: at most {DOC_INLINE_LIMIT} bytes, "
        "no symlink anywhere in its path; --by-path: a file under the document "
        "root); - reads standard input (inline only)",
    )
    parser.add_argument("--role", choices=DOC_ROLES, help="add: what the document is")
    parser.add_argument("--title", help="add: a one-line title")
    parser.add_argument(
        "--mime",
        help=f"add: the media type (default {DOC_DEFAULT_MIME}; {MARKDOWN_MIME} for .md)",
    )
    parser.add_argument(
        "--allow-credential-shapes",
        action="store_true",
        help="add: attach bytes holding a known credential shape, marking the "
        "document credential_override",
    )
    parser.add_argument(
        "--by-path",
        action="store_true",
        help=f"add: store FILE's path under the document root (DB.documents beside "
        f"the database) instead of its bytes (at most {DOC_PATH_LIMIT} bytes); "
        "show --content and verify re-read it",
    )
    parser.add_argument("--id", dest="event_id", help="show, verify: one document by event id")
    parser.add_argument(
        "--content",
        action="store_true",
        help="show: write that document's verified bytes to stdout (needs --id)",
    )


def _parse_event_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    if _EVENT_ID.fullmatch(raw) is None or int(raw) > _MAX_EVENT_ID:
        raise UsageError(f"--id must be an event id such as 7, got {raw!r}")
    return int(raw)


def _refuse_flags(args, names: tuple[str, ...], action: str) -> None:
    given = [name for name in names if getattr(args, name) not in (None, False)]
    if given:
        flags = ", ".join(
            "FILE" if name == "file" else "--" + name.replace("_", "-") for name in given
        )
        raise UsageError(f"doc {action} does not take {flags}")


def _run_add(args, ctx: Context) -> Result:
    # The verb's one write: it needs the writer identity, refused exactly as
    # the CLI refuses a write verb without it. show and verify are reads, so
    # the verb is not ``writes`` and this action checks for itself.
    if not ctx.harness:
        raise CliError(
            "LEDGER_HARNESS is required for writes; set it, e.g. "
            "LEDGER_HARNESS=claude-code",
            code="missing_harness",
            exit_code=EXIT_REFUSED,
        )
    _refuse_flags(args, ("event_id", "content"), "add")
    if args.item is None or args.file is None:
        raise UsageError("doc add needs ITEM and FILE")
    if args.role is None:
        raise UsageError(f"doc add needs --role ({', '.join(DOC_ROLES)})")
    if args.by_path and args.file == STDIN:
        raise UsageError("doc add --by-path needs a FILE: standard input has no path to store")
    lg = open_db(ctx, write=True)
    try:
        event, created = attach_document(
            lg,
            args.item,
            args.file,
            role=args.role,
            mime=args.mime,
            title=args.title,
            allow_credential_shapes=args.allow_credential_shapes,
            by_path=args.by_path,
            spelled_db=ctx.db,
            **actor_kwargs(ctx),
        )
    finally:
        lg.close()
    row = document_row(event)
    text = f"doc\t{args.item}\t{row['role']}\t{row['storage']}\t" + (
        f"event={event.id}" if created else f"unchanged event={event.id}"
    )
    if row["credential_override"]:
        text += "\tcredential-override"
    return Result(
        data={
            "item_id": args.item,
            "unchanged": not created,
            "credential_override": row["credential_override"],
            "document": row,
        },
        text=text,
    )


def _documents(lg: Ledger, item_id: str | None, event_id: int | None) -> list[Event]:
    if item_id is not None and lg.item(item_id) is None:
        raise CliError(f"item not found: {item_id}", code="not_found", exit_code=EXIT_NOT_FOUND)
    events = lg.events(item_id=item_id, kind=DOC_KIND)
    if event_id is None:
        return events
    chosen = [event for event in events if event.id == event_id]
    if not chosen:
        where = f"item {item_id} has" if item_id is not None else "the ledger has"
        raise CliError(
            f"{where} no document {event_id}", code="not_found", exit_code=EXIT_NOT_FOUND
        )
    return chosen


def item_documents(lg: Ledger, item_id: str) -> list[Event]:
    """The item's documents, oldest first, read as ``doc show`` reads
    them; refused ``not_found`` when the item does not exist. Events only:
    no document file is opened."""
    return _documents(lg, item_id, None)


def _show_line(row: dict[str, Any]) -> str:
    """One document as ``doc show`` prints it; the stored path and title are
    escaped (``text_cell``), so neither splits the line or a column. A
    by-path document's storage column ``path`` is followed by its stored
    path."""
    fields = [
        str(row["event_id"]),
        str(row["role"]),
        str(row["size"]),
        str(row["sha256"])[:12],
        str(row["storage"]),
    ]
    if row["storage"] == STORAGE_PATH:
        fields.append(text_cell(row["path"]))
    if row["title"] is not None:
        fields.append(text_cell(row["title"]))
    if row["credential_override"]:
        fields.append("credential-override")
    return "\t".join(fields)


def _run_show(args, ctx: Context) -> Result:
    _refuse_flags(args, _ADD_ONLY, "show")
    if args.item is None:
        raise UsageError("doc show needs ITEM")
    event_id = _parse_event_id(args.event_id)
    if args.content and event_id is None:
        raise UsageError("doc show --content needs --id N")
    lg = open_db(ctx, write=False)
    try:
        events = _documents(lg, args.item, event_id)
        root = document_root(lg.path)
    finally:
        lg.close()
    if args.content:
        [event] = events
        data = verified_content(event, root)
        document = {**document_row(event), "content_b64": base64.b64encode(data).decode("ascii")}
        return Result(data={"document": document}, raw=data)
    rows = [document_row(event) for event in events]
    text = "\n".join(_show_line(row) for row in rows) or f"no documents for {args.item}"
    return Result(data={"item_id": args.item, "documents": rows}, text=text)


def _run_verify(args, ctx: Context) -> Result:
    _refuse_flags(args, _ADD_ONLY + ("content",), "verify")
    event_id = _parse_event_id(args.event_id)
    lg = open_db(ctx, write=False)
    try:
        events = _documents(lg, args.item, event_id)
        root = document_root(lg.path)
    finally:
        lg.close()
    rows = [
        {
            "event_id": event.id,
            "item_id": event.item_id,
            "sha256": event.payload.get("sha256"),
            "status": verify_status(event, root),
        }
        for event in events
    ]
    failed = [row for row in rows if row["status"] != STATUS_OK]
    data = {"documents": rows}
    if failed:
        lines = [f"doc verify failed: {len(failed)} of {len(rows)} documents"]
        lines.extend(
            f"{row['event_id']}\t{row['status']}\t{row['item_id']}" for row in failed
        )
        return Result(
            data=data,
            text="\n".join(lines),
            exit_code=EXIT_INTEGRITY,
            error_code=VERIFY_FAILED,
        )
    return Result(data=data, text=f"ok documents={len(rows)}")


def run(args, ctx: Context) -> Result:
    if args.action == "add":
        return _run_add(args, ctx)
    if args.action == "show":
        return _run_show(args, ctx)
    return _run_verify(args, ctx)


VERB = Verb(
    name="doc",
    help="add, show or verify an item's content-addressed documents",
    add_arguments=add_arguments,
    run=run,
    # show and verify are reads (the mods pane reads documents without a
    # harness); add is the one write and checks LEDGER_HARNESS itself.
    writes=False,
)
