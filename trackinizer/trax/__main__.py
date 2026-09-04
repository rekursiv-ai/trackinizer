#!/bin/sh
# ruff: noqa: EXE003, D300 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
# `uv run` re-resolves the project on every invocation: measured 23ms of an
# 87ms `trax`, against the 1.4ms the server spends answering. uv's only job
# here is to find the venv interpreter, and that venv sits at a fixed place
# relative to THIS file -- so walk up and exec it directly.
#
# Keyed on the file's own location, never $VIRTUAL_ENV: an interactive shell
# that has not activated the venv does not export it, which is the common
# case and would silently skip this path. `readlink -f` resolves the bin/
# symlink first, so the walk starts inside the checkout rather than in bin/.
# Falling through to uv keeps a checkout with no built venv working.
_trax_self=$(readlink -f "$0"); _trax_dir=$(dirname "$_trax_self")
while [ "$_trax_dir" != "/" ]; do
  if [ -x "$_trax_dir/.venv/bin/python3" ]; then
    exec "$_trax_dir/.venv/bin/python3" "$_trax_self" "$@"
  fi
  _trax_dir=$(dirname "$_trax_dir")
done
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Command-line client for the trackinizer server.

Grammar:

  trax help [topic]                    -- show help
  trax profile                         -- show active profile
  trax profile foo                     -- show profile foo
  trax profile token to TOKEN          -- set active profile token
  trax profile foo token to TOKEN      -- set profile foo token
  trax profile current foo             -- select profile foo
  trax profile foo del                 -- delete profile foo
  trax                                 -- list all inquiry kinds
  trax <kind>                          -- list one inquiry kind
  trax <kind> <kind>                   -- list multiple inquiry kinds
  trax <kind> N..M                     -- list inclusive seq range
  trax <kind> field op value ...       -- list filtered rows (AND)
  trax <kind> field to value ...       -- create an inquiry
  trax <kind> field to -               -- read one field value from stdin
  trax <kind> <seq>                    -- detail view
  trax <kind> <seq> field [to value]   -- show or replace scalar/list fields
  trax <kind> <seq> <list> add value    -- append list value
  trax <kind> <seq> <list> del value    -- remove exact list value
  trax <kind> <seq> agent-cost add value -- apply signed cost delta
  trax <kind> <seq> del                 -- hard-delete an inquiry
  trax <kind> <seq> <edge> <kind> <seq> -- add an edge
  trax <kind> <seq> <edge> <kind> <seq> field to value -- annotate an edge
  trax <kind> <seq> <edge> <kind> <seq> del -- remove an edge
  trax next                            -- next active issue
  trax <field> <op> <value>            -- filter every kind
  trax recent                          -- audit-log feed
  trax cost <kind> <seq> [--deep]      -- agent-cost/resource-cost

Refs accept ``kind seq`` or a canonical UUID.
'''
# fmt: on

from __future__ import annotations

from trackinizer.trax.daemon.entry import main


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
