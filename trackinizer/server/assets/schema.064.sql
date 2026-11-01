-- The assistant's own sessions, parts and liveness rows take its chat provider's name,
-- ``assistant``: from here on ``sagent`` names a sagent run on a person's machine, as
-- ``claude`` and ``codex`` name theirs. Until now only the assistant wrote ``sagent``,
-- so every such row is its own. Data only; a replay changes nothing.
UPDATE inquiries SET agentsession_cli = 'assistant'
WHERE kind = 'AgentSession' AND agentsession_cli = 'sagent';
UPDATE session_manifests SET format = 'assistant' WHERE format = 'sagent';
UPDATE session_liveness SET cli = 'assistant' WHERE cli = 'sagent';
