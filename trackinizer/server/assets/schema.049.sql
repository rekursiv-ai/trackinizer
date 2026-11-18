-- schema.049.sql -- a machine has an owner: the person responsible for it, and whether
-- that person confirmed it.
--
-- Only the owner may open a terminal on a machine, and a personal machine is shown to
-- its owner alone. An existing machine takes the account that enrolled it last: the
-- ``created_by`` of its latest used enrollment, NULL for one that never joined. An
-- admin's enrollment is its own confirmation, so those machines are confirmed now; a
-- machine a writer enrolls (often through an agent's key) is confirmed by its owner
-- in the web app before a terminal may open on it. Additive: the old build never
-- reads either column, so this is safe to apply before the restart. A replay changes
-- nothing, since only a machine without an owner is filled.
ALTER TABLE machines ADD COLUMN IF NOT EXISTS owner TEXT;
ALTER TABLE machines ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;

UPDATE machines SET owner = latest.created_by, confirmed_at = now()
FROM (
    SELECT DISTINCT ON (machine_id) machine_id, created_by
    FROM machine_enrollments
    WHERE used_at IS NOT NULL
    ORDER BY machine_id, used_at DESC, created DESC
) AS latest
WHERE machines.id = latest.machine_id AND machines.owner IS NULL;
