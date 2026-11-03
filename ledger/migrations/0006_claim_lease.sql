-- A claim carries a lease. Views only: no table or column changes, and the
-- events table and its hash chain are untouched (ADR 0034).
--
-- v_leases has one row per item whose latest state is a claim. Its
-- expires_at is the claim's own payload stamp, or the latest stamp on an
-- item.progress event recorded after the claim when that is later. A claim
-- with no stamp (recorded before leases) has a NULL expires_at and never
-- expires. Stamps are UTC text in one fixed millisecond form, so they order
-- as text. Whether a lease has run out is read against the database clock:
-- v_ready treats a claimed item whose lease expired as open.
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
  END AS expires_at
FROM v_items AS items
JOIN events AS state ON state.id = items.state_event_id
WHERE items.status = 'claimed'
  AND state.kind = 'item.state';

DROP VIEW v_ready;
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
