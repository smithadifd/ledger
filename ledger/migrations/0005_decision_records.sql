-- One row per decision.recorded event, with its current standing.
-- The standing starts as the record's own and moves only by a later
-- decision.standing event that names it (the newest wins). result and the
-- evidence pointer come from the newest decision.result, falling back to
-- the record's own evidence. A standing is never a ticket status: this view
-- reads no item.state event.
CREATE VIEW v_decision_records AS
WITH ranked_standing AS (
  SELECT
    id,
    item_id,
    payload,
    json_extract(payload, '$.decision_event_id') AS decision_event_id,
    ROW_NUMBER() OVER (
      PARTITION BY json_extract(payload, '$.decision_event_id')
      ORDER BY id DESC
    ) AS rn
  FROM events
  WHERE kind = 'decision.standing'
),
latest_standing AS (
  SELECT id, item_id, payload, decision_event_id
  FROM ranked_standing
  WHERE rn = 1
),
ranked_result AS (
  SELECT
    id,
    item_id,
    payload,
    json_extract(payload, '$.decision_event_id') AS decision_event_id,
    ROW_NUMBER() OVER (
      PARTITION BY json_extract(payload, '$.decision_event_id')
      ORDER BY id DESC
    ) AS rn
  FROM events
  WHERE kind = 'decision.result'
),
latest_result AS (
  SELECT id, item_id, payload, decision_event_id
  FROM ranked_result
  WHERE rn = 1
)
SELECT
  d.id AS decision_event_id,
  d.item_id AS item_id,
  json_extract(d.payload, '$.who') AS who,
  json_extract(d.payload, '$.who_kind') AS who_kind,
  json_extract(d.payload, '$.relayed_by') AS relayed_by,
  COALESCE(
    json_extract(s.payload, '$.standing'),
    json_extract(d.payload, '$.standing')
  ) AS standing,
  CASE
    WHEN s.id IS NULL THEN json_extract(d.payload, '$.source_event_id')
    ELSE json_extract(s.payload, '$.source_event_id')
  END AS source_event_id,
  json_extract(s.payload, '$.superseded_by') AS superseded_by,
  s.id AS standing_event_id,
  json_extract(d.payload, '$.effect') AS effect,
  json_extract(d.payload, '$.tags') AS tags,
  json_extract(d.payload, '$.verbatim') AS verbatim,
  json_extract(d.payload, '$.phase') AS phase,
  COALESCE(
    json_extract(r.payload, '$.evidence'),
    json_extract(d.payload, '$.evidence')
  ) AS evidence,
  json_extract(r.payload, '$.result') AS result,
  r.id AS result_event_id,
  d.recorded_at AS recorded_at
FROM events AS d
LEFT JOIN latest_standing AS s
  ON s.decision_event_id = d.id AND s.item_id = d.item_id
LEFT JOIN latest_result AS r
  ON r.decision_event_id = d.id AND r.item_id = d.item_id
WHERE d.kind = 'decision.recorded';
