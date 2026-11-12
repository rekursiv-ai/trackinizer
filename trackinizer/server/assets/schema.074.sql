-- schema.074.sql -- lock history moves from ``inquiry_lock_log`` into ``change_log``,
-- as its first protected history class (``Change.PROTECTED_KINDS``).
-- The change log gains two kinds, ``locked`` and ``unlocked``, and each row of the log
-- becomes one such event under a deterministic id, with its time and actor unchanged.
--
-- Release N of a two-release drop: this build neither reads nor writes
-- ``inquiry_lock_log``, and the table stays. A later release drops it.
--
-- SIZE, measured 2026-11-09 on the two largest deployments: ``inquiry_lock_log`` 0 and
-- 2 rows; ``change_log`` 55,855,586 rows / 32 GB (a full scan took 2.8 s) and 460,638
-- rows / 279 MB. Neither has a lock-log row whose subject nothing remembers. The backfill is
-- one pass over ``inquiry_lock_log`` with a primary-key lookup in ``inquiries`` and one
-- ``idx_change_log_subject_kind`` probe per row. The constraint swap is the only
-- statement that scales with a table: adding ``change_log_kind_check`` revalidates
-- every ``change_log`` row under the boot's lock, as 046, 047 and 066 did.
--
-- Rerun-safe: the constraint is rebuilt to the same list, and ``ON CONFLICT (id) DO
-- NOTHING`` skips an event already moved. A lock the previous build logs after this
-- ran is not moved, so apply it only once that build is stopped, as a boot does.
ALTER TABLE change_log
    DROP CONSTRAINT change_log_kind_check,
    ADD CONSTRAINT change_log_kind_check CHECK (kind IN ({change_kinds}));

-- The id is the MD5 name-based UUID (version 3, ``uuid.uuid3`` in Python) of the URL
-- namespace and ``inquiry_lock_log:<row id>``. SQL has no SHA-1 without an extension
-- (PGlite loads none but pgvector), so this is not version 5.
--
-- ``subject_kind`` is the subject's kind: from ``inquiries`` while it exists, else
-- from any change that names it (a purged row keeps its ``created`` change), else
-- ``Issue`` for a row nothing else remembers, which the column cannot leave empty.
INSERT INTO change_log (id, created, actor, subject_id, subject_kind, kind)
SELECT
    (substr(named.hash, 1, 8) || '-' || substr(named.hash, 9, 4)
        || '-3' || substr(named.hash, 14, 3) || '-'
        || to_hex((get_byte(decode(substr(named.hash, 17, 2), 'hex'), 0) & 63) | 128)
        || substr(named.hash, 19, 2) || '-' || substr(named.hash, 21, 12))::uuid,
    lock_log.created,
    lock_log.actor,
    lock_log.inquiry_id,
    coalesce(subject.kind, seen.subject_kind, 'Issue'),
    CASE WHEN lock_log.locked THEN 'locked' ELSE 'unlocked' END
FROM inquiry_lock_log AS lock_log
CROSS JOIN LATERAL (
    SELECT md5(
        decode('6ba7b8119dad11d180b400c04fd430c8', 'hex')
        || convert_to('inquiry_lock_log:' || lock_log.id, 'UTF8')
    ) AS hash
) AS named
LEFT JOIN inquiries AS subject ON subject.id = lock_log.inquiry_id
LEFT JOIN LATERAL (
    SELECT audit.subject_kind FROM change_log AS audit
    WHERE audit.subject_id = lock_log.inquiry_id AND subject.id IS NULL
    LIMIT 1
) AS seen ON TRUE
ON CONFLICT (id) DO NOTHING;
