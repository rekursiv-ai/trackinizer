"""The edge topology has ONE source of truth: ``types.edges.edge_topology``.

A citation-direction change can break the SPA when the edge direction is encoded
twice -- in the schema CHECK and in hand-typed SPA JS -- with no parity check, so
changing one leaves the other stale. This test pins the Python topology against
the generated schema edge-validity CHECK, and ``/api/meta/edges`` serves the same
topology to the SPA. A future direction change that updates one but not the other
fails here, at unit speed, instead of in the browser.
"""

from __future__ import annotations

from pathlib import Path

import re

from trackinizer.server.schema_gen import (
    substitute_schema_placeholders,
)
from trackinizer.server.sql import load_sql
from trackinizer.types import inquiries
from trackinizer.types.edges import edge_topology


# Each arm is ``(edge_kind = 'X' OR edge_kind IN (...)) AND from_kind ... AND to_kind
# ...``; this recovers the admitted kind sets per edge_kind so the test can set-compare
# them against :func:`edge_topology`.
def _schema_edge_arms() -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Parse ``edge_kind -> (from_kinds, to_kinds)`` from the schema edge CHECK."""
    sql = substitute_schema_placeholders(load_sql("schema"))
    # The edges table body, up to the next CREATE. The edge-validity CHECK (the
    # from/to-kind arms) sits AFTER the PRIMARY KEY line, so the block must span
    # the whole table definition, not stop at the key.
    start = sql.index("CREATE TABLE IF NOT EXISTS edges")
    block = sql[start : sql.index("CREATE", start + len("CREATE TABLE"))]

    arm = re.compile(
        r"edge_kind\s*(?:=\s*'(?P<one>\w+)'|IN\s*\((?P<many>[^)]*)\))\s+"
        r"AND\s+from_kind\s*(?:=\s*'(?P<f1>\w+)'|IN\s*\((?P<fmany>[^)]*)\))\s+"
        r"AND\s+to_kind\s*(?:=\s*'(?P<t1>\w+)'|IN\s*\((?P<tmany>[^)]*)\))",
        re.IGNORECASE | re.DOTALL,
    )

    def members(one: str | None, many: str | None) -> frozenset[str]:
        if one is not None:
            return frozenset({one})
        assert many is not None
        return frozenset(str(m.group(1)) for m in re.finditer(r"'(\w+)'", many))

    out: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for m in arm.finditer(block):
        kinds = members(m["one"], m["many"])
        from_kinds = members(m["f1"], m["fmany"])
        to_kinds = members(m["t1"], m["tmany"])
        for kind in kinds:
            out[kind] = (from_kinds, to_kinds)
    return out


def test_edge_topology_matches_schema_check() -> None:
    """``edge_topology`` and the schema edge-validity CHECK agree on every kind.

    Set comparison (not order) per edge_kind. This is the drift gate: a citation
    direction must show the SAME from/to kinds in both the Python registry and
    the SQL, so the SPA (which derives from the registry via ``/api/meta/edges``)
    can never lag the schema again.
    """
    schema_arms = _schema_edge_arms()
    topo = edge_topology()
    assert set(schema_arms) == set(topo), (
        "edge kinds differ between schema CHECK and edge_topology:\n"
        f"  schema-only: {sorted(set(schema_arms) - set(topo))}\n"
        f"  topology-only: {sorted(set(topo) - set(schema_arms))}"
    )
    for kind in topo:
        sf, st = schema_arms[kind]
        assert frozenset(topo[kind]["from_kinds"]) == sf, f"{kind} from_kinds drift"
        assert frozenset(topo[kind]["to_kinds"]) == st, f"{kind} to_kinds drift"


def test_produces_docstring_does_not_claim_universal_inference() -> None:
    """The authoritative provenance docstring stays truthful about neutrality.

    ``Inquiry.produces`` (types/inquiries.py) is the source-of-record for the
    provenance rule; ``edges.py`` cross-references it. Once a provenance-NEUTRAL
    kind exists, that prose can no longer claim inference fires on "ANY" kind or
    that there is "no per-kind exception set" -- both are false for a neutral
    kind. Pin the doc to the code so the two cannot drift again (the exact drift
    the cites_paper review caught).
    """
    # Field docstrings are bare string expressions Python discards, so read the
    # module source and scope to the ``produces`` field's docstring block.
    src = Path(inquiries.__file__).read_text(encoding="utf-8")
    start = src.index("    produces: tuple[InquiryEdge, ...] = ()")
    end = src.index("    produced_by: tuple[InquiryEdge, ...] = ()", start)
    produces_doc = src[start:end]
    assert "no per-kind exception set" not in produces_doc
    assert "PRODUCED_INFERENCE_NEUTRAL" in produces_doc


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
