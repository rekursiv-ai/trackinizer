-- schema.020.sql -- backfill legacy agent_session_events into session_records.
--
-- Step 1 of retiring the 8-member ``Message`` union. The converter READS the
-- table that 021 drops, so the two are separate files: a rollback can stop
-- after this one with both representations intact.
--
-- A migration rather than a script because only a migration survives the DROP
-- and runs exactly once (``store/core.py::_bootstrap_once``).
--
-- COST. This runs inside ``_bootstrap_once``, which precedes the listener, so
-- its duration is downtime; ``Type=simple`` means ``TimeoutStartUSec`` never
-- fires and systemd will not cut it short. The deployed table is 3,081,202
-- rows / 7907 MB, each row computing a STORED to_tsvector on insert, and this
-- also writes ~1877 MB of ciphertext while ``agent_session_events`` still
-- stands -- so the database peaks near double before 021 reclaims it. Time it
-- against a restored dump before it runs anywhere real.
--
-- GUARD: a FRESH install has no legacy table to convert. The baseline no
-- longer declares ``agent_session_events`` (021 retired it), so on a new
-- database every statement here would fail on a missing relation. A plain
-- ``IF to_regclass(...) THEN`` does NOT suffice: PL/pgSQL parses the whole
-- block at compile time, so the reference fails before the guard can run.
-- The statements are built as TEXT and dispatched with EXECUTE, which defers
-- parsing to the branch that actually executes.
--
-- One DO block rather than three files: the backfill must be a single
-- statement sequence, or a partial apply leaves records without the manifest
-- that bounds them.
DO $backfill$
BEGIN
IF to_regclass('public.agent_session_events') IS NULL THEN
    RETURN;
END IF;

-- ---------------------------------------------------------------------------
-- Records.
--
-- Legacy rows land at ``part = -1``, a reserved namespace. A session captured
-- before phase 4 and resumed after has IR records at ``part = 0``, and mapping
-- ``seq -> idx`` into that part would collide with no offset that reconciles
-- turn-space with record-space.
--
-- Every legacy turn becomes an ``UncategorizedRecord``: the union it came from
-- is lossy, so promoting a row to a typed IR record would assert structure the
-- capture never recorded. The original discriminator survives as
-- ``legacy/<Kind>``.
--
-- The ``py/object`` tag is REQUIRED -- ``DataclassCodec`` dispatches on it and
-- an untagged payload does not decode at all. ``schema_backfill_test.py`` pins
-- the dotted name against what the codec emits.
--
-- ``jsonb_build_object`` then cast to ``json``: the builder exists only in
-- jsonb, while the column is ``json`` to preserve key order for byte-exact
-- rewrites. A legacy row is never rewritten to a native file (its manifest has
-- no format), so the one-way normalization costs nothing here.
--
-- The payload strip is guarded on the value being an OBJECT: ``jsonb - key``
-- raises "cannot delete from scalar" on anything else, and the column is only
-- ``NOT NULL DEFAULT '{}'`` -- nothing forbids a stored scalar. One such row
-- would abort the whole migration mid-way, during startup downtime.
EXECUTE $sql$
INSERT INTO session_records
    (session_id, part, idx, kind, timestamp, created, model, payload, text)
SELECT session_id, -1, seq, 'UncategorizedRecord', timestamp, created, model,
       jsonb_build_object(
           'py/object', 'trackinizer.lib.agent.types.sessions.UncategorizedRecord',
           'context_id', NULL,
           'timestamp', NULL,
           'kind', 'legacy/' || kind,
           'payload', CASE jsonb_typeof(message)
               WHEN 'object'
               THEN message - 'thinking_encrypted' - 'thinking_signature'
               ELSE message
           END
       )::text::json,
       left(concat_ws(E'\n',
           nullif(message->>'text', ''),
           nullif(message->>'thinking', ''),
           nullif(message->>'content', ''),
           nullif(message->>'summary', ''),
           nullif(message->>'command', '')), 250000)
FROM agent_session_events
ON CONFLICT (session_id, part, idx) DO NOTHING
$sql$;

-- ---------------------------------------------------------------------------
-- Ciphertext, moved rather than left inline.
--
-- Claude folds its signature into the encrypted blob; the legacy union stored
-- the two separately, so they concatenate back in the order the provider
-- wrote them. Without this move the retention lever cannot reach a legacy
-- session's sealed reasoning at all -- it would sit in ``payload`` forever,
-- indexed by nothing and deletable only by rewriting every row.
--
-- ``->>`` answers NULL on a scalar rather than raising, so the type guard the
-- record insert needs is unnecessary here; ``coalesce`` turns that into the
-- empty string and the row is skipped.
EXECUTE $sql$
INSERT INTO session_ciphertext (session_id, part, idx, bytes)
SELECT session_id, -1, seq,
       convert_to(
           coalesce(message->>'thinking_encrypted', '')
           || coalesce(message->>'thinking_signature', ''),
           'UTF8')
FROM agent_session_events
WHERE coalesce(message->>'thinking_encrypted', '') <> ''
   OR coalesce(message->>'thinking_signature', '') <> ''
ON CONFLICT (session_id, part, idx) DO NOTHING
$sql$;

-- ---------------------------------------------------------------------------
-- One manifest per legacy session, or the session reads as zero records:
-- every reader bounds itself by ``records``, and a part with no manifest has
-- no bound to read.
--
-- ``format = ''`` is what makes a legacy session searchable but NEVER
-- resumable: there is no native file these turns can be rewritten as, and
-- phase 6 refuses a part with no format rather than producing one the CLI
-- would reject.
EXECUTE $sql$
INSERT INTO session_manifests
    (session_id, part, name, metadata, ir_id, format, records)
SELECT e.session_id,
       -1,
       'legacy',
       '{}'::json,
       e.session_id,
       '',
       max(e.seq) + 1
FROM agent_session_events e
GROUP BY e.session_id
ON CONFLICT (session_id, part) DO NOTHING
$sql$;

END
$backfill$;
