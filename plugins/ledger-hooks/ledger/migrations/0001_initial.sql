-- Append-only events, hash chain, and current-state views.
-- BEFORE UPDATE / BEFORE DELETE triggers are an accident guard, not a
-- security boundary: anyone with filesystem access can drop them.

CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  payload_version INTEGER NOT NULL,
  payload TEXT NOT NULL,
  item_id TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  actor TEXT,
  harness TEXT,
  model TEXT,
  session TEXT,
  channel TEXT,
  provenance_class TEXT,
  recorded_at TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE
);

CREATE INDEX idx_events_item_id ON events(item_id);
CREATE INDEX idx_events_kind ON events(kind);
CREATE INDEX idx_events_recorded_at ON events(recorded_at);

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN
  SELECT RAISE(ABORT, 'events are append-only: UPDATE refused');
END;

CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
  SELECT RAISE(ABORT, 'events are append-only: DELETE refused');
END;

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
  SELECT
    item_id,
    created_event_id,
    type,
    title,
    created_status,
    created_at
  FROM ranked_created
  WHERE rn = 1
),
ranked_state AS (
  SELECT
    item_id,
    id AS state_event_id,
    payload,
    recorded_at,
    ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY id DESC) AS rn
  FROM events
  WHERE kind = 'item.state'
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
    created.created_status,
    'open'
  ) AS status,
  json_extract(latest_state.payload, '$.owner') AS owner,
  json_extract(latest_state.payload, '$.claim') AS claim,
  created.created_event_id AS created_event_id,
  latest_state.state_event_id AS state_event_id,
  created.created_at AS created_at,
  COALESCE(latest_state.recorded_at, created.created_at) AS updated_at
FROM created
LEFT JOIN latest_state ON latest_state.item_id = created.item_id;

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
          json_extract(elected.payload, '$.question_id') IS NOT NULL
          AND json_extract(elected.payload, '$.question_id')
            = json_extract(asked.payload, '$.question_id')
        )
        OR (
          json_extract(elected.payload, '$.asked_event_id') IS NULL
          AND json_extract(elected.payload, '$.question_id') IS NULL
          AND json_extract(elected.payload, '$.question')
            = json_extract(asked.payload, '$.question')
        )
      )
  );

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
