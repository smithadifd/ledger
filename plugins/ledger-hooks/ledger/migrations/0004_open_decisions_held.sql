-- A held decision has no open ask. An ask stays open unless:
--   * an election closes it (unchanged from 0002);
--   * the item is currently blocked by the needs-you held mapping's own
--     block (its key starts 'needs-you:<item>:item.state:blocked:'),
--     recorded after the ask. That block withdraws the asks it follows; an
--     ask recorded after it is a new question and stays open; reopening the
--     item (a later non-blocked state) brings a withdrawn ask back. A block a
--     person sets (for example `ledger state --status blocked --blocked-by X`)
--     withdraws nothing: the question is still open, to answer once the
--     blockers clear, and the decision card says so;
--   * the latest decision.held marker on the ask has status 'held'. The
--     marker withdraws one ask without changing item state, for items whose
--     state must not be written (claimed or loop-managed items, or a state a
--     person set). A later 'released' marker restores the ask. A marker
--     without asked_event_id is item-level: it names no ask, so it never
--     matches here and hides nothing.
DROP VIEW v_open_decisions;
CREATE VIEW v_open_decisions AS
SELECT
  asked.item_id AS item_id,
  asked.id AS asked_event_id,
  json_extract(asked.payload, '$.question') AS question,
  json_extract(asked.payload, '$.recommendation') AS recommendation,
  json_extract(asked.payload, '$.default') AS "default",
  asked.recorded_at AS recorded_at
FROM events AS asked
WHERE asked.kind = 'decision.asked'
  AND NOT EXISTS (
    SELECT 1
    FROM events AS elected
    WHERE elected.kind = 'decision.elected'
      AND elected.item_id = asked.item_id
      AND elected.id > asked.id
      AND (
        json_extract(elected.payload, '$.asked_event_id') = asked.id
        OR (
          json_extract(elected.payload, '$.asked_event_id') IS NULL
          AND json_extract(elected.payload, '$.question_id') IS NOT NULL
          AND json_extract(elected.payload, '$.question_id')
            = json_extract(asked.payload, '$.question_id')
        )
      )
  )
  AND NOT EXISTS (
    SELECT 1
    FROM v_items AS held
    JOIN events AS hold ON hold.id = held.state_event_id
    WHERE held.item_id = asked.item_id
      AND held.status = 'blocked'
      AND held.state_event_id > asked.id
      AND substr(hold.idempotency_key, 1,
                 length('needs-you:' || asked.item_id || ':item.state:blocked:'))
        = 'needs-you:' || asked.item_id || ':item.state:blocked:'
  )
  AND NOT EXISTS (
    SELECT 1
    FROM events AS mark
    WHERE mark.kind = 'decision.held'
      AND mark.item_id = asked.item_id
      AND json_extract(mark.payload, '$.asked_event_id') = asked.id
      AND json_extract(mark.payload, '$.status') = 'held'
      AND NOT EXISTS (
        SELECT 1
        FROM events AS later
        WHERE later.kind = 'decision.held'
          AND later.item_id = mark.item_id
          AND json_extract(later.payload, '$.asked_event_id') = asked.id
          AND later.id > mark.id
      )
  );
