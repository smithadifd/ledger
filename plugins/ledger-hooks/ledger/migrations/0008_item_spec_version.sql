-- A ticket's content is a versioned item.spec event. Views only: no table or
-- column changes, and the events table and its hash chain are untouched.
--
-- v_items gains spec_version: the version named by the item's latest
-- item.spec event (by event id), or NULL when the item has none. Every
-- other column is unchanged and keeps its place; spec_version comes last.
-- v_blocked, v_leases, v_ready and v_open_decisions read v_items, so they
-- are dropped and recreated unchanged (v_blocked as in 0003, v_leases and
-- v_ready as in 0007, v_open_decisions as in 0004).
DROP VIEW v_open_decisions;
DROP VIEW v_ready;
DROP VIEW v_leases;
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
),
ranked_spec AS (
  SELECT
    item_id,
    json_extract(payload, '$.version') AS spec_version,
    ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY id DESC) AS rn
  FROM events
  WHERE kind = 'item.spec'
),
latest_spec AS (
  SELECT item_id, spec_version
  FROM ranked_spec
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
  COALESCE(latest_state.recorded_at, created.created_at) AS updated_at,
  latest_spec.spec_version AS spec_version
FROM created
LEFT JOIN managed ON managed.item_id = created.item_id
LEFT JOIN latest_state ON latest_state.item_id = created.item_id
LEFT JOIN latest_spec ON latest_spec.item_id = created.item_id;

CREATE VIEW v_blocked AS
SELECT * FROM v_items WHERE status = 'blocked';

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
