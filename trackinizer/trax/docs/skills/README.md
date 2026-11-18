# trax authoring skills -- per-inquiry expectations (SoT)

This directory is the **source of truth** for how each Trackinizer inquiry kind
should be filled: not the field list (that is `types/inquiries.py`), not the
command grammar (that is `trax/SKILL.md`, `../grammar.lark`, `../GRAMMAR.md`),
but the **content expectations** -- what makes a well-formed, complete instance
of each kind.

The children nest under `trax/`, one directory per inquiry kind, each holding a
`SKILL.md`. The router links to them using relative paths.

| Kind | File | Owns |
|---|---|---|
| Issue | `trax/issue/SKILL.md` | issue-body + bug-report standard; success criteria; verification |
| Paper | `trax/paper/SKILL.md` | full bib completeness; source cascade; DOI resolution; companion WebResults |
| Belief | `trax/belief/SKILL.md` | judgement/confidence; signed-valence citations; claims-not-papers |
| Experiment | `trax/experiment/SKILL.md` | codechanges; outcome; config; proved_by |
| CodeChange | `trax/codechange/SKILL.md` | full 40-char SHA; produced-link |
| WebResult | `trax/webresult/SKILL.md` | url; companion-of-paper pattern |
| WebSearch | `trax/websearch/SKILL.md` | query/provider; findings as produced_by |
| AgentSession | `trax/agentsession/SKILL.md` | envelope vs events asymmetry |

Rules for these docs:
- Describe EXPECTATIONS, not field enumerations. `types/inquiries.py` owns fields;
  do not hand-maintain them here.
- Reference `trax/SKILL.md` for the write grammar; do not duplicate it.
- Every value is best-effort ("try each source, fill what resolves, flag gaps");
  never fabricate, never leave a placeholder (e.g. a `-01-01` date).

`trax/SKILL.md` is the router: grammar, workflow, and a pointer into this dir.

Run `trax doctor` after setup or an upgrade. It checks the selected agent's
skill discovery path, compares managed skills and manuals with this package,
and checks the configured profile, server build endpoint, and authentication.
The command does not change files unless `--fix` or `--uninstall` is present.

Use `trax doctor --fix --target claude|cursor|codex` for initial setup or to
refresh managed skills. User installs use `~/.claude/skills`,
`~/.cursor/skills`, and `~/.agents/skills`, respectively. Add `--project` to use
the same directory beneath the caller's project. The bundle includes the router,
child skills, and both grammar manuals. Start a new agent session if its skill
list was already loaded. Add `--offline` to skip profile and server checks;
local repairs also complete before reporting a connection problem.

Valid source symlinks are treated as development installations and preserved.
Existing unmanaged directories, broken links, and edited managed copies are
reported without being overwritten. An older unmarked copy is unmanaged;
inspect and move it aside yourself before installing a managed copy.

Use `trax doctor --uninstall --target claude|cursor|codex` to remove an unchanged
managed copy. Removal never needs a server. It removes only installer-owned
files and empty parent directories created during installation, preserving
preexisting directories and other skills. Installed copies remain available
after the package is removed; run this command before uninstalling the package
if you also want to remove its copied skills.
