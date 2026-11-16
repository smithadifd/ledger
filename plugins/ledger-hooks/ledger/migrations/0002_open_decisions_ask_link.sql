-- An election carrying asked_event_id closes exactly that ask. Elections
-- without asked_event_id may close an ask by question_id. Question-text
-- equality no longer closes an ask: an election without an ask link closes
-- nothing, and answering a paraphrased question with --ask still closes it.
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
  );
