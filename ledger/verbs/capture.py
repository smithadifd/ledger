"""Record what the harness saw: ``ledger capture --kind prompt|picker``.

The harness's hooks call this verb; an agent never does (a guard rail denies
it). It reads exactly one hook JSON object on stdin, never argv, and writes
one item-less ``utterance.captured`` event on a reserved channel. The store
lets no other writer use a channel that starts with ``hook:``.

``--kind prompt`` takes the UserPromptSubmit hook input
(``hook_event_name`` ``UserPromptSubmit``, ``prompt``, and optionally
``session_id`` and ``prompt_id``) and writes the prompt on channel
``hook:prompt`` with provenance class ``submitted-prompt`` and
``origin: person``. A prompt over ``PROMPT_CAP_BYTES`` of UTF-8 is recorded
as a pointer: a fixed text line plus ``omitted_bytes`` and
``omitted_sha256``.

Two cases of ``--kind prompt`` write nothing and exit 0, both decided before
the database is opened, so neither creates it. The result is the text line
``skipped``, a tab and the reason (JSON ``data``:
``{"event": null, "skipped": "<reason>"}``):

- ``frame``: after its leading ASCII whitespace, the prompt starts with one of
  ``HARNESS_FRAMES``, the text a harness puts in the prompt slot for a task
  notification, a cross-session or agent message, a system reminder or a
  subagent hand-back. The match is exact and case-sensitive. It is a label,
  never proof of authorship: a prompt with no frame is stored as the
  person's, whoever wrote it.
- ``prompts_off``: ``capture_prompts`` is ``false`` in the ``config.json``
  beside the database. No file, no key, or ``true`` leaves prompt capture on.
  A config that cannot be read or parsed (a ``config.json`` symbolic link
  that does not resolve included), or a value that is not a JSON boolean, is
  refused (``capture_config``, exit 3, nothing written), so a typo never
  turns a person's ``false`` into stored prompts.

A valid input is checked first, so every refusal above still applies to a
frame. ``--kind picker`` never reads the config.

``--kind picker`` takes the PostToolUse hook input for AskUserQuestion
(``hook_event_name`` ``PostToolUse``, ``tool_name`` ``AskUserQuestion``,
``tool_use_id``, ``tool_input.questions`` and ``tool_response.answers``,
optionally ``tool_response.annotations``) and writes one event per picker
call on channel ``hook:picker`` with provenance class ``tool-answer``. Its
payload carries a ``picker`` object:

- ``display``: the whole ``questions`` array, verbatim at every level;
  ``question_count`` its length; ``display_sha256`` the SHA-256 hex of its
  canonical JSON;
- ``answers``: the tool result's answers map, verbatim (``{}`` when there
  is none); ``annotation``: its ``annotations`` map, verbatim, when present;
- for a one-question picker, ``chosen_label`` (the answer, verbatim) and
  ``chosen_position`` (the 1-based position of the one option with that
  label). No matching option sets ``typed``; more than one sets
  ``ambiguous``. A picker with more than one question singles out no answer;
- ``asked_event_id``: the ask an ``[ask K]`` tag at the very start of the
  first question names, when there is one;
- ``tool_use_id``: the tool call's id.

The text is each question with its answer (``<question>\\nchosen: <answer>``)
or, when the answers map leaves it out, ``<question>\\nunanswered``, joined by
a blank line. A retry of one tool call (the same ``tool_use_id``) returns the
stored event; a differing payload under the same id is refused.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any, Mapping

from ledger.answers import (
    ask_tag,
    fingerprint,
    picker_text,
    unanswered_text,
)
from ledger.schema import PICKER_CHANNEL, PROMPT_CHANNEL
from ledger.verbs import (
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    Verb,
    actor_kwargs,
    event_dict,
    new_key,
    open_db,
)

KINDS = ("prompt", "picker")
SUBMITTED_PROMPT = "submitted-prompt"
TOOL_ANSWER = "tool-answer"
# A prompt larger than this many bytes of UTF-8 is stored as a pointer.
PROMPT_CAP_BYTES = 64 * 1024
# The store appends a hook: capture only for a caller holding this.
_CAPTURE_WRITER_CAPABILITY = object()
# Text the harness itself puts in the prompt slot. A prompt that starts with
# one of these, after leading ASCII whitespace, is not captured.
HARNESS_FRAMES = (
    "<task-notification>",
    "<cross-session-message",
    "<agent-message",
    "<system-reminder>",
    "[Subagent hand-back]",
)
# ASCII whitespace only: a frame after a no-break space is still captured, so
# the filter fails toward capture.
_ASCII_WHITESPACE = " \t\n\r\v\f"
# The config.json key that turns prompt capture off; picker capture never reads it.
PROMPTS_CONFIG_KEY = "capture_prompts"


def _bad_input(message: str) -> CliError:
    return CliError(message, code="capture_bad_input", exit_code=EXIT_REFUSED)


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def read_hook_input(raw: str) -> dict[str, Any]:
    """One hook JSON object, or a ``capture_bad_input`` refusal."""
    try:
        hook = json.loads(raw, parse_constant=_refuse_constant)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise _bad_input(f"stdin is not JSON: {exc}") from exc
    if type(hook) is not dict:
        raise _bad_input("stdin must be one JSON object (the hook input)")
    return hook


def _utf8(text: str, name: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _bad_input(f"{name} is not UTF-8 text: {exc.reason}") from exc


def _optional_str(hook: Mapping[str, Any], name: str) -> str | None:
    value = hook.get(name)
    if value is None:
        return None
    if type(value) is not str or value == "":
        raise _bad_input(f"{name} must be a non-empty string")
    return value


def prompt_capture(hook: Mapping[str, Any]) -> dict[str, Any]:
    """The ``utterance.captured`` payload for a UserPromptSubmit hook input."""
    if hook.get("hook_event_name") != "UserPromptSubmit":
        raise CliError(
            "capture --kind prompt takes a UserPromptSubmit hook input",
            code="capture_not_prompt",
            exit_code=EXIT_REFUSED,
        )
    prompt = hook.get("prompt")
    if type(prompt) is not str or prompt == "":
        raise _bad_input("prompt must be a non-empty string")
    body = _utf8(prompt, "prompt")
    payload: dict[str, Any] = {"origin": "person"}
    if len(body) > PROMPT_CAP_BYTES:
        digest = hashlib.sha256(body).hexdigest()
        payload["text"] = f"[omitted: prompt of {len(body)} bytes, sha256 {digest}]"
        payload["omitted_bytes"] = len(body)
        payload["omitted_sha256"] = digest
    else:
        payload["text"] = prompt
    for name in ("session_id", "prompt_id"):
        value = _optional_str(hook, name)
        if value is not None:
            payload[name] = value
    return payload


def is_harness_frame(prompt: str) -> bool:
    """True when ``prompt`` starts with a harness frame after ASCII whitespace."""
    return prompt.lstrip(_ASCII_WHITESPACE).startswith(HARNESS_FRAMES)


def _config_refusal(message: str) -> CliError:
    return CliError(message, code="capture_config", exit_code=EXIT_REFUSED)


def prompts_enabled(db: str) -> bool:
    """Whether ``capture_prompts`` in the ``config.json`` beside ``db`` leaves
    prompt capture on: yes with no file, no key or ``true``; no with ``false``.

    Anything else is refused rather than read as on or off.
    """
    from ledger import search  # config.json is read in one place

    path = search.config_file(db)
    # load_config reads a link that resolves to nothing as no file, which here
    # would turn a person's ``false`` into stored prompts.
    if os.path.lexists(path) and not os.path.exists(path):
        raise _config_refusal(
            f"unreadable ledger config {path}: a symbolic link that does not resolve;"
            " prompt capture refused"
        )
    try:
        config = search.load_config(db)
    except search.SearchError as exc:
        raise _config_refusal(f"{exc}; prompt capture refused") from exc
    if PROMPTS_CONFIG_KEY not in config:
        return True
    value = config[PROMPTS_CONFIG_KEY]
    if type(value) is not bool:
        raise _config_refusal(
            f"{PROMPTS_CONFIG_KEY} in ledger config {search.config_file(db)} must be"
            " true or false; prompt capture refused"
        )
    return value


def _questions(tool_input: Any) -> list[dict[str, Any]]:
    if type(tool_input) is not dict:
        raise _bad_input("tool_input must be an object")
    questions = tool_input.get("questions")
    if type(questions) is not list or not questions:
        raise _bad_input("tool_input.questions must be a non-empty array")
    for index, question in enumerate(questions):
        where = f"tool_input.questions[{index}]"
        if type(question) is not dict:
            raise _bad_input(f"{where} must be an object")
        if type(question.get("question")) is not str or question["question"] == "":
            raise _bad_input(f"{where}.question must be a non-empty string")
        options = question.get("options")
        if type(options) is not list:
            raise _bad_input(f"{where}.options must be an array")
        for position, option in enumerate(options):
            if type(option) is not dict or type(option.get("label")) is not str:
                raise _bad_input(f"{where}.options[{position}] must be an object with a label")
    return questions


def picker_capture(hook: Mapping[str, Any]) -> dict[str, Any]:
    """The ``utterance.captured`` payload for a PostToolUse AskUserQuestion input."""
    if hook.get("hook_event_name") != "PostToolUse" or hook.get("tool_name") != "AskUserQuestion":
        raise CliError(
            "capture --kind picker takes a PostToolUse hook input for AskUserQuestion",
            code="capture_not_picker",
            exit_code=EXIT_REFUSED,
        )
    tool_use_id = hook.get("tool_use_id")
    if type(tool_use_id) is not str or tool_use_id == "":
        raise _bad_input("tool_use_id must be a non-empty string")
    questions = _questions(hook.get("tool_input"))
    response = hook.get("tool_response")
    answers: Any = {}
    annotation: Any = None
    if type(response) is dict:
        answers = response.get("answers", {})
        annotation = response.get("annotations")
    if type(answers) is not dict:
        raise _bad_input("tool_response.answers must be an object")
    for key, value in answers.items():
        if type(value) is not str:
            raise _bad_input(f"tool_response.answers[{key!r}] must be a string")
    if annotation is not None and type(annotation) is not dict:
        raise _bad_input("tool_response.annotations must be an object")
    texts = [
        picker_text(question["question"], answers[question["question"]])
        if question["question"] in answers
        else unanswered_text(question["question"])
        for question in questions
    ]
    _utf8(json.dumps(questions, ensure_ascii=False), "tool_input.questions")
    picker: dict[str, Any] = {
        "display": questions,
        "display_sha256": fingerprint(questions),
        "question_count": len(questions),
        "answers": answers,
        "typed": False,
        "ambiguous": False,
        "tool_use_id": tool_use_id,
    }
    if len(questions) == 1:
        question = questions[0]
        if question["question"] in answers:
            label = answers[question["question"]]
            picker["chosen_label"] = label
            matches = [
                position
                for position, option in enumerate(question["options"], start=1)
                if option["label"] == label
            ]
            if len(matches) == 1:
                picker["chosen_position"] = matches[0]
            elif not matches:
                picker["typed"] = True
            else:
                picker["ambiguous"] = True
    if annotation is not None:
        picker["annotation"] = annotation
    asked = ask_tag(questions[0]["question"])
    if asked is not None:
        picker["asked_event_id"] = asked
    payload: dict[str, Any] = {"text": "\n\n".join(texts), "picker": picker}
    session_id = _optional_str(hook, "session_id")
    if session_id is not None:
        payload["session_id"] = session_id
    return payload


def write_capture(
    lg,
    kind: str,
    hook: Mapping[str, Any],
    *,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
):
    """Build the capture for ``kind`` from the hook input and append it."""
    if kind == "picker":
        payload = picker_capture(hook)
        channel, provenance = PICKER_CHANNEL, TOOL_ANSWER
        key = f"capture:picker:{payload['picker']['tool_use_id']}"
    elif kind == "prompt":
        payload = prompt_capture(hook)
        channel, provenance = PROMPT_CHANNEL, SUBMITTED_PROMPT
        prompt_id = payload.get("prompt_id")
        key = f"capture:prompt:{prompt_id}" if prompt_id else f"capture:prompt:{new_key()}"
    else:
        raise _bad_input(f"unknown capture kind: {kind!r}")
    return lg._append_capture_event(
        _CAPTURE_WRITER_CAPABILITY,
        kind="utterance.captured",
        payload=payload,
        idempotency_key=key,
        channel=channel,
        provenance_class=provenance,
        actor=actor,
        harness=harness,
        model=model,
        session=session,
    )


def add_arguments(parser) -> None:
    parser.add_argument(
        "--kind",
        required=True,
        choices=KINDS,
        help="prompt (UserPromptSubmit input) or picker (PostToolUse AskUserQuestion input);"
        " the hook input is read from stdin",
    )


def _skipped(reason: str) -> Result:
    return Result(data={"event": None, "skipped": reason}, text=f"skipped\t{reason}")


def run(args, ctx: Context) -> Result:
    try:
        raw = sys.stdin.read()
    except UnicodeDecodeError as exc:
        raise _bad_input(f"stdin is not UTF-8 text: {exc}") from exc
    hook = read_hook_input(raw)
    if args.kind == "prompt":
        # Validate first, so every refusal stands; then the two skips, both
        # before open_db, which would create the database.
        prompt_capture(hook)
        if is_harness_frame(hook["prompt"]):
            return _skipped("frame")
        if not prompts_enabled(ctx.db):
            return _skipped("prompts_off")
    lg = open_db(ctx, write=True)
    try:
        event = write_capture(lg, args.kind, hook, **actor_kwargs(ctx))
    finally:
        lg.close()
    asked = (event.payload.get("picker") or {}).get("asked_event_id")
    return Result(
        data={"event": event_dict(event)},
        text=f"captured\t{event.channel}\tevent={event.id}"
        + (f"\task={asked}" if asked is not None else ""),
    )


VERB = Verb(
    name="capture",
    help="record a harness hook's input (stdin) as a prompt or picker capture; hooks only",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
