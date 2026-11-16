# CARD-ESCAPE-NOTES1: escape and refuse control characters in item text

Status: design revision 4, for review before build. Line numbers are at main
`bb2cb09`. Revision 4 makes these changes:

- The `spec diff` header's item id is escaped.
- A field absent from a spec version (None) stays absent in the diff.
- The `export-item --ticket` file-locator text is escaped.
- Context splits spec text on LF only.
- T11 also checks the JSON diff lines.

Spelling in this file: a test input is a Python literal in double quotes
(`"\x1b"` is one ESC code point). An expected output is a Python raw
literal (`r"\u001b"` is the six visible characters backslash, u, 0, 0, 1,
b). Printed output must hold the raw-literal text and never the code point.

## 1. Problem

`ledger card`, `ledger spec show` and the other item printers print stored text
exactly as it is stored. A note holding ESC `]52;c;aGk=` BEL writes the
operator's clipboard (OSC 52); ESC `[2J` clears the screen. Item text comes
from outside the machine (PR text, tracker content relayed by seats), so under
the alpha threat model an attacker can reach it. The card's `--md` file gets
the same bytes (`ledger/verbs/card.py:38`, written at `:45-51`).

Print sites in `ledger/card.py` that are raw today:

| Site | Stored text printed raw |
|---|---|
| `_fmt_notes` `:769-771` (Notes `:1084`, Progress `:891`) | `item.note.text` (copied by `_note_row` `:1504-1505`); `item.progress.text` (`_progress_row` `:1557-1559` collapses whitespace only) |
| `_render_card` `:1045-1052` | item id, type, title, status, owner, claim |
| `_render_card` `:1074` | next step (carries blocker names, claimant, chosen option) |
| `_fmt_decisions` `:603-611` | quote_ref, question, chosen |
| `_fmt_questions` `:749-758`, `_option_lines` `:712-721` | question, ask, option label and description, recommendation, default, effort, added, unblocks |
| `_fmt_links` `:762-766` | rel, target (link text) |
| `_fmt_blockers` `:817-818` (Blockers `:1081`, skills `:894`, refs `:895`) | blocker names, skills, refs |
| `_fmt_records` `:821-844` | standing, who, who_kind, relayed_by, phase, verbatim, effect, tag type and id, evidence, result |
| `_fmt_tried` `:847-854`, `_fmt_reviews` `:857-869`, `_fmt_stop` `:872-875`, `_fmt_pointers` `:878-885` | what, outcome, verdict, findings, phase, point, pointer ref and note |
| `recover_command` `:618-622`, `footer_line` `:625-627`, `children_command` `:640-643`, `duplicates_command` `:646-648`, `blockers_command` `:651-655`, `derive_next_step` `:405` | item id inside `shlex.join`, which quotes the id but leaves ESC in it |

`_fmt_children` `:791-797` (child id, title, status, holder) already escapes
through `_one_line` `:777-788`, but that escaper does not catch bidi controls.

Other print sites:

- **`spec show` and `spec diff`** (`ledger/verbs/spec.py`). `_show_text`
  `:238-250` prints the item id `:240`, title `:241`, size, merge class, source
  and source_rev `:243-245`, acceptance `:248` and body `:249` raw.
  `diff_fields` `:257-274` splits raw values with `splitlines()` (`_lines`
  `:253-254`). That consumes U+2028, U+2029, NEL, VT, FF and U+001C-U+001E as
  line breaks. `_run_diff` prints the header at `:388`, which interpolates the
  item id raw, and the lines at `:389-393`.
- **`export-item --ticket`** (`ledger/verbs/export_item.py`). There are two
  text forms:
  - The file-locator form prints the ticket section that `item_ticket_text`
    (`:116`, `ledger/export.py:1109`) read from a committed repository file.
    It prints raw at `:135`, and the same text goes to `data.ticket`.
    Committed repository content arrives from outside, so it is in scope.
  - The spec fallback: `_spec_text` `:97-107` prints the title `:97`, body
    `:103` and acceptance `:105` raw, and the result carries the same text as
    `data.ticket` `:127-130`.
- **`ledger context`** (`ledger/verbs/context.py`). `fit` joins every line raw
  into the text at `:429`. The lines come from:
  - the header (`header_lines` `:378-385`: item id, title);
  - the spec title and the body and acceptance lines (`_spec_lines`
    `:169-192`). These are split with `splitlines()` at `:186`, which consumes
    U+2028, U+2029 and U+0085 before any escaping could see them;
  - rows built by `_flat` `:158-159` and `_clip` `:162-166`: notes `:261`,
    decisions, questions, documents, and related items `:319-328`.

  `_flat` and `_clip` call `str.split()` first, which collapses whitespace
  (TAB included) but leaves ESC, BEL and the bidi controls in place.
- **`ledger search`** (`ledger/verbs/search.py`). `_text` `:80-88` prints the
  card id and title `:83`, the snippet `:85` (note or body text) and the caveat
  `:87` raw.
- **Item listings.** `format_items` (`ledger/verbs/__init__.py:282-286`) prints
  the item id, type, status and title raw, for `query`
  (`ledger/verbs/query.py:136`) and `ready` (`ledger/verbs/ready.py:39`).

**Write paths.** Every writer reaches `Ledger._append` (`ledger/store.py:905`):
append, append-many, `new` (sourced or not), `note`, the loop writer, and
`set_spec` through `_append_spec_event` (`ledger/store.py:801-810`). Its
`checked_precondition` calls `refuse_display_control_characters(kind,
payload_obj)` at `ledger/store.py:987` for every writer, internal ones
included, after the idempotency lookup. That rule (`ledger/schema.py:987-997`)
covers only `decision.asked` and `decision.elected` (`_DISPLAYED_TEXT`
`:693-702`). These fields are left free: `item.created.title` `:194`,
`item.note.text` `:245`, `item.spec.title`, `body_md` and `acceptance`
`:262-263,267`, and `item.progress.text` `:527`.

**File input.** `spec set --body-file` reads with
`Path.read_text(encoding="utf-8")` (`ledger/verbs/spec.py:298`). That call
translates CRLF to LF, so a CR in the file never reaches a write rule. No
other verb reads note, progress or acceptance text from a file. `append-many`
reads a JSON array from stdin (`ledger/verbs/append_many.py:70`), and a CR
inside a JSON string is spelled `"\r"`, which text-mode reading leaves
intact.

**Bundled mirror.** `plugins/ledger-hooks/ledger/` is a byte-for-byte copy of
`ledger/`, enforced by
`tests/test_plugin_bin.py::test_bundled_ledger_matches_package` (`:95-105`).
Every site above exists in both trees.

## 2. Render rule

Escaping happens at format time, in the text a verb prints. `--json` forms keep
their text unescaped: the stored text, or for context its flattened and
clipped text. They stay safe because `json.dumps` runs with its default
`ensure_ascii=True` (`ledger/cli.py:162`), which escapes every C0, C1 and
non-ASCII code point.

**One-line escaper.** This is the existing `_one_line` (`ledger/card.py:777`),
widened. It writes LF, CR and TAB as `r"\n"`, `r"\r"` and `r"\t"`.
It writes every other Cc, U+2028, U+2029, U+202A-U+202E and U+2066-U+2069 as
`r"\uXXXX"`. The bidi ranges are checked by code point.

**Multi-line escaper.** This is a new `_multi_line(value)` next to `_one_line`.
It keeps LF and TAB, writes CR as `r"\r"`, and writes every other code
point `_one_line` escapes with the same `r"\uXXXX"`.

Neither escaper emits whitespace. Escaping therefore never adds words, so a
word budget computed before escaping still holds after it.

Sites:

1. **Card** (`ledger/card.py`). Apply `_one_line` to every value in the section
   1 table, at the f-string that prints it.
   - `_fmt_notes` is one site, and it serves Notes and Progress.
   - For ask fields, escape the value before `_shown` adds its marker.
   - The header item id `:1045` is escaped too.
   - The six command builders go through one new helper,
     `_command(program, args)`, which returns
     `f"{program} {_one_line(shlex.join(args))}"`. `program` is already
     shell-escaped by `invocation_prefix`
     (`ledger/verbs/__init__.py:152-157`).
   - Enum fields are escaped too.
   - The `--md` file is the same string.
   - `_card_word_count` `:279-310` measures `format_card`, so the bound stays
     exact.
   - The card prints no spec title (`:1053` prints only `spec v<N>`).
2. **`spec show`**. Apply `_one_line` to the item id, title, source and
   source_rev, and `_multi_line` to acceptance and body.
3. **`spec diff`**.
   - The header `:388` prints the item id through `_one_line`.
   - `diff_fields(old, new, escape=False)` still decides which fields changed,
     on the raw values.
   - With `escape=True`, each value is escaped before `_lines` splits it:
     title, size and merge class through `_one_line`; body_md and acceptance
     through `_multi_line`. After escaping, LF is the only line break left, and
     a title TAB prints as `r"\t"`.
   - An absent field stays absent. The escape is `None if value is None else
     escaper(value)`, so `_lines(None)` still yields no lines and no `None`
     text is ever printed.
   - The text uses `escape=True`. `data.fields` uses `escape=False`, so the
     stored `from`, `to` and `diff` lines keep their code points until
     `json.dumps` escapes them.
4. **export-item.**
   - File-locator form: the text is `_multi_line(item_ticket_text(...))` at
     `:135`, and `data.ticket` keeps the unescaped section.
   - Spec fallback: `_spec_text` gains an `escape` flag. The text uses it
     (`_one_line` on the title, `_multi_line` on body and acceptance), and
     `data.ticket` keeps today's unescaped rendering. `data.spec` keeps the
     stored fields.
   - The default form prints one `json.dumps` record per line `:144`, which is
     already safe; a test pins it.
5. **context.** Context normalizes whitespace before escaping. `_flat` and
   `_clip` may turn TAB and other whitespace into a space first, and that is
   accepted.
   - `_spec_lines` `:186` splits on LF only (`str(text).split("\n")`) in
     place of `splitlines()`. U+2028, U+2029, U+0085, VT, FF and
     U+001C-U+001E then stay inside their line.
   - `fit` applies `_one_line` to each line as it joins the text at `:429`.
   - Every remaining Cc, separator and bidi control is escaped, on both the
     flattened path and the spec path, so no raw control survives either one.
   - A separator at the very end of a body line can still be removed by
     `rstrip()` `:186`. That is whitespace normalization: the separator is
     removed, never printed raw.
   - `data.spec` and `data.sections[].lines` keep the unescaped lines.
6. **search** (`ledger/verbs/search.py:80-88`). Apply `_one_line` to the id,
   title, snippet and caveat. `data.cards` is unchanged.
7. **Item listings.** `format_items` applies `_one_line` to each of its four
   columns, so a TAB in a title cannot add a column. `item_dict` `:267-279` is
   unchanged.

## 3. Write rule

Two code-point sets, written as literal ranges:

- **MULTI** is U+0000-U+0008, U+000B-U+001F, U+007F-U+009F (every Cc except
  TAB and LF, so CR, ESC, BEL, NUL, DEL, NEL and CSI U+009B are in it), U+2028,
  U+2029, U+202A-U+202E and U+2066-U+2069.
- **ONE** is MULTI plus TAB U+0009 and LF U+000A.

| Field | Set |
|---|---|
| `item.created.title`, `item.spec.title` | ONE |
| `item.note.text`, `item.progress.text`, `item.spec.body_md`, `item.spec.acceptance` | MULTI |

The ask rule's NFC, blank, edge-whitespace, Cf and invisible classes are not
applied. They exist so a one-line picker answer has one spelling to bind an
election to. These fields bind nothing, and they are prose: Markdown ends in a
newline and indents with tabs, and pasted text carries decomposed accents, ZWJ
emoji and variation selectors.

**Refusal.** The reason is `display_control_character:<kind>.<field>`, for
example `display_control_character:item.spec.body_md`. It is held in a new
constant, `DISPLAY_CONTROL_CHARACTER`, beside `ASK_CONTROL_CHARACTER`
(`ledger/schema.py:687`). The reason never carries the character.
`ValidationError` (`ledger/schema.py:671-676`) gains a keyword-only `hint`.
`reason` stays exactly the name. When the first refused code point is CR, the
CLI prints `str(exc)` (`ledger/cli.py:281-282`), which then reads
`<reason>: U+000D: convert line endings to LF`.

**File input.** `spec set --body-file` reads with
`Path(...).read_bytes().decode("utf-8")` (`ledger/verbs/spec.py:298`), so CRLF
reaches the rule as CR LF and is refused with the hint. The existing
`UnicodeDecodeError` handling at `:299-300` still applies.

**Hook.** In `ledger/schema.py`, after `display_control_character`, add a
table `_ITEM_TEXT = {kind: ((field, one_line), ...)}` and a function
`item_text_control_character(kind, payload) -> (field, code point) | None`.
A non-string value is skipped, because the schema already ran at
`ledger/store.py:937`. `refuse_display_control_characters` calls it after the
ask check. This keeps one call site, `ledger/store.py:987`, which every writer
reaches, and one patch point for pre-rule rows.

**Write time only.** The rule is not in `validate_payload` and is not the
`no_control` schema flag (`ledger/schema.py:1054`), because verify and export
run `validate_payload` again (`ledger/export.py:1259,1628`). Restore and
replay (`_replay_events`, `ledger/restore.py:194-268`) never call the store
precondition, so an older ESC-bearing row restores byte for byte. An exact
retry of a stored keyed row returns it. The same text under a new key is
refused.

CHANGELOG Unreleased gets one entry. It says that card, spec, export-item,
context, search and listing output escape control characters, and that new
text holding them is refused.

## 4. Boundary

- **CR is refused in every field, CRLF included.** A spec body file with
  Windows line endings is refused, and the message says to convert line
  endings to LF. This is a known limit; the content-hash rule is unchanged.
- **`item.spec.source` and `source_rev` are escaped when printed, not refused
  at write.** `source_rev` is already single-line in the schema
  (`ledger/schema.py:271-272`).
- **Context and search show whitespace-normalized text.** A TAB or LF inside a
  context row can print as a space. Every control and bidi code point that is
  left is escaped.
- **Other Cf code points are neither refused nor escaped.** This covers
  U+200B-U+200F (including LRM and RLM), U+061C, U+FEFF and the tag
  characters. They occur in real RTL and emoji text and drive no terminal.
- **`_one_line` is not injective.** Stored text spelled `r"\u001b"` prints
  the same as an escaped ESC. This is accepted, as for Children rows today.
- **An id holding a control character gets an escaped recovery command that
  will not run.** Item ids have no grammar (`ledger/schema.py:1186-1201`).
- **Tried, stopped, review and decision-record text are escaped where these
  verbs print them, but not refused at write.**
- **Printers outside these sites are unchanged.** `events` and `links` already
  use `text_cell`. `resume` and `doc` are untouched.

## 5. Tests

**Pre-rule rows.** Some text can no longer be written through the CLI once the
rule ships. Each test marked [pre-rule] stores its rows in one of two ways:
`append`/`set_spec` with `ledger.store.refuse_display_control_characters`
patched to a no-op (as `write_legacy` does,
`ledger/tests/test_ask_control_characters.py:197-204`), or `replay_events`.

Constants: OSC = `"\x1b]52;c;aGk=\x07"`. RLO = `"\u202e"`. LRI =
`"\u2066"`. LS = `"\u2028"`.

- T1 [pre-rule]. Note `"a" + OSC + "b\nc"`. `card` stdout and the `--md`
  file hold no `"\x1b"` and no `"\x07"`. Both hold
  `r"a\u001b]52;c;aGk=\u0007b\nc"` on one line. Also under `--full`.
- T2 [pre-rule]. Progress `"\x1b[2Jx"`. Stdout and `--md` hold no
  `"\x1b"`, and both hold `r"\u001b[2Jx"`. Also under `--full`.
- T3 [pre-rule]. Title `"T\x1b[31m\nX"`. The `title:` line holds
  `r"T\u001b[31m\nX"`. There is no raw ESC, and no output line equals
  `X`.
- T4 [pre-rule]. Parametrized over every other card field in the section 1
  table that a write can carry. Value `"v\x1b[2J"`: stdout holds no
  `"\x1b"` and holds `r"v\u001b[2J"`.
- T5 [pre-rule]. `spec show` with:
  - title `"S\x1b[2J"`;
  - acceptance `"ok\x1b[2J\nnext"`;
  - body `"# H\n\n\tcode\x1b[2J\r\nz" + LS + "w\n"`.

  Stdout holds no `"\x1b"` and no LS. The title line is `r"S\u001b[2J"`.
  `## Body` is followed by the lines `# H`, an empty line,
  `"\tcode" + r"\u001b[2J\r"` and `r"z\u2028w"`. The acceptance
  lines are `r"ok\u001b[2J"` and `next`.
- T6. A unit test over every code point U+0000-U+009F, U+2028, U+2029,
  U+202A-U+202E and U+2066-U+2069.
  - `_multi_line` keeps LF and TAB, maps CR to `r"\r"`, and maps the rest
    to `r"\uXXXX"`.
  - `_one_line` maps LF, CR and TAB to `r"\n"`, `r"\r"` and `r"\t"`,
    and the rest to `r"\uXXXX"`.
  - Neither output holds a listed code point, except LF and TAB from
    `_multi_line`.
  - `"\u00e9"`, `"\u200d"` and `"\u200e"` pass through both.
- T7. Multi-line fields, each written through the CLI and through the library:
  `item.note.text` (`note`), `item.progress.text` (`append`),
  `item.spec.body_md` and `item.spec.acceptance` (`spec set`), plus `append`,
  `append_many` and `set_spec`. Code points: ESC, BEL, NUL, CR, VT, DEL,
  U+0085, U+009B, U+2028, U+2029, U+202A, U+202E, U+2066, U+2069. Each write is
  refused with `exc.reason` exactly `display_control_character:<kind>.<field>`.
  The event count is unchanged, and the message holds none of the code points.
- T7b. One-line fields: `item.created.title` (`new`, `append`) and
  `item.spec.title` (`spec set --title`), with T7's code points plus LF and
  TAB. Each is refused with the same reason form.
- T8. Ordinary text writes.
  - The note, progress, spec body and acceptance are
    `"# Heading\n\n\tcode\n- item  \n"`, plus `"e\u0301"`,
    `"\U0001f468\u200d\U0001f469"`, `"\u200e"`,
    `"\u2764\ufe0f"`, a leading space and a trailing newline.
  - The titles are `"Fix \u00e9 \u2764\ufe0f now"`.
  - All exit 0, and `spec show` prints the body with its LF and TAB.
- T9 [pre-rule]. Old rows still read.
  1. Write an item whose title, note (OSC), progress, and spec body and
     acceptance hold ESC.
  2. `lg.verify()` passes and `export-item` exits 0.
  3. Run `export_events`, then `verify_export`, then `restore_export` into a
     fresh path. On the restored database, `lg.verify()` passes and each
     payload equals the original byte for byte (`canonical_dumps`). `card` and
     `spec show` exit 0 with escaped output.
  4. Repeat into another fresh database through `replay_events`.
  5. An exact keyed retry of the pre-rule note returns the stored event; a new
     key is refused.
- T10 [pre-rule]. Item id `"X\x1b[2J"`. The card's header line and footer
  hold `r"X\u001b[2J"`, and stdout holds no `"\x1b"`.
- T11 [pre-rule]. `spec diff`, v1 to v2, on the item id `"D" + OSC` (an id
  with ESC and BEL).
  - v1: title `"A"`, body `"p\nq\n"`.
  - v2: title `"A\tB"`, body `"p\x1b[2J" + RLO + "\nq" + LS +
    "r\n"`.

  Text output:
  - Stdout holds no ESC, BEL, RLO or LS, and no raw TAB.
  - The header line starts `r"spec diff D\u001b]52;c;aGk=\u0007 v1"`.
  - It holds `r"+A\tB"`, `r"+p\u001b[2J\u202e"` and
    `r"+q\u2028r"` (one line).

  With `--json`, stdout holds no raw ESC. In the decoded `data.fields`:
  - `from` and `to` equal the stored values.
  - The title entry's `diff` holds the line `"+A\tB"` with its raw TAB.
  - The body entry's `diff` holds a line containing `"\x1b[2J" + RLO` raw.

  So every JSON diff line keeps the stored code points until `json.dumps`
  escapes them.
- T11b [pre-rule]. Optional field absent, then present, then absent again.
  - Spec v1 has no acceptance. v2 has acceptance `"ok\x1b[2J"`. v3 has no
    acceptance.
  - `spec diff` v1 v2: the `## acceptance` hunk holds `r"+ok\u001b[2J"`.
  - `spec diff` v2 v3: the hunk holds `r"-ok\u001b[2J"`.
  - Neither output holds the text `None` on any line.
  - With `--json`, the acceptance entry's `from` (v1 v2) and `to` (v2 v3) are
    `null`.
- T12 [pre-rule]. `card --json` and `spec show --json`, note and body holding
  ESC and `"\x9b"`. Stdout holds neither code point raw.
  `data.notes[0].text` and `data.body_md` equal the stored text.
- T13 [pre-rule]. `export-item --ticket` on a sourceless item whose spec title,
  body and acceptance hold ESC and RLO.
  - The text holds neither code point raw, holds `r"\u001b"` and
    `r"\u202e"`, and keeps the body's lines.
  - With `--json`, stdout holds no raw ESC, and `data.spec.body_md` equals the
    stored body.
  - Plain `export-item` on the same item: stdout holds no raw ESC.
- T14 [pre-rule]. `context` on an item whose title, note and spec body hold
  `"\x1b[2J" + "\x07" + RLO` and a TAB.
  - Stdout holds no ESC, BEL or RLO and holds `r"\u001b"`, `r"\u0007"`
    and `r"\u202e"`. The TAB may print as a space.
  - The spec body also holds `"x" + LS + "y\u2029z\x85w\n"`. Stdout
    holds `r"x\u2028y\u2029z\u0085w"` on one spec line, and no line
    starts with `y`, `z` or `w`.
  - With `--json`, stdout holds no raw ESC, and the note row in
    `data.sections` still holds the ESC.
- T15 [pre-rule]. `query` and `ready` with the title
  `"T\x1b[2J\tX" + RLO`. Each item line has exactly four TAB-separated
  columns, and the title column is `r"T\u001b[2J\tX\u202e"`. With
  `--json`, `title` equals the stored title.
- T16 [pre-rule]. Card note `"a" + RLO + "b"` and a child title holding LRI.
  Stdout holds `r"a\u202eb"` and `r"\u2066"` and no raw bidi control.
- T17. `spec set --body-file` on a file written in binary as `b"line\r\nnext\r\n"`.
  Refused: `exc.reason` (library) and the start of the CLI error message are
  `display_control_character:item.spec.body_md`, and the message ends
  `convert line endings to LF`. No event is written. A body holding ESC is
  refused with a message equal to the reason.
- T18 [pre-rule]. `search` for a word in an item whose title and note hold
  `"\x1b[2J" + RLO`. Stdout holds no ESC or RLO and holds
  `r"\u001b[2J\u202e"` in the title column and in the snippet line.
  `--json` `data.cards[0].title` equals the stored title.
- T19. `export-item --ticket` with a file locator. Build a temporary git
  repository as the existing `--ticket` tests do. Commit a ticket section
  holding `"\tstep\x1b[2J" + RLO + "\nnext" + LS + "line\n"`, and
  point the item's source at it (`path:line@commit`).
  - The text holds no ESC, RLO or LS. It holds
    `"\tstep" + r"\u001b[2J\u202e"` (TAB kept) and
    `r"next\u2028line"` on its own line.
  - With `--json`, stdout holds no raw ESC, and the decoded `data.ticket`
    equals the section as read, raw code points included.

## 6. Mutations

Each runs on a scratch copy, RED then GREEN, and quotes the pytest `E` line.
Every refusal name is observed RED: M6-M9, M11, M12 and M21.

- M1. `_fmt_notes` prints `row['text']` raw: T1 RED.
- M2. The Progress entry of `_RESUME_FORMAT` (`ledger/card.py:891`) uses a raw
  formatter: T2 RED.
- M3. The `title:` line skips `_one_line`: T3 RED.
- M4. `_show_text` prints `body_md` raw: T5 RED.
- M5. `_multi_line` lets ESC through: T6 and T5 RED.
- M6. `item_text_control_character` skips `item.note`: T7[item.note.text] RED.
- M7. It skips `item.progress`: T7[item.progress.text] RED.
- M8. It skips `item.spec` `body_md`: T7[item.spec.body_md] RED.
- M9. It skips `item.created` `title`: T7b[item.created.title] RED.
- M10. MULTI also holds LF: T8 RED.
- M11. Titles use MULTI, not ONE: T7b[item.created.title, LF] and
  T7b[item.spec.title, TAB] RED.
- M12. It skips `item.spec` `acceptance`: T7[item.spec.acceptance] RED.
- M13a. `_one_line` lets U+202E through: T6 and T16 RED.
- M13b. `_multi_line` lets U+202E through: T6 and T13 RED.
- M14a. export-item's `_spec_text` prints raw: T13 RED.
- M14b. Context's `fit` joins raw lines: T14 RED.
- M14c. `format_items` prints the title raw: T15 RED.
- M14d. Search's `_text` prints the title or snippet raw: T18 RED.
- M15a. `Card.as_dict` escapes the notes: T12 RED.
- M15b. Context `data.sections` lines are escaped: T14 RED.
- M15c. export-item `data.spec` is escaped: T13 RED.
- M15d. `item_dict` escapes the title: T15 RED.
- M15e. Search `data.cards` is escaped: T18 RED.
- M15f. `spec diff` `data.fields` is escaped: T11 RED.
- M16. MULTI drops U+2028 (then U+202A-U+202E): T7[U+2028] (T7[U+202E]) RED.
- M17. The refusal also runs from `validate_payload`: T9 RED.
- M18a. `_command` drops `_one_line`: T10 RED.
- M18b. The header line prints the item id raw: T10 RED.
- M19a. `spec diff` escapes the finished diff lines instead of the field
  values: T11 RED (LS split, raw TAB).
- M19b. `spec diff` prints raw: T11 RED.
- M20. The spec title or acceptance skips its escaper: T5 RED.
- M21. It skips `item.spec` `title`: T7b[item.spec.title] RED.
- M22. One mutation per T4 field: drop that field's `_one_line`, and T4[that
  field] goes RED.
- M23. `cli.py` JSON output uses `ensure_ascii=False`: T12 RED (U+009B).
- M24. The CR hint is dropped: T17 RED.
- M25. `_multi_line` escapes LF (or TAB): T5 and T8 RED.
- M26. `--body-file` goes back to `read_text`: T17 RED (CRLF becomes LF, and
  the write succeeds).
- M27. The `spec diff` header prints the item id raw: T11 RED.
- M28. The diff escape turns None into text (`escaper(value)` for every
  value): T11b RED.
- M29. export-item prints the file-locator ticket text raw: T19 RED.
- M30. export-item escapes `data.ticket` too: T19 RED.
- M31. Context's `_spec_lines` goes back to `splitlines()`: T14 RED.

## 7. Build notes

- **Plugin mirror.** Copy every edited file under `ledger/` to
  `plugins/ledger-hooks/ledger/` byte for byte in the same commit.
  `tests/test_plugin_bin.py::test_bundled_ledger_matches_package` fails
  otherwise. Mutations run on `ledger/` only; the mirror is restored with the
  rest of the scratch copy.
- **Note line breaks.** A multi-line note now prints on one line
  (`r"a\nb"`). Existing card tests that pin a note's line breaks are
  updated, and the commit says why.
- **Import cycle.** `ledger/verbs/__init__.py` and `ledger/verbs/search.py` will
  import `_one_line` from `ledger.card`. Check for a cycle with
  `python -I -c "import ledger.verbs, ledger.cli"` and the full suite. If
  there is one, move both escapers to a small `ledger/escape.py` (mirrored too)
  that `ledger.card` re-exports.
- **Context budget.** Context's word budget is computed before escaping.
  Escaping never adds words (section 2), so the budget still holds.
- **`hint` keyword.** `ValidationError`'s `hint` is keyword-only, so every
  existing caller is unchanged.
- **Keep this file ASCII.** In this file and in test sources, spell control
  and bidi code points as escapes, never raw.
