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
-- ``WebSearchHit`` already. Replaying this changes nothing: no old name is left.

UPDATE session_records
SET payload = regexp_replace(
    payload::text,
    '("py/object"\s*:\s*)"WebSearchResult"',
    '\1"WebSearchHit"',
    'g'
)::json
WHERE payload::text ~ '"py/object"\s*:\s*"WebSearchResults"';

UPDATE session_records
SET payload = regexp_replace(
    regexp_replace(
        regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        payload::text,
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
WHERE payload::text ~ (
    '"py/object"\s*:\s*"(ShellCommandResult|FileReadResult|FileWriteResult'
    '|FileEditResult|AgentStatusResult|WebSearchResults)"'
);

UPDATE session_records
SET kind = CASE kind
    WHEN 'ShellCommandResult' THEN 'BashResult'
    WHEN 'FileReadResult' THEN 'ReadResult'
    WHEN 'FileWriteResult' THEN 'WriteResult'
    WHEN 'FileEditResult' THEN 'EditResult'
    WHEN 'AgentStatusResult' THEN 'AgentSpawnResult'
    WHEN 'WebSearchResults' THEN 'WebSearchResult'
END
WHERE kind IN (
    'ShellCommandResult', 'FileReadResult', 'FileWriteResult',
    'FileEditResult', 'AgentStatusResult', 'WebSearchResults'
);
