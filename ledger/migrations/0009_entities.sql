-- The entity registry: automations, projects, machines and models as ledger
-- state. Views only: no table or column changes, and the events table and
-- its hash chain are untouched. Every view 0008 defines is left as it is.
--
-- v_entities has one row per entity, keyed by (type, entity_id) and built
-- from the first entity.registered event for that key (the writer,
-- verbs/entity.py, refuses a second). state is a JSON object holding, for
-- each state field any entity.state event has named, the value from the
-- newest such event by event id. revision is the id of the entity's newest
-- event: its newest entity.state event, or its registration when it has
-- none. updated_at is that event's recorded_at. An entity with no state
-- event has state '{}'. A state event for a key that was never registered
-- is ignored (the writer refuses one).
CREATE VIEW v_entities AS
WITH ranked_registered AS (
  SELECT
    id AS registered_event_id,
    json_extract(payload, '$.type') AS type,
    json_extract(payload, '$.id') AS entity_id,
    json_extract(payload, '$.title') AS title,
    recorded_at AS registered_at,
    ROW_NUMBER() OVER (
      PARTITION BY json_extract(payload, '$.type'), json_extract(payload, '$.id')
      ORDER BY id ASC
    ) AS rn
  FROM events
  WHERE kind = 'entity.registered'
),
registered AS (
  SELECT registered_event_id, type, entity_id, title, registered_at
  FROM ranked_registered
  WHERE rn = 1
),
ranked_fields AS (
  SELECT
    event.id AS state_event_id,
    json_extract(event.payload, '$.type') AS type,
    json_extract(event.payload, '$.id') AS entity_id,
    field.key AS field,
    field.value AS value,
    ROW_NUMBER() OVER (
      PARTITION BY
        json_extract(event.payload, '$.type'),
        json_extract(event.payload, '$.id'),
        field.key
      ORDER BY event.id DESC
    ) AS rn
  FROM events AS event, json_each(event.payload, '$.state') AS field
  WHERE event.kind = 'entity.state'
),
latest_fields AS (
  SELECT
    type,
    entity_id,
    json_group_object(field, value) AS state,
    MAX(state_event_id) AS state_event_id
  FROM ranked_fields
  WHERE rn = 1
  GROUP BY type, entity_id
)
SELECT
  registered.type AS type,
  registered.entity_id AS entity_id,
  registered.title AS title,
  COALESCE(latest_fields.state, '{}') AS state,
  registered.registered_event_id AS registered_event_id,
  COALESCE(latest_fields.state_event_id, registered.registered_event_id) AS revision,
  registered.registered_at AS registered_at,
  COALESCE(latest.recorded_at, registered.registered_at) AS updated_at
FROM registered
LEFT JOIN latest_fields
  ON latest_fields.type = registered.type
  AND latest_fields.entity_id = registered.entity_id
LEFT JOIN events AS latest ON latest.id = latest_fields.state_event_id;
