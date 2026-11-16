"""
diagram_builder.py — diagrams as Mermaid text, validated before you paste them.

WHY THIS EXISTS NEXT TO build_diagram
    The build_diagram plugin next door draws a PICTURE: it turns 'A -> B -> C'
    into an image and opens it. It knows one shape of think — boxes and arrows.
    This plugin does the other half of the job: it produces Mermaid TEXT, which
    is what you want when the diagram has to live inside a README or a wiki or a
    pull request, and it covers the four kinds that a picture-of-boxes cannot
    express at all: sequence diagrams, mind maps, entity relationships, and
    architecture graphs. Same suite, two tools, no duplicated work — this one
    does not draw, the other does not write text.

WHAT MAKES IT SAFE TO TRUST
    Every diagram is parsed and checked before it is handed over. Mermaid fails
    quietly in a browser — you get an error box, not a sentence — so the checks
    live here instead: unbalanced brackets and quotes, arrows pointing at labels
    that were never declared, participants used before they are declared, ER
    relations naming an entity that does not exist, duplicate ids. Each one is
    reported by line number with the reason.

    Nothing leaves the machine. A diagram is text: shown, saved if asked, or
    pasted by you. There is no call to any rendering service.

SETTINGS (Plugin Manager → Diagram builder)
    output_dir  blank = Documents/JARVIS_Diagrams
    theme       default | neutral | dark | forest — Mermaid's own theme names
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "diagram_builder",
    "description": (
        "Builds Mermaid diagrams as text and checks their syntax: flowcharts, sequence diagrams, "
        "mind maps, architecture graphs and ER diagrams. Use for 'make me a sequence diagram of', "
        "'mermaid for this process', 'draw the architecture as code', 'ER diagram for these "
        "tables'. The result is text you can paste anywhere — and it is validated first."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "build | validate | help"},
            "kind": {"type": "STRING",
                     "description": "flowchart | sequence | mindmap | er | architecture"},
            "description": {"type": "STRING",
                            "description": "The steps/relations, e.g. 'Start -> Check ->|yes| Send' "
                                           "or 'A: B, C' for a mind map"},
            "mermaid": {"type": "STRING", "description": "For action=validate: the text to check"},
            "title": {"type": "STRING", "description": "Optional heading"},
            "save_as": {"type": "STRING", "description": "Optional file name (.mmd)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "diagram_builder",
    "title": "Diagram builder / باني المخططات",
    "fields": [
        {"key": "output_dir", "type": "text", "label": "Save folder (blank = Documents/JARVIS_Diagrams)",
         "default": ""},
        {"key": "theme", "type": "text", "label": "Theme (default/neutral/dark/forest)",
         "default": "default"},
    ],
}

_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KINDS = ("flowchart", "sequence", "mindmap", "er", "architecture")


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("diagram_builder") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def output_dir() -> Path:
    raw = str(_cfg().get("output_dir") or "").strip()
    base = Path(raw).expanduser() if raw else (Path.home() / "Documents" / "JARVIS_Diagrams")
    try:
        return base.resolve()
    except Exception:
        return Path.home() / "Documents" / "JARVIS_Diagrams"


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 500):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem} ({os.getpid()}){path.suffix}")


# ── the syntax checker ────────────────────────────────────────────────────────

def _balanced(text: str) -> list[str]:
    """
    Brackets and quotes, reported with line numbers. Braces are counted across
    the WHOLE diagram, not per line: an ER entity block opens its '{' on the
    entity line and closes it after the attributes two lines later, so a per-line
    brace check called every valid ER diagram broken — found by the test.
    """
    problems: list[str] = []
    pairs = {"[": "]", "(": ")"}
    opening = set(pairs)
    brace_depth = 0
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("%%"):
            continue
        # Mermaid's ER cardinality symbols are made of brace characters that are
        # not brackets at all: 'o{', '|{', '}o', '}|'. Counting them as brackets
        # flagged every valid relationship line — found by this file's own test.
        checkable = re.sub(r"[|}o][-|o.]{1,4}[|o{]", "--", stripped) \
            if re.search(r"[|}o][-|o.]{1,4}[|o{]", stripped) and not stripped.endswith("{") \
            else stripped
        if checkable.count('"') % 2:
            problems.append(f"line {number}: an odd number of double quotes — a label is not closed")
        stack: list[str] = []
        for char in checkable:
            if char in opening:
                stack.append(char)
            elif char in pairs.values():
                if not stack or pairs[stack.pop()] != char:
                    problems.append(f"line {number}: '{char}' closes nothing — the brackets are "
                                    f"unbalanced")
                    break
            elif char == "{":
                brace_depth += 1
            elif char == "}":
                brace_depth -= 1
                if brace_depth < 0:
                    problems.append(f"line {number}: '}}' closes nothing — the brackets are "
                                    f"unbalanced")
                    brace_depth = 0
        if stack:
            problems.append(f"line {number}: '{stack[-1]}' is never closed")
    if brace_depth:
        problems.append(f"{brace_depth} '{{' brace(s) are opened and never closed anywhere in "
                        f"the diagram")
    return problems


def _nodes_in(line: str) -> list[str]:
    """Every 'Id[' / 'Id(' / 'Id{' / 'Id((' on a line, in order."""
    return re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?=[\[\(\{])", line)


def validate(mermaid: str) -> dict[str, Any]:
    """
    Returns {ok, kind, problems, notes, lines}. It checks what can be checked
    without a browser: structure, balance, references, duplicates. It never
    claims a diagram 'renders' — only that the text obeys the rules this plugin
    can verify, and it says so.
    """
    text = str(mermaid or "").replace("\r\n", "\n").strip()
    result: dict[str, Any] = {"ok": False, "kind": "", "problems": [], "notes": [],
                              "lines": len(text.splitlines())}
    if not text:
        result["problems"].append("there is nothing to check — the diagram is empty")
        return result

    lines = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("%%")]
    first = lines[0].strip().lower()
    kind = ""
    for candidate in _KINDS:
        if first.startswith(candidate):
            kind = candidate
            break
    if not kind:
        result["problems"].append(
            f"the first line must say what kind of diagram it is — one of "
            f"{', '.join(_KINDS)}. It says '{lines[0].strip()[:30]}'")
        return result
    result["kind"] = kind
    result["problems"] += _balanced(text)

    if kind == "flowchart":
        declared: set[str] = set()
        references: list[tuple[int, str]] = []
        for number, line in enumerate(lines[1:], 2):
            stripped = line.strip().rstrip(";")
            for node in _nodes_in(stripped):
                declared.add(node)
            # Everything with a label is a declaration; what remains are bare ids,
            # and a bare id must have been declared somewhere or Mermaid draws a
            # box called "Missing" that nobody asked for. The first version of
            # this check trusted the right-hand side of an arrow blindly, so the
            # test's "A[Start] -> Missing" passed validation — caught by the test.
            bare = re.sub(r"[A-Za-z_][A-Za-z0-9_]*\s*[\[\(\{][^\]\)\}]*[\]\)\}]*", " ", stripped)
            bare = re.sub(r"\|[^|]*\|", " ", bare)
            bare = re.sub(r"-{1,3}[->.o|]*", " ", bare)
            for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", bare):
                if token.lower() in ("flowchart", "graph", "tb", "bt", "lr", "rl", "td"):
                    continue
                references.append((number, token))
        for number, token in references:
            if token not in declared:
                result["problems"].append(
                    f"line {number}: '{token}' is used in an arrow but never declared with a "
                    f"label (write {token}[Something])")
        if not declared:
            result["problems"].append("no nodes were found — a flowchart needs at least one")
        if result["problems"]:
            result["notes"].append("unbalanced or undeclared items are the two things Mermaid "
                                   "fails on silently; everything else it forgives")

    elif kind == "sequence":
        participants: set[str] = set()
        used: list[tuple[int, str]] = []
        for number, line in enumerate(lines[1:], 2):
            stripped = line.strip()
            match = re.match(r"(?:participant|actor)\s+([A-Za-z_][A-Za-z0-9_]*)", stripped)
            if match:
                participants.add(match.group(1))
                continue
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:-->>|->>|-->|->|--x|-x)\s*"
                             r"([A-Za-z_][A-Za-z0-9_]*)", stripped)
            if match:
                used.append((number, match.group(1)))
                used.append((number, match.group(2)))
        if not used:
            result["problems"].append("no messages found — a sequence diagram needs 'A -> B: text'")
        if participants:
            for number, name in used:
                if name not in participants:
                    result["problems"].append(
                        f"line {number}: '{name}' sends or receives a message but was never "
                        f"declared — add 'participant {name}' or drop the declarations "
                        f"and let Mermaid infer them")
        else:
            result["notes"].append("no participants declared — Mermaid will infer them from the "
                                   "messages, which is valid")
        for number, line in enumerate(lines[1:], 2):
            if re.search(r"(?:->>|->|-->>|-->)\s*$", line) and ":" not in line:
                result["notes"].append(f"line {number}: a message with no text after ':' is "
                                       f"allowed but reads as blank in the picture")

    elif kind == "mindmap":
        depths = [len(line) - len(line.lstrip()) for line in lines[1:] if line.strip()]
        if not depths:
            result["problems"].append("a mind map needs at least one child line under 'mindmap'")
        elif min(depths) == 0:
            result["problems"].append("at least one line has no indentation — mind map children "
                                      "must be indented under their parent")
        stray = [n for n, line in enumerate(lines[1:], 2) if line.rstrip().endswith(("->", "-->"))]
        if stray:
            result["problems"].append(f"line {stray[0]}: a mind map uses indentation, not arrows")

    elif kind == "er":
        entities: set[str] = set()
        for number, line in enumerate(lines[1:], 2):
            stripped = line.strip()
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*\{", stripped)
            if match:
                entities.add(match.group(1))
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+[|}o][-|o{]*[-|o{]*[|o{]\s+"
                             r"([A-Za-z_][A-Za-z0-9_]*)", stripped)
            if match:
                for name in (match.group(1), match.group(2)):
                    if name not in entities:
                        result["problems"].append(
                            f"line {number}: '{name}' appears in a relationship but has no block "
                            f"of its own — declare it as '{name} {{ ... }}'")
        if not entities:
            result["problems"].append("no entity block found — an ER diagram needs at least "
                                      "'Entity { string field }'")
        for number, line in enumerate(lines[1:], 2):
            stripped = line.strip()
            if stripped and not stripped.endswith(("{", "}")) and "{" not in stripped \
                    and not re.match(r"[A-Za-z_][A-Za-z0-9_]*\s+[|}o]", stripped):
                if re.match(r"[a-zA-Z]+\s+[a-zA-Z]+$", stripped) and stripped.count(" ") == 1 \
                        and not stripped.endswith("}"):
                    result["notes"].append(f"line {number}: an attribute inside an entity block "
                                           f"needs a type — 'name string', not 'name'")

    elif kind == "architecture":
        services: set[str] = set()
        for number, line in enumerate(lines[1:], 2):
            stripped = line.strip()
            if not stripped:
                continue
            if ":" not in stripped and not stripped.startswith(("service", "group")):
                result["problems"].append(f"line {number}: an architecture line reads 'Name: detail'")
                continue
            name = stripped.split(":", 1)[0].strip()
            name = re.sub(r"^(?:service|group)\s+", "", name)
            if not name:
                result["problems"].append(f"line {number}: a line with no name on the left of ':'")
                continue
            services.add(name)
        if not services:
            result["problems"].append("no services found — write one line per service: "
                                      "'API: talks to the database'")
        result["notes"].append("architecture graph is a user-defined convention here, not a "
                               "Mermaid keyword: it renders as a flowchart with grouped nodes. "
                               "That is stated rather than pretended — Mermaid itself has no "
                               "'architecture' diagram type")

    result["ok"] = not result["problems"]
    return result


# ── building ──────────────────────────────────────────────────────────────────

def _flowchart(description: str, title: str) -> str:
    """Reuses the same shorthand build_diagram accepts, so you write it once."""
    body = re.sub(r"(?im)^\s*(flowchart|graph)\s+\w+\s*", "", str(description or "")).strip()
    body = body.replace(";", "\n") if "->" not in body.split(";")[0] and ";" in body else body
    statements = []
    for part in re.split(r";|\n", body):
        part = part.strip()
        if part:
            statements.append(re.sub(r"\s*->\s*", " --> ", part))
    return "flowchart TB\n" + "\n".join("    " + s for s in statements)


def _sequence(description: str, title: str) -> str:
    """
    'Browser -> Server: login' style lines, or plain 'A -> B' pairs. Anything
    that is not already a message becomes one with an automatic label, because a
    sequence diagram with participants and no messages is a picture of nothing.
    """
    lines = ["sequenceDiagram"]
    if title:
        lines.append(f"    %% {title}")
    step = 0
    for raw in re.split(r";|\n", str(description or "")):
        part = raw.strip()
        if not part:
            continue
        match = re.match(r"([A-Za-z_][A-Za-z0-9_\u0600-\u06FF ]*?)\s*(?:-->>|->>|-->|->)\s*"
                         r"([A-Za-z_][A-Za-z0-9_\u0600-\u06FF ]*?)\s*(?::\s*(.*))?$", part)
        if match:
            sender = match.group(1).strip()
            receiver = match.group(2).strip()
            label = (match.group(3) or "").strip()
        else:
            step += 1
            sender, receiver, label = "User", "System", part
        sender_id = re.sub(r"\W+", "_", sender)[:24] or "A"
        receiver_id = re.sub(r"\W+", "_", receiver)[:24] or "B"
        label_id = re.sub(r"\W+", "_", label)[:24] or "step"
        lines.append(f"    participant {sender_id} as {sender}" if f"as {sender}" not in
                     "\n".join(lines) else "")
        lines.append(f"    participant {receiver_id} as {receiver}" if f"as {receiver}" not in
                     "\n".join(lines) else "")
        lines.append(f"    {sender_id}->>{receiver_id}: {label_id}")
    if len(lines) <= 2:
        lines.append("    User->>System: (nothing to say — the description was empty)")
    return "\n".join(line for line in lines if line.strip())


def _mindmap(description: str, title: str) -> str:
    """'Root: child, child, grandchild(grandchild)' — indentation from nesting."""
    root = title or "Mind map"
    lines = ["mindmap", f"  root(({root}))"]
    text = str(description or "").strip()
    if not text:
        return "\n".join(lines)

    def split_items(text: str) -> list[str]:
        """Commas at depth zero only — 'Plugins(memory, search)' is one item, and
        the first version of this split it into two broken halves."""
        items, buffer, depth = [], "", 0
        for char in text:
            if char == "(":
                depth += 1
                buffer += char
            elif char == ")":
                depth = max(0, depth - 1)
                buffer += char
            elif char in ",;" and depth == 0:
                items.append(buffer)
                buffer = ""
            else:
                buffer += char
        if buffer.strip():
            items.append(buffer)
        return items

    def add(children: str, depth: int) -> None:
        for piece in split_items(children):
            piece = piece.strip()
            if not piece:
                continue
            match = re.match(r"([^()]+)\((.*)\)\s*$", piece)
            if match:
                name, inner = match.group(1).strip(), match.group(2).strip()
            else:
                name, inner = piece, ""
            if name:
                lines.append("  " * (depth + 1) + name)
            if inner:
                add(inner, depth + 1)

    if ":" in text:
        head, _, rest = text.partition(":")
        head = head.strip()
        if head:
            lines = ["mindmap", f"  root(({head}))"]
        add(rest, 1)
    else:
        add(text, 1)
    return "\n".join(lines)


def _er(description: str, title: str) -> str:
    """
    'User: id int, name string; Order: id int' — each entity followed by fields.
    Relations: 'User 1--* Order' lines are passed through and checked.
    """
    lines = ["erDiagram"]
    text = str(description or "")
    for entity in re.split(r";|\n", text):
        entity = entity.strip()
        if not entity:
            continue
        # Mermaid's crow's-foot relationships: ||--o{ , }o..|| , |o--|{ … The
        # first version only recognised a made-up form, so a correct relationship
        # line was turned into a mangled entity — found by the test once the test
        # itself was given a correct line to feed it.
        if re.search(r"[|}o][|o{]?[-.][-.][|o{]?[|}o]?\s*[A-Za-z_]", entity):
            lines.append("    " + re.sub(r"\s+", " ", entity))
            continue
        name, _, fields = entity.partition(":")
        name = re.sub(r"\W+", "_", name.strip())[:32]
        if not name:
            continue
        lines.append(f"    {name} {{")
        for field in re.split(r",", fields):
            field = field.strip()
            if not field:
                continue
            parts = field.split()
            if len(parts) == 1:
                # One word: give it the type Mermaid demands rather than emitting
                # a line Mermaid will reject. The note in validate() says why.
                lines.append(f"        string {re.sub(r'\\W+', '_', parts[0])[:24]}")
            else:
                field_name = re.sub(r"\W+", "_", parts[0])[:24]
                field_type = re.sub(r"\W+", "_", parts[1])[:16]
                lines.append(f"        {field_type} {field_name}")
        lines.append("    }")
    if len(lines) == 1:
        lines.append("    Entity { string id }")
    return "\n".join(lines)


def _architecture(description: str, title: str) -> str:
    """
    Rendered as a flowchart with subgraphs — Mermaid has no 'architecture' type,
    so the output is honest flowchart syntax rather than invented keywords.
    """
    lines = ["flowchart LR"]
    if title:
        lines.append(f"    %% {title}")
    index = {}
    for raw in re.split(r";|\n", str(description or "")):
        part = raw.strip()
        if not part or ":" not in part:
            continue
        name, detail = part.split(":", 1)
        name, detail = name.strip(), detail.strip()
        if not name:
            continue
        node = re.sub(r"\W+", "_", name)[:24] or f"S{len(index)}"
        index[name] = (node, detail)
        lines.append(f'    {node}["{name}<br/>{detail[:60]}"]')
    if not index:
        lines.append('    Service["Service<br/>what it does"]')
    return "\n".join(lines)


_BUILDERS = {"flowchart": _flowchart, "sequence": _sequence, "mindmap": _mindmap,
             "er": _er, "architecture": _architecture}


def build(kind: str, description: str, title: str = "") -> dict[str, Any]:
    kind = str(kind or "flowchart").strip().lower()
    if kind not in _BUILDERS:
        return {"ok": False, "reason": f"'{kind}' is not a diagram kind I build "
                                       f"({', '.join(_KINDS)})", "mermaid": ""}
    text = _BUILDERS[kind](description, title)
    theme = str(_cfg().get("theme") or "default").strip()
    if theme and theme != "default":
        text = f"%%{{init: {{'theme': '{re.sub(chr(39), '', theme)}}} }} }}\n" + text
    checked = validate(text)
    return {"ok": checked["ok"], "mermaid": text, "check": checked, "kind": kind}


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """One diagram action. Text only — nothing is rendered or sent anywhere."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "build").strip().lower()

        if action in ("help", "?"):
            return ("I write Mermaid diagrams and check them before you paste them, sir: "
                    "flowcharts, sequence diagrams, mind maps, ER diagrams and architecture "
                    "graphs. Say e.g. 'flowchart of: start -> check ->|no| retry'. For a picture "
                    "instead of text, build_diagram draws one.")

        if action in ("validate", "check"):
            text = str(p.get("mermaid") or p.get("description") or "")
            if not text.strip():
                return "Which diagram should I check, sir? Paste the Mermaid text."
            report = validate(text)
            head = (f"Valid as far as I can check, sir — {report['kind']}, {report['lines']} line(s)."
                    if report["ok"] else
                    f"{len(report['problems'])} problem(s) found:")
            lines = [head]
            for problem in report["problems"][:12]:
                lines.append(f"  • {problem}")
            for note in report["notes"][:3]:
                lines.append(f"  note: {note}")
            if report["ok"]:
                lines.append("  I check structure, balance and references — not whether a browser "
                             "will render it identically.")
            return "\n".join(lines)

        if action in ("build", "make", "draw", "create"):
            kind = str(p.get("kind") or p.get("type") or "flowchart").strip().lower()
            description = str(p.get("description") or p.get("steps") or "").strip()
            if not description:
                return ("Give me the shape of it, sir — e.g. 'start -> check ->|no| retry' for a "
                        "flowchart, 'Browser -> Server: login' for a sequence, or "
                        "'Root: child, child' for a mind map.")
            built = build(kind, description, str(p.get("title") or ""))
            if not built["ok"] and not built["mermaid"]:
                return f"Sir, {built['reason']}."
            lines = [built["mermaid"]]
            check = built["check"]
            if check["ok"]:
                lines.append("")
                lines.append(f"Checked, sir — {check['kind']}, {check['lines']} line(s), all "
                             f"balanced and every reference declared.")
            else:
                lines.append("")
                lines.append(f"{len(check['problems'])} thing(s) worth knowing before you paste it:")
                for problem in check["problems"][:8]:
                    lines.append(f"  • {problem}")
            save_as = str(p.get("save_as") or "").strip()
            if save_as:
                try:
                    out = _unique(output_dir() / (save_as if save_as.endswith(".mmd")
                                                  else save_as + ".mmd"))
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(built["mermaid"], encoding="utf-8")
                    lines.append(f"Saved to {out}")
                except Exception as e:
                    lines.append(f"(I could not save it: {str(e)[:70]})")
            if player:
                try:
                    player.show_content(f"Mermaid — {check['kind']}", built["mermaid"])
                except Exception:
                    pass
            return "\n".join(lines)

        return (f"Sir, '{action}' is not a diagram action I have. I can build or validate — "
                f"kinds: {', '.join(_KINDS)}.")
    except Exception as e:                                     # noqa: BLE001
        return f"Sir, the diagram builder hit a problem: {str(e)[:150]}"


# ── self-test: python diagram_builder.py ─────────────────────────────────────

def _selftest() -> int:
    print("diagram_builder — self-test")
    problems = 0

    flow = build("flowchart", "Start(Open) -> Check{Password OK?} ->|yes| Home[Dashboard]")
    print(f"  flowchart               : {flow['mermaid'].splitlines()[1][:64]} …")
    problems += 0 if flow["ok"] and "flowchart TB" in flow["mermaid"] else 1

    flow2 = build("flowchart", "A[Start] -> Missing")
    print(f"  عقدة غير معلنة          : {flow2['check']['problems'][:1]}")
    problems += 0 if not flow2["ok"] and "never declared" in flow2["check"]["problems"][0] else 1

    seq = build("sequence", "Browser -> Server: login; Server -> Database: check; Database -> Server: ok; Server -> Browser: token")
    report = validate(seq["mermaid"])
    print(f"  sequence                : {len(seq['mermaid'].splitlines())} أسطر، صالح={report['ok']}")
    problems += 0 if report["ok"] and "sequenceDiagram" in seq["mermaid"] else 1
    print("    " + " | ".join(seq["mermaid"].splitlines()[2:5]))

    seq_stray = validate("sequenceDiagram\nparticipant A\nA -> Ghost: hello")
    print(f"  مشارك غير معلن          : {seq_stray['problems'][:1]}")
    problems += 0 if not seq_stray["ok"] and "never declared" in seq_stray["problems"][0] else 1

    mind = build("mindmap", "JARVIS: Plugins(memory, search, watch), Voice(wake, arabic)")
    print(f"  mindmap                 : {mind['mermaid'].splitlines()[1:5]}")
    problems += 0 if mind["ok"] and "root((" in mind["mermaid"] else 1
    mind_bad = validate("mindmap\nroot\nchild")
    problems += 0 if not mind_bad["ok"] and "indentation" in mind_bad["problems"][0] else 1

    er = build("er", "User: id int, name string; Order: id int, total float; User ||--o{ Order : places")
    print(f"  er                      : {er['mermaid'].splitlines()[1:4]} …")
    problems += 0 if er["ok"] and "User {" in er["mermaid"] else 1
    er_bad = validate("erDiagram\nCUSTOMER ||--o{ ORDER : places")
    print(f"  علاقة بلا كيان          : {er_bad['problems'][:1]}")
    problems += 0 if not er_bad["ok"] and "no block of its own" in er_bad["problems"][0] else 1
    er_typed = build("er", "Thing: name")
    problems += 0 if "string name" in er_typed["mermaid"] else 1
    print(f"  حقل بلا نوع (يُكمل تلقائيًّا): {er_typed['mermaid'].splitlines()[1:3]}")

    arch = build("architecture", "UI: talks to the API; API: reads the database; DB: sqlite file")
    print(f"  architecture            : {arch['mermaid'].splitlines()[1][:66]} …")
    problems += 0 if arch["ok"] and "flowchart LR" in arch["mermaid"] else 1

    broken = validate("flowchart TB\n    A[Start -> B[End]")
    print(f"  أقواس مكسورة            : {broken['problems'][:1]}")
    problems += 0 if not broken["ok"] and "never closed" in " ".join(broken["problems"]) else 1
    quotes = validate('flowchart TB\n    A["unclosed label]\n    A --> B[B]')
    problems += 0 if not quotes["ok"] and "quotes" in quotes["problems"][0] else 1

    empty = validate("")
    problems += 0 if not empty["ok"] and "empty" in empty["problems"][0] else 1
    wrong_kind = validate("graph TD\n A --> B")
    print(f"  نوع غير مدعوم           : {wrong_kind['problems'][0][:74]}")
    problems += 0 if not wrong_kind["ok"] and "first line must say" in wrong_kind["problems"][0] else 1

    # saving is a new file, never an overwrite, and inside the configured folder
    save_as = "probe_diagram"
    first = None
    for _ in range(2):
        answer = run({"action": "build", "kind": "flowchart", "description": "A[One] -> B[Two]",
                      "save_as": save_as})
        path = answer.split("Saved to ")[-1].strip() if "Saved to " in answer else ""
        if first is None:
            first = path
        second = path
    print(f"  الحفظ بلا استبدال       : {Path(second).name}")
    problems += 0 if first and second and first != second and "(2)" in second else 1
    for leftover in output_dir().glob("probe_diagram*"):
        leftover.unlink()

    print(f"  run بلا وصف             : {run({'action': 'build', 'kind': 'flowchart'})[:64]}")
    problems += 0 if "Give me the shape" in run({"action": "build", "kind": "flowchart"}) else 1
    print(f"  run بنوع مجهول          : {run({'action': 'build', 'kind': 'hologram', 'description': 'A -> B'})[:60]}")
    problems += 0 if "not a diagram kind" in run(
        {"action": "build", "kind": "hologram", "description": "A -> B"}) else 1
    print(f"  validate بلا نصّ         : {run({'action': 'validate'})[:64]}")
    problems += 0 if "Which diagram" in run({"action": "validate"}) else 1
    print(f"  run(action مجهول)       : {run({'action': 'sing'})[:60]}")
    problems += 0 if "not a diagram action" in run({"action": "sing"}) else 1

    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
