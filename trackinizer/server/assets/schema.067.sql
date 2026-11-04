-- schema.067.sql -- Tool result records are named by the sagent tool that did the act:
-- ShellCommandResult is BashResult, FileReadResult/FileWriteResult/FileEditResult are
-- ReadResult/WriteResult/EditResult, AgentStatusResult is AgentSpawnResult, and
-- WebSearchResults is WebSearchResult, whose hits were WebSearchResult and are now
-- WebSearchHit. Each row's ``kind`` and every ``py/object`` tag in its payload is
-- renamed in place. The payload is JSON, not JSONB, and is read back byte for byte,
-- so the tags are renamed as text: no other byte of a payload changes.
--
-- A hit is renamed only in a payload that still names ``WebSearchResults``, the one
-- container it nested in; a payload written after the rename names its hits
-- ``WebSearchHit`` already. The hit rename runs before the container rename, which
-- would otherwise turn the container into a hit. Replaying this changes nothing: no
-- old name is left.
--
-- Measured on the production database (128 cores): ``session_records`` held ~10M
-- rows in 38 GB (11 GB heap, 23 GB TOAST), and a 0.05% TABLESAMPLE matched ~4.3% of
-- them. Written as three statements, this ran ~33 minutes at startup, all of it
-- downtime, in one backend on one core: each statement de-TOASTed every payload to
-- text for its regex (360 GB read by minute 22) while writing ~115 MB/s. It is one
-- statement, so one scan, and it rewrites a payload only where a tag matches. A
-- rewrite this size runs before the restart, sharded; see
-- ``docs/db_schema_migration.md``, Roadmap A step 5.

UPDATE session_records
SET
    payload = CASE
        WHEN payload::text ~ (
            '"py/object"\s*:\s*"(ShellCommandResult|FileReadResult|FileWriteResult'
            '|FileEditResult|AgentStatusResult|WebSearchResults)"'
        )
        THEN regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            regexp_replace(
                                CASE
                                    WHEN payload::text ~ '"py/object"\s*:\s*"WebSearchResults"'
                                    THEN regexp_replace(
                                        payload::text,
                                        '("py/object"\s*:\s*)"WebSearchResult"',
                                        '\1"WebSearchHit"',
                                        'g'
                                    )
                                    ELSE payload::text
                                END,
                                '("py/object"\s*:\s*)"ShellCommandResult"', '\1"BashResult"', 'g'
                            ),
                            '("py/object"\s*:\s*)"FileReadResult"', '\1"ReadResult"', 'g'
                        ),
                        '("py/object"\s*:\s*)"FileWriteResult"', '\1"WriteResult"', 'g'
                    ),
                    '("py/object"\s*:\s*)"FileEditResult"', '\1"EditResult"', 'g'
                ),
                '("py/object"\s*:\s*)"AgentStatusResult"', '\1"AgentSpawnResult"', 'g'
            ),
            '("py/object"\s*:\s*)"WebSearchResults"', '\1"WebSearchResult"', 'g'
        )::json
        ELSE payload
    END,
    kind = CASE kind
        WHEN 'ShellCommandResult' THEN 'BashResult'
        WHEN 'FileReadResult' THEN 'ReadResult'
        WHEN 'FileWriteResult' THEN 'WriteResult'
        WHEN 'FileEditResult' THEN 'EditResult'
        WHEN 'AgentStatusResult' THEN 'AgentSpawnResult'
        WHEN 'WebSearchResults' THEN 'WebSearchResult'
        ELSE kind
    END
WHERE kind IN (
    'ShellCommandResult', 'FileReadResult', 'FileWriteResult',
    'FileEditResult', 'AgentStatusResult', 'WebSearchResults'
)
OR payload::text ~ (
    '"py/object"\s*:\s*"(ShellCommandResult|FileReadResult|FileWriteResult'
    '|FileEditResult|AgentStatusResult|WebSearchResults)"'
);
