-- The operator is the person part of an event's actor. Views only: no table
-- or column changes, and the events table and its hash chain are untouched.
--
-- The CLI stamps actor as <operator>/<agent> when LEDGER_OPERATOR and
-- LEDGER_AGENT are both set, and refuses either part containing '/'. A view's
-- operator is the text before the first '/' in actor, or '' when actor has no
-- '/' (every older event, whose actor is the bare agent) or is NULL.
--
-- v_events is every event with its operator. v_leases gains operator: the
-- operator of the claim event's actor. v_ready reads v_leases, so it is
-- dropped and recreated unchanged.
CREATE VIEW v_events AS
SELECT
  events.*,
  CASE
    WHEN instr(events.actor, '/') > 0
      THEN substr(events.actor, 1, instr(events.actor, '/') - 1)
    ELSE ''
  END AS operator
FROM events;

DROP VIEW v_ready;
DROP VIEW v_leases;

CREATE VIEW v_leases AS
SELECT
  items.item_id AS item_id,
  COALESCE(items.owner, items.claim) AS holder,
  items.state_event_id AS claim_event_id,
  items.updated_at AS claimed_at,
  CASE
    WHEN json_extract(state.payload, '$.expires_at') IS NULL THEN NULL
    ELSE MAX(
      json_extract(state.payload, '$.expires_at'),
      COALESCE(
        (
          SELECT MAX(json_extract(progress.payload, '$.expires_at'))
          FROM events AS progress
          WHERE progress.kind = 'item.progress'
            AND progress.item_id = items.item_id
            AND progress.id > items.state_event_id
        ),
        json_extract(state.payload, '$.expires_at')
      )
    )
  END AS expires_at,
  CASE
    WHEN instr(state.actor, '/') > 0
      THEN substr(state.actor, 1, instr(state.actor, '/') - 1)
    ELSE ''
  END AS operator
FROM v_items AS items
JOIN events AS state ON state.id = items.state_event_id
WHERE items.status = 'claimed'
  AND state.kind = 'item.state';

CREATE VIEW v_ready AS
SELECT * FROM v_items
WHERE (
    status = 'open'
    OR (
      status = 'claimed'
      AND EXISTS (
        SELECT 1
        FROM v_leases AS lease
        WHERE lease.item_id = v_items.item_id
          AND lease.expires_at IS NOT NULL
          AND lease.expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
      )
    )
  )
  AND NOT EXISTS (
    SELECT 1
    FROM events AS link
    WHERE link.kind = 'item.linked'
      AND json_extract(link.payload, '$.rel') = 'blocks'
      AND json_extract(link.payload, '$.target') = v_items.item_id
      AND NOT EXISTS (
        SELECT 1
        FROM v_items AS blocker
        WHERE blocker.item_id = link.item_id
          AND blocker.status = 'closed'
      )
  );
