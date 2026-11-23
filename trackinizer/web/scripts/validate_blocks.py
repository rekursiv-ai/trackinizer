#!/usr/bin/env python3
"""The pre-mount gate for a blocks answer.

Fails loudly on every mistake that has reached the canvas: named HTML entities in
drawing SVG, unparsable SVG, animates the drawing auditor will drop (x/y),
catalog-foreign nodes, budget mismatches, wrong plate figure shapes, and em dashes.
"""

from pathlib import Path

import json
import re
import sys
import xml.etree.ElementTree as ET


# Every node the renderer has an arm for. It must stay the union of what
# `compose.tsx` draws and what DESIGN.md documents: a node missing here is
# REJECTED before it mounts, so an agent that read the brief and picked the right
# component got "not in the catalog" and fell back to prose. That is how the six
# richest nodes — pipeline, matrix_schematic, settlement, paths, code_context,
# review_accordion — were unreachable while being documented and rendered.
CATALOG = {
    "heading", "prose", "section", "drawing", "cells", "row", "marks", "code",
    "image", "figure", "plate", "field", "footnote", "chip", "register", "rule",
    "graph", "text", "tier", "card", "flow", "embed", "illustration",
    "paths", "pipeline", "matrix_schematic", "settlement", "code_context",
    "review_accordion",
}
ANIMATABLE = {"opacity", "fill-opacity", "stroke-opacity", "stroke", "stroke-dashoffset", "height", "width", "rx", "ry", "r"}
# The excerpt a code card draws itself: 6 lines before the first highlight and 11 after
# (`ui/code.tsx`). A `text` no longer than this, starting at line 1, was cut by hand.
CARD_WINDOW = 18

def main(path: str) -> None:
    """Check the composition at ``path`` and exit non-zero on the first set of faults."""
    problems: list[str] = []
    raw = Path(path).read_text(encoding="utf-8")
    # data-marker dashes inside plate table cells are the paper's own convention, not prose
    prose_text = re.sub(r'"\u2014"', '""', raw)
    if "\u2014" in prose_text:
        problems.append("em dash present in the answer prose (data-marker cells are exempt)")
    d = json.loads(raw)
    compose = d.get("compose", [])
    used = {}
    for k, item in enumerate(compose):
        node = item.get("node")
        used[node] = used.get(node, 0) + 1
        if node not in CATALOG:
            problems.append(f"compose[{k}]: node '{node}' is not in the catalog")
        if node == "drawing":
            body = item.get("body", "")
            named = sorted(set(re.findall(r"&[a-zA-Z]+;", body)))
            if named:
                problems.append(f"drawing[{k}]: named HTML entities will be rejected: {named}")
            try:
                # S314: this parses the agent's own composition from disk, asking only
                # whether it is well-formed. It is not network input, and nothing is
                # resolved or evaluated from the tree.
                ET.fromstring('<svg xmlns="http://www.w3.org/2000/svg">' + body + "</svg>")  # noqa: S314
            except ET.ParseError as e:
                problems.append(f"drawing[{k}]: SVG does not parse as XML: {e}")
            problems.extend(
                f"drawing[{k}]: animate on '{m.group(1)}' will be dropped; use animateTransform or an allowed attribute"
                for m in re.finditer(r'<animate(?![A-Za-z])[^>]*attributeName="([a-zA-Z]+)"', body)
                if m.group(1) not in ANIMATABLE
            )
            if not item.get("label"):
                problems.append(f"drawing[{k}]: a drawing with no label is refused")
        if node == "code":
            lines = item.get("text", "").count("\n") + 1
            frm = item.get("from", 1)
            problems.extend(
                f"code[{k}]: note line {note['line']} outside excerpt {frm}..{frm + lines - 1}"
                for note in item.get("notes", [])
                if not (frm <= note["line"] < frm + lines)
            )
            # A pre-excerpted file. The card cuts its own 18-line window and the header
            # promises "full code · N lines", so handing over only the interesting lines
            # makes that promise a lie: clicking it reopens what was already on screen.
            # The node wants the whole file with `from: 1`.
            if lines <= CARD_WINDOW and frm == 1 and lines > 1:
                problems.append(
                    f"code[{k}]: text is {lines} lines from line 1 — that is an excerpt, not a file. "
                    f"Pass the whole file: the card cuts its own window and 'full code' must open the rest",
                )
        if node == "plate":
            for pk, pi in enumerate(item.get("items", [])):
                if pi.get("node") == "figure" and "kind" not in pi.get("figure", {}):
                    problems.append(f"plate[{pk}]: figure needs kind (table/chart/series/equation)")
    budget = d.get("budget", {})
    for name, count in used.items():
        if name in budget and budget[name] < count:
            problems.append(f"budget: '{name}' used {count}, budget allows {budget[name]}")
    problems.extend(
        f"budget: '{name}' declared but never rendered — the composition omits a component the budget promises"
        for name in budget
        if name not in used
    )
    if problems:
        for p in problems:
            print("FAIL:", p)
        sys.exit(1)
    print(f"PASS: {len(compose)} items, budget honored, SVG parses clean, no entities, no em dashes.")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "public/blocks-latest.json")
