"""
plugins/build_diagram.py — turns a description into a real diagram, offline.

Drop this single file into `plugins/`. Standard library only. It draws SVG
directly — no Graphviz, no mermaid-cli, no headless browser — so the picture
appears the same second the model asks for it, on a machine that has never
installed a diagramming tool.

THE IDEA
    Text cannot show a branch. Ask for "explain how the login flow works" in a
    chat and you get a paragraph; the same description here becomes boxes and
    arrows you can read at a glance and hand to somebody else.

WHAT IT UNDERSTANDS
    A mermaid-style shorthand the model can write directly:

        Start[Visitor opens the site] -> Check{Logged in?}
        Check ->|yes| Home[Dashboard]
        Check ->|no| Login[Login page] -> Check

    · A[text]    a rectangle  — a step
    · A(text)    a stadium    — a start or an end
    · A{text}    a diamond    — a decision
    · A((text))  a circle     — a junction
    · A -> B     an arrow, optionally labelled A ->|yes| B

HOW IT LAYS OUT
    Nodes go into layers by their longest path from the start, so nothing is
    ever drawn above something it depends on. Within a layer, a barycentre pass
    pulls each node towards the average position of its neighbours, which removes
    most of the crossed arrows — the same trick Graphviz uses, in forty lines
    instead of a dependency.

SETTINGS (Plugin Manager → Build Diagram)
    Direction (top-down or left-right), colour theme, and whether to open the
    browser automatically.
"""

from __future__ import annotations

import json
import re
import webbrowser
from pathlib import Path

PLUGIN = {
    "name": "build_diagram",
    "description": (
        "Draws a flow chart, process diagram or architecture sketch as an image "
        "and opens it. Use for: 'draw me a diagram of', 'make a flowchart for', "
        "'show the steps of', 'diagram this process', 'how does X work, visually'. "
        "Write the diagram in shorthand: nodes are A[Step], A(Start), A{Decision}, "
        "A((Junction)) and arrows are A -> B, with optional labels A ->|yes| B. "
        "Do NOT use screen_process to 'show' a diagram — this draws a real file."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "steps": {
                "type": "STRING",
                "description": (
                    "The diagram in shorthand, e.g. "
                    "'Start(Open app) -> Ask{Password OK?}; Ask ->|yes| Home[Dashboard]; "
                    "Ask ->|no| Retry[Try again] -> Ask'. "
                    "Separate independent chains with a semicolon or a new line."
                ),
            },
            "title": {"type": "STRING", "description": "Heading printed on the diagram."},
            "direction": {
                "type": "STRING",
                "description": "'TB' top-to-bottom (default) or 'LR' left-to-right.",
            },
        },
        "required": ["steps"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "build_diagram",
    "title": "Build Diagram / رسم المخططات",
    "fields": [
        {"key": "direction", "type": "choice", "label": "Direction / الاتجاه",
         "default": "TB", "options": ["TB", "LR"]},
        {"key": "theme", "type": "choice", "label": "Theme / السمة",
         "default": "hud", "options": ["hud", "light", "mono"]},
        {"key": "open_browser", "type": "toggle", "label": "Open the file automatically",
         "default": True},
    ],
}

#: Colour themes. `hud` matches the assistant's own screens; `light` is for
#: printing or pasting into a document; `mono` for a report.
_THEMES = {
    "hud": {"bg": "#000407", "line": "#46e0ff", "text": "#cfefff", "dim": "#2f7f9c",
            "node": "#03141d", "edge": "#2f8fa8", "edge_text": "#ffd23f",
            "title": "#46e0ff", "accent": "#0b2b3a"},
    "light": {"bg": "#ffffff", "line": "#1b3a4b", "text": "#12232e", "dim": "#5b7686",
              "node": "#eef6fa", "edge": "#7a9aab", "edge_text": "#9a6b00",
              "title": "#12374a", "accent": "#d6e9f2"},
    "mono": {"bg": "#ffffff", "line": "#111111", "text": "#111111", "dim": "#666666",
             "node": "#f4f4f4", "edge": "#444444", "edge_text": "#000000",
             "title": "#000000", "accent": "#e2e2e2"},
}

_SHAPES = {"rect": "[", "stadium": "(", "diamond": "{", "circle": "(("}
_CLOSERS = {"rect": "]", "stadium": ")", "diamond": "}", "circle": "))"}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<arrow>->|-->|=>)                          |  # an arrow
        \|(?P<elabel>[^|]{1,40})\|                    |  # |edge label|
        (?P<id>[A-Za-z_][A-Za-z0-9_]*(?:[ .\-][A-Za-z0-9_]+)*)   |
        (?P<open>\[|\(\(|\{|\()                       |
        (?P<close>\]|\)\)|\}|\))                       |
        (?P<other>.)
    )""",
    re.VERBOSE,
)


def _parse(text: str) -> tuple[dict[str, dict], list[dict]]:
    """
    Shorthand → (nodes, edges).

    A forgiving parser on purpose: the model writes this by hand, so a missing
    closing bracket yields a plain node rather than an exception, and anything
    unrecognisable is skipped instead of failing the whole diagram.
    """
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    for statement in re.split(r"[;\n]+", str(text or "")):
        if not statement.strip():
            continue
        # A leading "flowchart TB" from mermaid habit, and a "title:" prefix —
        # but only when nothing before the colon is bracketed, so a label that
        # itself contains a colon is left alone.
        statement = re.sub(r"^\s*(flowchart|graph)\s+\w+\s*", "", statement, flags=re.I)
        prefix = re.match(r"^\s*([A-Za-z][A-Za-z0-9_ ]{0,18}):\s*", statement)
        if prefix and not any(ch in statement[:prefix.end()] for ch in "[({"):
            statement = statement[prefix.end():]

        tokens = list(_TOKEN.finditer(statement))
        chain: list[str] = []
        pending_label = ""
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.group("arrow"):
                pending_label = ""
                i += 1
                continue
            if tok.group("elabel"):
                pending_label = tok.group("elabel").strip()
                i += 1
                continue
            if tok.group("id"):
                name = tok.group("id").strip()
                shape, label = "rect", name
                # does a shape bracket follow?
                if i + 1 < len(tokens):
                    nxt = tokens[i + 1]
                    opener = nxt.group("open")
                    if opener:
                        shape = {"[": "rect", "((": "circle", "{": "diamond", "(": "stadium"}[opener]
                        closer = _CLOSERS[shape]
                        body, j = [], i + 2
                        while j < len(tokens):
                            t2 = tokens[j]
                            if t2.group("close") == closer or (shape == "circle" and t2.group("close") == "))"):
                                break
                            body.append(t2.group(0))
                            j += 1
                        label = "".join(body).strip() or name
                        i = j
                node = nodes.setdefault(name, {"id": name, "label": label, "shape": shape})
                if label != name and node["label"] == name:
                    node["label"] = label
                if shape != "rect" and node["shape"] == "rect":
                    node["shape"] = shape
                if chain:
                    edges.append({"from": chain[-1], "to": name, "label": pending_label})
                chain.append(name)
                i += 1
                continue
            i += 1

    # Drop self-loops and duplicates; a repeated arrow says nothing new.
    seen: set[tuple[str, str]] = set()
    clean: list[dict] = []
    for edge in edges:
        key = (edge["from"], edge["to"])
        if edge["from"] == edge["to"] or key in seen:
            continue
        seen.add(key)
        clean.append(edge)
    return nodes, clean


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def _wrap(label: str, width: int = 22, max_lines: int = 3) -> list[str]:
    words, lines, current = label.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and len(" ".join(words)) > sum(len(l) for l in lines):
        lines[-1] = lines[-1][:width - 1].rstrip() + "…"
    return lines or [""]


def _back_edges(nodes: dict[str, dict], edges: list[dict]) -> set[tuple[str, str]]:
    """
    Find the edges that point backwards, by walking the graph depth-first.

    A process with a retry path contains a cycle, and a cycle cannot be layered:
    something must point up. Left alone, the layout tries to put the retry
    target *below* the retry itself, which pushes the entire diagram down one
    row per loop and produces a tall thin picture full of long lines. Pulling
    those few edges out first — the same first step Graphviz takes — leaves a
    clean top-to-bottom DAG, and the retry is drawn as the single backwards
    arrow it actually is.
    """
    outgoing: dict[str, list[str]] = {n: [] for n in nodes}
    for edge in edges:
        if edge["to"] in outgoing:
            outgoing[edge["from"]].append(edge["to"])

    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(nodes, WHITE)
    back: set[tuple[str, str]] = set()

    for root in nodes:
        if colour[root] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        colour[root] = GREY
        while stack:
            name, index = stack[-1]
            if index < len(outgoing[name]):
                stack[-1] = (name, index + 1)
                nxt = outgoing[name][index]
                if colour.get(nxt) == GREY:
                    back.add((name, nxt))          # points at our own ancestor
                elif colour.get(nxt) == WHITE:
                    colour[nxt] = GREY
                    stack.append((nxt, 0))
            else:
                colour[name] = BLACK
                stack.pop()
    return back


def _layer_nodes(nodes: dict[str, dict], edges: list[dict]) -> dict[str, int]:
    """
    Longest-path layering over the forward edges only.

    Each node sits one row below the deepest thing that leads to it, which is
    what makes a diagram read top-down: nothing is ever drawn above something
    it depends on.
    """
    back = _back_edges(nodes, edges)
    incoming: dict[str, list[str]] = {n: [] for n in nodes}
    for edge in edges:
        if (edge["from"], edge["to"]) in back:
            continue                                # drawn as a retry, not a dependency
        incoming[edge["to"]].append(edge["from"])

    layer = {n: 0 for n in nodes}
    for _ in range(len(nodes) + 2):
        changed = False
        for name, sources in incoming.items():
            if not sources:
                continue
            best = max(layer[s] for s in sources if s in layer) + 1
            if best > layer[name]:
                layer[name] = best
                changed = True
        if not changed:
            break
    return layer


def _order_layers(nodes: dict[str, dict], edges: list[dict],
                  layer: dict[str, int]) -> dict[int, list[str]]:
    buckets: dict[int, list[str]] = {}
    for name in nodes:
        buckets.setdefault(layer[name], []).append(name)
    neighbours: dict[str, list[str]] = {n: [] for n in nodes}
    for edge in edges:
        neighbours[edge["from"]].append(edge["to"])
        neighbours[edge["to"]].append(edge["from"])

    for depth in sorted(buckets):
        if depth == 0:
            continue
        previous = buckets.get(depth - 1, [])
        index = {name: i for i, name in enumerate(previous)}

        def key(name: str) -> float:
            positions = [index[n] for n in neighbours[name] if n in index]
            return sum(positions) / len(positions) if positions else 1e6

        buckets[depth] = sorted(buckets[depth], key=key)
    return buckets


def _layout(nodes: dict[str, dict], edges: list[dict], direction: str) -> dict:
    """Returns {nodes: {...with x,y,w,h}, edges: [...], width, height} in pixels."""
    if not nodes:
        return {"nodes": {}, "edges": [], "width": 520, "height": 240}

    layer = _layer_nodes(nodes, edges)
    buckets = _order_layers(nodes, edges, layer)

    # Node size from its own wrapped text; two lines minimum so short labels do
    # not sit in a squashed box next to a tall one.
    for name, node in nodes.items():
        node["lines"] = _wrap(node["label"])
        longest = max(len(line) for line in node["lines"])
        text_w = longest * 7.6 + 9
        text_h = len(node["lines"]) * 17
        node["w"] = max(112, min(268, text_w))
        node["h"] = max(48, 20 + text_h)

        if node["shape"] == "diamond":
            # A diamond is only as wide as its middle. Text that fits a
            # rectangle runs out through the sloping sides, which is exactly
            # what it did before this line existed: the usable width at the
            # text's own height is w·(1 − text_h/h), so w has to grow by the
            # reciprocal of that — plus a margin, because it looks cramped
            # even when it technically fits.
            node["w"] = min(420, (text_w + 26) * 1.55)
            node["h"] = max(76, text_h * 1.9 + 46)
        elif node["shape"] == "circle":
            node["w"] = max(node["w"], 96)
            node["h"] = max(node["h"], 96)

    gap_main = 86          # between layers — room for the edge labels
    gap_cross = 30         # between neighbours in a layer

    vertical = direction.upper() != "LR"
    if vertical:
        y = 0
        for depth in sorted(buckets):
            names = buckets[depth]
            tallest = max(nodes[n]["h"] for n in names)
            x = 0
            for name in names:
                node = nodes[name]
                node["x"] = x
                node["y"] = y + (tallest - node["h"]) / 2
                x += node["w"] + gap_cross
            y += tallest + gap_main
        width = max((sum(nodes[n]["w"] for n in names) + gap_cross * (len(names) - 1))
                    for names in buckets.values())
        height = y - gap_main
    else:
        x = 0
        for depth in sorted(buckets):
            names = buckets[depth]
            widest = max(nodes[n]["w"] for n in names)
            y = 0
            for name in names:
                node = nodes[name]
                node["y"] = y
                node["x"] = x + (widest - node["w"]) / 2
                y += node["h"] + gap_cross
            x += widest + gap_main
        width = x - gap_main
        height = max((sum(nodes[n]["h"] for n in names) + gap_cross * (len(names) - 1))
                     for names in buckets.values())

    # Centre every layer on the widest one, so the diagram looks deliberate.
    for depth in sorted(buckets):
        names = buckets[depth]
        if vertical:
            span = sum(nodes[n]["w"] for n in names) + gap_cross * (len(names) - 1)
            shift = (width - span) / 2
            for name in names:
                nodes[name]["x"] += shift
        else:
            span = sum(nodes[n]["h"] for n in names) + gap_cross * (len(names) - 1)
            shift = (height - span) / 2
            for name in names:
                nodes[name]["y"] += shift

    # A retry lane runs outside the rightmost box, so the canvas has to be wider
    # than the boxes alone — otherwise the loop is drawn off the edge of it.
    if any(_is_back(nodes[e["from"]], nodes[e["to"]], vertical) for e in edges):
        if vertical:
            width += 46
        else:
            height += 46
    return {"nodes": nodes, "edges": edges, "width": width, "height": height,
            "direction": direction.upper()}


def _is_back(a: dict, b: dict, vertical: bool) -> bool:
    return (b["y"] < a["y"] + a["h"] - 1) if vertical else (b["x"] < a["x"] + a["w"] - 1)


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

def _shape_path(node: dict) -> str:
    x, y, w, h = node["x"], node["y"], node["w"], node["h"]
    if node["shape"] == "diamond":
        return f"M{x + w / 2},{y} L{x + w},{y + h / 2} L{x + w / 2},{y + h} L{x},{y + h / 2} Z"
    if node["shape"] == "circle":
        rx, ry = w / 2, h / 2
        return (f"M{x},{y + ry} A{rx},{ry} 0 1,0 {x + w},{y + ry} "
                f"A{rx},{ry} 0 1,0 {x},{y + ry} Z")
    if node["shape"] == "stadium":
        r = h / 2
        return (f"M{x + r},{y} L{x + w - r},{y} A{r},{r} 0 0,1 {x + w - r},{y + h} "
                f"L{x + r},{y + h} A{r},{r} 0 0,1 {x + r},{y} Z")
    r = 7
    return (f"M{x + r},{y} L{x + w - r},{y} Q{x + w},{y} {x + w},{y + r} "
            f"L{x + w},{y + h - r} Q{x + w},{y + h} {x + w - r},{y + h} "
            f"L{x + r},{y + h} Q{x},{y + h} {x},{y + h - r} "
            f"L{x},{y + r} Q{x},{y} {x + r},{y} Z")


def _edge_path(a: dict, b: dict, vertical: bool) -> tuple[str, float, float]:
    """
    An elbow from one box to the next, plus where its label hangs.

    Labels are always placed horizontally and to the side of the line. Rotated
    labels were the first attempt and they read badly: the eye has to tilt, and
    a long word along a short segment pokes into the boxes at either end.

    A backwards edge — a retry, a loop — leaves the right face, runs out to a
    dedicated lane clear of every box, and comes back in. Drawing it straight
    down through the middle would run it through whatever sits between.
    """
    if vertical:
        forward = b["y"] >= a["y"] + a["h"] - 1
        if forward:
            x1, y1 = a["x"] + a["w"] / 2, a["y"] + a["h"]
            x2, y2 = b["x"] + b["w"] / 2, b["y"]
            if abs(x1 - x2) < 1.5:
                return f"M{x1:.0f},{y1:.0f} L{x2:.0f},{y2:.0f}", x1 + 9, (y1 + y2) / 2 + 4
            mid = (y1 + y2) / 2
            return (f"M{x1:.0f},{y1:.0f} L{x1:.0f},{mid:.0f} L{x2:.0f},{mid:.0f} "
                    f"L{x2:.0f},{y2:.0f}"), x2 + 9, mid + 4
        x1, y1 = a["x"] + a["w"], a["y"] + a["h"] / 2
        x2, y2 = b["x"] + b["w"], b["y"] + b["h"] / 2
        lane = max(x1, x2) + 30
        return (f"M{x1:.0f},{y1:.0f} L{lane:.0f},{y1:.0f} L{lane:.0f},{y2:.0f} "
                f"L{x2:.0f},{y2:.0f}"), lane + 7, (y1 + y2) / 2 + 4

    forward = b["x"] >= a["x"] + a["w"] - 1
    if forward:
        x1, y1 = a["x"] + a["w"], a["y"] + a["h"] / 2
        x2, y2 = b["x"], b["y"] + b["h"] / 2
        if abs(y1 - y2) < 1.5:
            return f"M{x1:.0f},{y1:.0f} L{x2:.0f},{y2:.0f}", (x1 + x2) / 2, y1 - 8
        mid = (x1 + x2) / 2
        return (f"M{x1:.0f},{y1:.0f} L{mid:.0f},{y1:.0f} L{mid:.0f},{y2:.0f} "
                f"L{x2:.0f},{y2:.0f}"), mid - 4, y2 - 8
    x1, y1 = a["x"] + a["w"] / 2, a["y"] + a["h"]
    x2, y2 = b["x"] + b["w"] / 2, b["y"] + b["h"]
    lane = max(y1, y2) + 30
    return (f"M{x1:.0f},{y1:.0f} L{x1:.0f},{lane:.0f} L{x2:.0f},{lane:.0f} "
            f"L{x2:.0f},{y2:.0f}"), (x1 + x2) / 2, lane + 14


def _svg(plan: dict, title: str, theme_name: str) -> str:
    theme = _THEMES.get(theme_name, _THEMES["hud"])
    pad, head = 34, (54 if title else 20)
    width = int(plan["width"] + pad * 2)
    height = int(plan["height"] + pad * 2 + head)
    vertical = plan.get("direction", "TB") != "LR"

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Segoe UI,Helvetica,Arial,sans-serif">',
        f'<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0,1 L9,5 L0,9 z" fill="{theme["edge"]}"/></marker></defs>',
        f'<rect width="{width}" height="{height}" fill="{theme["bg"]}"/>',
        f'<g transform="translate({pad},{pad + head})">',
    ]
    if title:
        parts.append(
            f'<text x="0" y="-24" fill="{theme["title"]}" font-size="17" '
            f'font-weight="600" letter-spacing="0.4">{_escape(title)}</text>')

    nodes = plan["nodes"]
    label_marks: list[dict] = []          # collected first, positioned later

    for edge in plan["edges"]:
        a, b = nodes.get(edge["from"]), nodes.get(edge["to"])
        if not a or not b:
            continue
        path, lx, ly = _edge_path(a, b, vertical)
        parts.append(f'<path d="{path}" fill="none" stroke="{theme["edge"]}" '
                     f'stroke-width="1.7" marker-end="url(#a)" stroke-linejoin="round"/>')
        if edge.get("label"):
            anchor = "middle" if not vertical and ly < b["y"] else "start"
            label_marks.append({"x": lx, "y": ly, "anchor": anchor,
                                "text": str(edge["label"])})

    for name, node in nodes.items():
        parts.append(f'<path d="{_shape_path(node)}" fill="{theme["node"]}" '
                     f'stroke="{theme["line"]}" stroke-width="1.9" stroke-linejoin="round"/>')
        line_height = 17
        first = node["y"] + node["h"] / 2 - (len(node["lines"]) - 1) * line_height / 2
        for index, line in enumerate(node["lines"]):
            parts.append(
                f'<text x="{node["x"] + node["w"] / 2:.0f}" '
                f'y="{first + index * line_height + 4.5:.0f}" '
                f'fill="{theme["text"]}" font-size="13" text-anchor="middle">'
                f'{_escape(line)}</text>')

    # Labels are written last, on top of everything, and only after being pushed
    # apart — two edges from one decision can land their words in the same few
    # pixels, and a label hidden behind a box is worse than no label at all.
    _spread_labels(label_marks)
    for mark in label_marks:
        common = (f'x="{mark["x"]:.0f}" y="{mark["y"]:.0f}" font-size="11.5" '
                  f'font-weight="700" text-anchor="{mark["anchor"]}"')
        # Two copies: the one underneath is thick and the colour of the
        # background, the one on top is the real label. That carves a gap out of
        # the arrow so the words stay readable where they cross it — and unlike
        # paint-order, it works in every SVG renderer, including the ones that
        # ignore that attribute and would paint the halo over the text.
        parts.append(f'<text {common} fill="{theme["bg"]}" stroke="{theme["bg"]}" '
                     f'stroke-width="4.5" stroke-linejoin="round">'
                     f'{_escape(mark["text"])}</text>')
        parts.append(f'<text {common} fill="{theme["edge_text"]}">'
                     f'{_escape(mark["text"])}</text>')

    parts.append("</g></svg>")
    return "\n".join(parts)


def _spread_labels(marks: list[dict], min_dy: float = 14.0, min_dx: float = 52.0) -> None:
    """
    Nudge labels apart in place so no two of them print on top of each other.

    Two edges leaving the same decision fan out to targets that can sit at
    almost the same height, and their "yes"/"no" land in the same few pixels.
    Sorted by height, then pushed down whenever one is too close to the one
    before it — enough to separate them, and never enough to hide the arrow.
    """
    order = sorted(range(len(marks)), key=lambda i: (marks[i]["y"], marks[i]["x"]))
    for position in range(1, len(order)):
        current = order[position]
        for earlier in range(position):
            previous = order[earlier]
            if abs(marks[current]["x"] - marks[previous]["x"]) < min_dx and \
                    abs(marks[current]["y"] - marks[previous]["y"]) < min_dy:
                marks[current]["y"] = marks[previous]["y"] + min_dy
                order = sorted(order, key=lambda i: (marks[i]["y"], marks[i]["x"]))
                break


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def to_mermaid(text: str, direction: str = "TB") -> str:
    """The same diagram as mermaid source, for pasting into a doc or a chat."""
    nodes, edges = _parse(text)
    lines = [f"flowchart {direction.upper()}"]
    for name, node in nodes.items():
        label = node["label"].replace('"', "'")
        if node["shape"] == "diamond":
            lines.append("    " + name + "{" + '"' + label + '"' + "}")
        elif node["shape"] == "circle":
            lines.append("    " + name + '((("' + label + '")))')
        elif node["shape"] == "stadium":
            lines.append("    " + name + '(["' + label + '"])')
        else:
            lines.append("    " + name + '["' + label + '"]')
    for edge in edges:
        if edge.get("label"):
            lines.append(f'    {edge["from"]} -->|{edge["label"]}| {edge["to"]}')
        else:
            lines.append(f'    {edge["from"]} --> {edge["to"]}')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _output_dir() -> Path:
    try:
        from memory.config_manager import BASE_DIR
        base = Path(BASE_DIR)
    except Exception:
        base = Path(__file__).resolve().parent.parent
    folder = base / "config" / ".plugin_cache" / "diagrams"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _config() -> dict:
    values = {"direction": "TB", "theme": "hud", "open_browser": True}
    try:
        from memory.config_manager import get_plugin_config
        for key, value in (get_plugin_config("build_diagram") or {}).items():
            if value not in (None, ""):
                values[key] = value
    except Exception:
        pass
    return values


def build(steps: str, title: str = "", direction: str | None = None,
          theme: str | None = None) -> tuple[Path, str, str]:
    """Draw it and return (svg path, mermaid source, summary line)."""
    config = _config()
    direction = (direction or config["direction"] or "TB").upper()
    theme = (theme or config["theme"] or "hud").lower()

    nodes, edges = _parse(steps)
    if not nodes:
        raise ValueError("no boxes found in that description")
    plan = _layout(nodes, edges, direction)

    folder = _output_dir()
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "diagram").lower()).strip("-")[:40] or "diagram"
    path = folder / f"{slug}.svg"
    path.write_text(_svg(plan, title, theme), encoding="utf-8")

    mermaid = to_mermaid(steps, direction)
    (folder / f"{slug}.mmd").write_text(mermaid, encoding="utf-8")
    (folder / f"{slug}.json").write_text(json.dumps(
        {"title": title, "nodes": nodes, "edges": edges}, ensure_ascii=False,
        indent=1), encoding="utf-8")

    summary = (f"{len(nodes)} boxes and {len(edges)} arrows, laid out "
               f"{'top to bottom' if direction != 'LR' else 'left to right'}")
    return path, mermaid, summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    try:
        steps = parameters.get("steps") or parameters.get("description") or ""
        if not str(steps).strip():
            return ("Give me the steps and I will draw them, sir — for example "
                    "'Start(Open app) -> Ask{Password OK?}; Ask ->|yes| Home'.")
        title = str(parameters.get("title") or "").strip()
        path, mermaid, summary = build(steps, title,
                                       parameters.get("direction"),
                                       parameters.get("theme"))

        config = _config()
        opened = False
        if config.get("open_browser", True):
            try:
                webbrowser.open(path.as_uri())
                opened = True
            except Exception:
                pass

        where = "I have opened it" if opened else f"It is saved as {path.name}"
        result = (f"The diagram is drawn, sir — {summary}. {where}"
                  + (f", titled '{title}'." if title else "."))

    except Exception as exc:
        return f"Sir, build_diagram failed: {exc}"

    if player:
        try:
            player.write_log(f"JARVIS: {result}")
        except Exception:
            pass
    return result


if __name__ == "__main__":
    sample = ("Start(Visitor opens the site) -> Check{Already logged in?}; "
              "Check ->|yes| Home[Dashboard]; "
              "Check ->|no| Login[Enter email and password] -> Verify{Password correct?}; "
              "Verify ->|no| Limit[Count failed attempts] -> Lock{Three failures?}; "
              "Lock ->|yes| Reset[Lock and email a reset link]; "
              "Lock ->|no| Login; Verify ->|yes| Session[Create session token] -> Home")
    path, mermaid, summary = build(sample, "Login flow", "TB", "hud")
    print(f"file:   {path}  ({path.stat().st_size / 1024:.1f} KB)")
    print(f"layout: {summary}")
    print("\nmermaid:\n" + mermaid)
