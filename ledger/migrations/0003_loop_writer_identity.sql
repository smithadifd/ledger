-- Loop-managed items can only be closed by the internal loop.closed event.
-- Existing item.state events remain authoritative for every other item.

DROP VIEW v_ready;
DROP VIEW v_blocked;
DROP VIEW v_items;

CREATE VIEW v_items AS
WITH ranked_created AS (
  SELECT
    item_id,
    id AS created_event_id,
    json_extract(payload, '$.type') AS type,
    json_extract(payload, '$.title') AS title,
    json_extract(payload, '$.status') AS created_status,
    recorded_at AS created_at,
    ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY id ASC) AS rn
  FROM events
  WHERE kind = 'item.created'
),
created AS (
  SELECT item_id, created_event_id, type, title, created_status, created_at
  FROM ranked_created
  WHERE rn = 1
),
managed AS (
  SELECT DISTINCT item_id FROM events WHERE kind = 'loop.managed'
),
loop_closed AS (
  SELECT DISTINCT item_id FROM events WHERE kind = 'loop.closed'
),
state_candidates AS (
  SELECT
    event.item_id,
    event.id AS state_event_id,
    event.payload,
    event.recorded_at
  FROM events AS event
  LEFT JOIN managed ON managed.item_id = event.item_id
  LEFT JOIN loop_closed ON loop_closed.item_id = event.item_id
  WHERE event.kind = 'item.state'
    AND (
      managed.item_id IS NULL
      OR (
        json_extract(event.payload, '$.status') <> 'closed'
        AND loop_closed.item_id IS NULL
      )
    )
  UNION ALL
  SELECT
    item_id,
    id AS state_event_id,
    json_object('status', 'closed') AS payload,
    recorded_at
  FROM events
  WHERE kind = 'loop.closed'
),
ranked_state AS (
  SELECT
    item_id,
    state_event_id,
    payload,
    recorded_at,
    ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY state_event_id DESC) AS rn
  FROM state_candidates
),
latest_state AS (
  SELECT item_id, state_event_id, payload, recorded_at
  FROM ranked_state
  WHERE rn = 1
)
SELECT
  created.item_id AS item_id,
  created.type AS type,
  created.title AS title,
  COALESCE(
    json_extract(latest_state.payload, '$.status'),
    CASE
      WHEN managed.item_id IS NOT NULL AND created.created_status = 'closed' THEN NULL
      ELSE created.created_status
    END,
    'open'
  ) AS status,
  json_extract(latest_state.payload, '$.owner') AS owner,
  json_extract(latest_state.payload, '$.claim') AS claim,
  created.created_event_id AS created_event_id,
  latest_state.state_event_id AS state_event_id,
  created.created_at AS created_at,
  COALESCE(latest_state.recorded_at, created.created_at) AS updated_at
FROM created
LEFT JOIN managed ON managed.item_id = created.item_id
LEFT JOIN latest_state ON latest_state.item_id = created.item_id;

CREATE VIEW v_blocked AS
SELECT * FROM v_items WHERE status = 'blocked';

CREATE VIEW v_ready AS
SELECT * FROM v_items
WHERE status = 'open'
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
