"""
document_generator.py — turning what you say into a real document file.

WHAT IT MAKES
    .txt and .md   always, no library needed
    .docx          when python-docx is installed (it is in the project's
                   requirements for the file reader, so it usually already is)
    .pdf           only when a PDF library is present; if it is not, it says so
                   and tells you the exact command, rather than writing a
                   broken file

WHAT GOES IN
    Headings, paragraphs, bullet lists, numbered lists, tables and code blocks,
    written the way you would write them anyway:

        # Title
        ## A section
        - a point
        - another
        1. first step
        | Name | Value |
        | ---- | ----- |
        | cost | 240   |

    Plain sentences work too — a blank line starts a new paragraph.

WHERE IT SAVES, AND WHAT IT WILL NOT DO
    Everything lands in ONE folder: Documents/JARVIS by default, or whatever you
    set. Paths are resolved and checked; the folder is created if it is missing.

    It will not overwrite a document. If the name is taken it writes
    "report (2).docx" and tells you the exact path it used. Overwriting is
    possible only when you ask for it by name — and then it goes through the
    confirmation gate, because a lost file is not something you can undo by
    asking again.

ARABIC AND PDF — A REAL LIMITATION, SAID PLAINLY
    DOCX, Markdown and TXT carry Arabic perfectly; Word and any text editor shape
    it correctly. PDF is different: a PDF must embed the letters themselves, and
    the built-in PDF fonts have no Arabic glyphs. So a PDF containing Arabic is
    refused with an explanation instead of producing a page of empty boxes.

SETTINGS (Plugin Manager → Document generator)
    output_dir   Documents/JARVIS — one folder, nothing outside it
    format       auto | txt | md | docx | pdf
    font_path    optional .ttf for PDF text (never needed for DOCX)
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "document_generator",
    "description": (
        "Writes a real document file from what is dictated: 'write this into a Word "
        "document', 'make me a PDF report about this', 'save these notes as a markdown "
        "file'. It reports the exact path it saved to. Never overwrites a file."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "create | list | status"},
            "title": {"type": "STRING", "description": "Document title"},
            "content": {"type": "STRING", "description": "The text, with # headings and - bullets"},
            "format": {"type": "STRING", "description": "auto | txt | md | docx | pdf"},
            "filename": {"type": "STRING", "description": "Optional file name without extension"},
            "overwrite": {"type": "STRING", "description": "yes to replace an existing file"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "document_generator",
    "title": "Document generator / إنشاء المستندات",
    "fields": [
        {"key": "output_dir", "type": "text", "label": "Output folder (blank = Documents/JARVIS)",
         "default": ""},
        {"key": "format", "type": "choice", "label": "Default format",
         "default": "auto", "choices": ["auto", "txt", "md", "docx", "pdf"]},
        {"key": "font_path", "type": "text", "label": "A .ttf for PDF text (optional)",
         "default": ""},
    ],
}

MAX_CHARS = 200_000


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("document_generator") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def output_dir() -> Path:
    """One safe folder. Created on demand; never resolves outside itself."""
    raw = str(_cfg().get("output_dir") or "").strip()
    base = Path(raw).expanduser() if raw else (Path.home() / "Documents" / "JARVIS")
    try:
        base = base.resolve()
    except Exception:
        base = Path.home() / "Documents" / "JARVIS"
    return base


def _safe_name(name: str, fallback: str = "document") -> str:
    """A file name that cannot escape the folder or upset Windows."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "")).strip().strip(".")
    name = re.sub(r"\s+", "_", name)
    if not name:
        name = fallback
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if name.upper() in reserved:
        name = f"_{name}"
    return name[:80]


def _unique_path(folder: Path, stem: str, ext: str) -> Path:
    """Never overwrite: 'report (2).docx' instead of clobbering 'report.docx'."""
    candidate = folder / f"{stem}{ext}"
    n = 2
    while candidate.exists() and n < 500:
        candidate = folder / f"{stem} ({n}){ext}"
        n += 1
    return candidate


def _has_arabic(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", text or ""))


# ── parsing the content ───────────────────────────────────────────────────────

def parse_blocks(content: str) -> list[tuple[str, Any]]:
    """
    Turn the text into (kind, value) blocks. Kinds: h1..h3, bullet, number,
    table, code, para. Deliberately simple and predictable — no clever guessing.
    """
    blocks: list[tuple[str, Any]] = []
    lines = (content or "").replace("\r\n", "\n").split("\n")
    i, para = 0, []

    def flush() -> None:
        if para:
            blocks.append(("para", " ".join(para).strip()))
            para.clear()

    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            i += 1
            continue
        if stripped.startswith("```"):
            flush()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", "\n".join(code)))
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m:
            flush()
            blocks.append((f"h{len(m.group(1))}", m.group(2).strip()))
            i += 1
            continue
        if re.match(r"^[-*+]\s+", stripped):
            flush()
            blocks.append(("bullet", re.sub(r"^[-*+]\s+", "", stripped)))
            i += 1
            continue
        if re.match(r"^\d+[.)]\s+", stripped):
            flush()
            blocks.append(("number", re.sub(r"^\d+[.)]\s+", "", stripped)))
            i += 1
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue
        para.append(stripped)
        i += 1
    flush()
    return blocks


def strip_markup(text: str) -> str:
    """Lose the markdown, keep the words — for formats that have no styling."""
    out = re.sub(r"```(.*?)```", lambda m: m.group(1), text or "", flags=re.S)
    out = re.sub(r"^#{1,6}\s*", "", out, flags=re.M)
    out = re.sub(r"^\s*[-*+]\s+", "- ", out, flags=re.M)
    return out


# ── writers ───────────────────────────────────────────────────────────────────

def _write_txt(path: Path, title: str, content: str) -> tuple[bool, str]:
    body = (title + "\n" + "=" * len(title) + "\n\n" + strip_markup(content)) if title else strip_markup(content)
    path.write_text(body, encoding="utf-8")
    return True, ""


def _write_md(path: Path, title: str, content: str) -> tuple[bool, str]:
    body = (f"# {title}\n\n{content}" if title else content)
    path.write_text(body, encoding="utf-8")
    return True, ""


def _write_docx(path: Path, title: str, content: str) -> tuple[bool, str]:
    try:
        from docx import Document
    except Exception:
        return False, ("python-docx is not installed — that is what writes Word files "
                       "(pip install python-docx)")
    try:
        doc = Document()
        if title:
            doc.add_heading(title, level=0)
        for kind, value in parse_blocks(content):
            if kind == "h1":
                doc.add_heading(value, level=1)
            elif kind == "h2":
                doc.add_heading(value, level=2)
            elif kind == "h3":
                doc.add_heading(value, level=3)
            elif kind == "bullet":
                doc.add_paragraph(value, style="List Bullet")
            elif kind == "number":
                doc.add_paragraph(value, style="List Number")
            elif kind == "code":
                doc.add_paragraph(value, style="No Spacing")
            elif kind == "table":
                rows = value
                table = doc.add_table(rows=0, cols=max(len(r) for r in rows))
                table.style = "Table Grid"
                for r in rows:
                    cells = table.add_row().cells
                    for j, cell in enumerate(r[:len(cells)]):
                        cells[j].text = str(cell)
            else:
                doc.add_paragraph(value)
        doc.save(str(path))
        return True, ""
    except Exception as e:
        return False, f"the Word writer failed: {str(e)[:90]}"


def _pdf_library():
    """reportlab, then fpdf2, then the old fpdf — whichever exists."""
    for name in ("reportlab", "fpdf"):
        try:
            __import__(name)
            return name
        except Exception:
            continue
    return ""


def _write_pdf(path: Path, title: str, content: str) -> tuple[bool, str]:
    lib = _pdf_library()
    if not lib:
        return False, ("no PDF library is installed — run: pip install reportlab "
                       "(or: pip install fpdf2)")
    if _has_arabic(title) or _has_arabic(content):
        font = str(_cfg().get("font_path") or "").strip()
        if not font or not Path(font).is_file():
            return False, ("PDF cannot carry Arabic with the built-in fonts — a PDF must embed "
                           "the letters and the standard fonts have none. Save it as .docx and "
                           "it will be perfect, or point 'font_path' at an Arabic .ttf")
    if lib == "reportlab":
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.lib.units import cm
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
            from reportlab.lib import colors

            styles = getSampleStyleSheet()
            font = str(_cfg().get("font_path") or "").strip()
            if font and Path(font).is_file():
                from reportlab.pdfbase import pdfmetrics
                from reportlab.pdfbase.ttfonts import TTFont
                pdfmetrics.registerFont(TTFont("custom", font))
                styles["Normal"].fontName = "custom"
                styles["Title"].fontName = "custom"
            doc = SimpleDocTemplate(str(path), pagesize=A4, title=title or "Document",
                                    leftMargin=2 * cm, rightMargin=2 * cm)
            story = []
            if title:
                story.append(Paragraph(title, styles["Title"]))
                story.append(Spacer(1, 0.5 * cm))
            for kind, value in parse_blocks(content):
                if kind == "h1":
                    story.append(Paragraph(value, styles["Heading1"]))
                elif kind == "h2":
                    story.append(Paragraph(value, styles["Heading2"]))
                elif kind == "h3":
                    story.append(Paragraph(value, styles["Heading3"]))
                elif kind == "bullet":
                    story.append(Paragraph(f"\u2022 {value}", styles["Normal"]))
                elif kind == "number":
                    story.append(Paragraph(f"\u2013 {value}", styles["Normal"]))
                elif kind == "code":
                    story.append(Paragraph(value.replace(" ", "&nbsp;").replace("\n", "<br/>"),
                                           styles["Code"]))
                elif kind == "table":
                    data = [[Paragraph(str(c), styles["Normal"]) for c in row] for row in value]
                    t = Table(data, hAlign="LEFT")
                    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                                           ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke)]))
                    story.append(t)
                    story.append(Spacer(1, 0.3 * cm))
                else:
                    story.append(Paragraph(value, styles["Normal"]))
                story.append(Spacer(1, 0.15 * cm))
            doc.build(story)
            return True, ""
        except Exception as e:
            return False, f"the PDF writer failed: {str(e)[:90]}"
    try:                                   # fpdf / fpdf2
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        font = str(_cfg().get("font_path") or "").strip()
        if font and Path(font).is_file():
            pdf.add_font("custom", "", font)
            pdf.set_font("custom", size=12)
        else:
            pdf.set_font("Helvetica", size=12)
        if title:
            pdf.set_font_size(18)
            pdf.multi_cell(0, 10, title)
            pdf.ln(2)
            pdf.set_font_size(12)
        for kind, value in parse_blocks(content):
            if kind.startswith("h"):
                pdf.set_font_size(15 if kind == "h1" else 13)
                pdf.multi_cell(0, 8, value)
                pdf.set_font_size(12)
            elif kind == "table":
                for row in value:
                    pdf.multi_cell(0, 7, "  |  ".join(str(c) for c in row))
            elif kind == "bullet":
                pdf.multi_cell(0, 7, f"- {value}")
            else:
                pdf.multi_cell(0, 7, str(value))
            pdf.ln(1)
        pdf.output(str(path))
        return True, ""
    except Exception as e:
        return False, f"the PDF writer failed: {str(e)[:90]}"


_WRITERS = {"txt": _write_txt, "md": _write_md, "docx": _write_docx, "pdf": _write_pdf}


def _pick_format(title: str, content: str, asked: str) -> str:
    asked = (asked or "").strip().lower().lstrip(".")
    if asked in _WRITERS:
        return asked
    default = str(_cfg().get("format") or "auto").strip().lower()
    if default in _WRITERS:
        return default
    # A table or a code block needs a format that can show it.
    kinds = {k for k, _ in parse_blocks(content)}
    if "table" in kinds or "code" in kinds or _has_arabic(content):
        return "docx" if _docx_ok() else "md"
    return "md"


def _docx_ok() -> bool:
    """Is python-docx importable? Asked with importlib rather than by importing
    it and throwing the name away — pyflakes was right that the old form left an
    unused import behind."""
    try:
        import importlib.util
        return importlib.util.find_spec("docx") is not None
    except Exception:
        return False


def generate(title: str, content: str, fmt: str = "auto", filename: str = "",
             overwrite: bool = False) -> dict[str, Any]:
    """
    The reusable API: writes the file and returns {ok, path, format, bytes, reason}.
    Never overwrites unless `overwrite` is explicitly true. Never raises.
    """
    content = str(content or "")
    if not content.strip() and not str(title or "").strip():
        return {"ok": False, "reason": "there is nothing to write", "path": ""}
    if len(content) > MAX_CHARS:
        content = content[:MAX_CHARS] + "\n\n… (truncated)"
    chosen = _pick_format(str(title or ""), content, fmt or "auto")

    folder = output_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"ok": False, "path": "", "reason": f"I could not create {folder}: {str(e)[:70]}"}
    if not os.access(folder, os.W_OK):
        return {"ok": False, "path": "", "reason": f"I cannot write inside {folder}"}

    stem = _safe_name(filename or title or f"document_{time.strftime('%Y%m%d_%H%M')}")
    path = folder / f"{stem}.{chosen}"
    if path.exists() and not overwrite:
        path = _unique_path(folder, stem, f".{chosen}")
    try:
        ok, why = _WRITERS[chosen](path, str(title or ""), content)
    except Exception as e:
        return {"ok": False, "path": str(path), "reason": f"the writer failed: {str(e)[:90]}"}
    if not ok:
        return {"ok": False, "path": "", "format": chosen, "reason": why}
    try:
        size = path.stat().st_size
    except Exception:
        size = 0
    return {"ok": True, "path": str(path), "format": chosen, "bytes": size,
            "renamed": path.stem != stem, "reason": ""}


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one document action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "create").strip().lower()

        if action in ("create", "make", "write", "save", "generate"):
            title = str(p.get("title") or "").strip()
            content = str(p.get("content") or "")
            if not content.strip():
                return "What should I write in it, sir?"
            fmt = str(p.get("format") or "auto")
            wants_overwrite = str(p.get("overwrite") or "").strip().lower() in ("yes", "true", "on", "1")
            filename = str(p.get("filename") or "")
            if wants_overwrite:
                target = output_dir() / f"{_safe_name(filename or title or 'document')}"
                if any(target.with_suffix(f".{e}").exists() for e in _WRITERS):
                    # Replacing a file cannot be taken back, so the REPLACEMENT
                    # ITSELF lives inside the gate: core/confirm parks this
                    # callable and runs it only when the user presses CONFIRM on
                    # the HUD, later — not now, and not if there is no interface.
                    #
                    # An earlier version of this file asked the gate a question
                    # and then replaced the file by itself. Reading core/confirm.py
                    # shows why that fails: request() HANDS BACK A SENTENCE, it
                    # never returns the answer. So that version replaced the file
                    # before the banner was answered — and replaced it with no
                    # interface at all. Both are the silent overwrite this plugin
                    # promises never to do. The test at the bottom of this file
                    # now proves the difference: it binds a real gate, and only
                    # the CONFIRM button changes the file.
                    def _replace() -> str:
                        outcome = generate(title, content, fmt, filename, overwrite=True)
                        if outcome.get("ok"):
                            return (f"Replaced {outcome['path']} "
                                    f"({outcome.get('bytes', 0)} bytes).")
                        return f"the replacement failed: {outcome.get('reason', 'unknown')}"

                    try:
                        from core import confirm
                        return confirm.request(
                            key="doc_overwrite", title="Replace an existing document?",
                            detail=(f"{target.name} already exists in {output_dir()} — replacing "
                                    f"it loses the old file."),
                            run=_replace)
                    except Exception:
                        return ("Sir, I need your yes to replace a file and I could not ask for "
                                "it, so nothing was replaced. Say it again with a different name "
                                "and I will write a new one beside it.")
            result = generate(title, content, fmt, filename, overwrite=False)
            if not result["ok"]:
                return f"Sir, {result['reason']}."
            if player:
                try:
                    player.write_log(f"[Doc] {Path(result['path']).name}")
                except Exception:
                    pass
            note = (" (the name was taken, so I did not overwrite — this is a new file)"
                    if result.get("renamed") else "")
            return (f"Done, sir — {result['format'].upper()}, {result['bytes']:,} bytes, saved to:"
                    f"\n{result['path']}{note}")

        if action in ("list", "files"):
            folder = output_dir()
            if not folder.is_dir():
                return f"I have not written anything yet — the folder {folder} does not exist."
            docs = sorted([f for f in folder.iterdir() if f.suffix.lower() in (".txt", ".md", ".docx", ".pdf")],
                          key=lambda f: -f.stat().st_mtime)[:12]
            if not docs:
                return f"{folder} is empty, sir."
            lines = [f"Recent documents in {folder}:"]
            for d in docs:
                lines.append(f"  {d.name}  ({d.stat().st_size:,} bytes)")
            return "\n".join(lines)

        if action in ("status", "info"):
            folder = output_dir()
            lib = _pdf_library()
            return (f"Output folder : {folder}\n"
                    f"Word (.docx)  : {'ready' if _docx_ok() else 'python-docx is not installed'}\n"
                    f"PDF           : {'ready via ' + lib if lib else 'no PDF library — pip install reportlab'}\n"
                    f"Arabic in PDF : not supported by the built-in fonts (use DOCX — it is perfect)\n"
                    f"Overwriting   : never, unless you ask for it and confirm")

        return (f"Sir, '{action}' is not a document action I have. I can create a document, "
                f"list what I have written, or report settings.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the document generator hit a problem: {str(e)[:150]}"


# ── self-test: python document_generator.py ──────────────────────────────────

def _selftest() -> int:
    print("document_generator — self-test")
    problems = 0

    sample = ("# Quarterly Report\n\n## Costs\n\nThe plant cost 240 million pounds.\n\n"
              "- first point\n- second point\n\n1. step one\n2. step two\n\n"
              "| Item | Value |\n| ---- | ----- |\n| Water | 240 |\n| Staff | 18 |\n\n"
              "Final paragraph of the report.")

    blocks = parse_blocks(sample)
    kinds = [k for k, _ in blocks]
    print(f"  parsed blocks       : {kinds}")
    problems += 0 if {"h1", "h2", "bullet", "number", "table", "para"} <= set(kinds) else 1

    table = next((v for k, v in blocks if k == "table"), [])
    print(f"  table rows          : {table}")
    problems += 0 if len(table) == 3 else 1

    print(f"  safe file name      : {_safe_name('../../etc/pas swd')!r}")
    problems += 0 if "/" not in _safe_name("../../etc/passwd") else 1
    print(f"  windows reserved    : {_safe_name('CON')!r}")
    problems += 0 if _safe_name("CON") != "CON" else 1

    # write into a probe folder, then clean up — never into the user's Documents
    probe = Path(__file__).resolve().parent / "_docgen_probe"
    global _cfg
    real_cfg = _cfg
    _cfg = lambda: {**real_cfg(), "output_dir": str(probe)}
    try:
        for fmt in ("txt", "md", "docx", "pdf"):
            r = generate("Test Report", sample, fmt=fmt, filename=f"probe_{fmt}")
            if r["ok"]:
                print(f"  {fmt:<19} : ✓ {Path(r['path']).name} ({r['bytes']:,} bytes)")
            else:
                print(f"  {fmt:<19} : ✗ {r['reason'][:74]}")
                # txt/md must always work; docx/pdf only when their library exists
                if fmt in ("txt", "md"):
                    problems += 1
        r1 = generate("Test Report", sample, fmt="txt", filename="probe_txt")
        print(f"  never overwrites    : {Path(r1['path']).name} (renamed={r1.get('renamed')})")
        problems += 0 if r1.get("renamed") else 1
        r2 = generate("", "no title here", fmt="txt", filename="no_title")
        print(f"  no title            : ok={r2['ok']} {Path(r2['path']).name if r2['ok'] else r2['reason'][:50]}")
        problems += 0 if r2["ok"] else 1
        empty = generate("", "", fmt="txt")
        print(f"  empty content       : {empty['reason']}")
        problems += 0 if not empty["ok"] else 1
        pdf_ar = generate("تقرير", "نص عربي طويل بما يكفي", fmt="pdf", filename="probe_ar")
        if pdf_ar["ok"]:
            print("  Arabic PDF          : written (a font was configured)")
        else:
            print(f"  Arabic PDF          : refused honestly — {pdf_ar['reason'][:58]}")
    finally:
        _cfg = real_cfg
        for f in probe.glob("*"):
            try:
                f.unlink()
            except Exception:
                pass
        try:
            probe.rmdir()
        except Exception:
            pass

    print(f"  status              : {run({'action': 'status'}).splitlines()[0]}")
    print(f"  bad action          : {run({'action': 'dance'})[:70]}")
    problems += 0 if "not a document action" in run({"action": "dance"}) else 1
    print(f"  no content          : {run({'action': 'create', 'title': 'x'})}")
    problems += 0 if "What should I write" in run({"action": "create", "title": "x"}) else 1

    # ── the overwrite gate, proven against the REAL core/confirm ──────────────
    # Four states matter, and three of them must leave the file alone: no
    # interface bound, an interface with the banner still waiting, and a user who
    # pressed CANCEL. Only CONFIRM may change the file — and it must change it
    # exactly then, not before. This runs the project's own gate, not a stub, so
    # it also proves the plugin still works the day confirm.py changes.
    gate_dir = Path(__file__).resolve().parent / "_doc_gate"
    gate_dir.mkdir(exist_ok=True)
    existing = gate_dir / "existing.txt"
    existing.write_text("ORIGINAL CONTENT", encoding="utf-8")
    saved_cfg = _cfg
    globals()["_cfg"] = lambda: {**saved_cfg(), "output_dir": str(gate_dir)}
    try:
        try:
            import core.confirm as gate
            saved_gate = (gate._show_cb, gate._hide_cb, gate._log_cb)
            try:
                # 1) no interface at all — nothing may be replaced
                gate._show_cb = None
                answer = run({"action": "create", "title": "x", "content": "NEW",
                              "format": "txt", "filename": "existing", "overwrite": "yes"})
                kept = existing.read_text(encoding="utf-8") == "ORIGINAL CONTENT"
                print(f"  replace, no interface: kept={kept} — {answer.splitlines()[0][:58]}")
                # Two honest refusals are possible here and both are accepted:
                # confirm.py's own sentence ("…so I have not done it") or this
                # file's message when the import itself fails. What matters is
                # that the file did not change.
                refused = any(w in answer for w in ("nothing was replaced", "have not done it",
                                                    "not been done"))
                problems += 0 if kept and refused else 1

                # 2) interface bound, banner waiting — still nothing
                gate._show_cb = lambda title, detail: None
                answer = run({"action": "create", "title": "x", "content": "NEW",
                              "format": "txt", "filename": "existing", "overwrite": "yes"})
                waiting = "[CONFIRMATION_PENDING]" in answer
                kept = existing.read_text(encoding="utf-8") == "ORIGINAL CONTENT"
                print(f"  replace, banner shown: waiting={waiting} kept={kept}")
                problems += 0 if waiting and kept else 1

                # 3) the user presses CANCEL — still nothing
                gate.resolve(False)
                time.sleep(0.25)
                kept = existing.read_text(encoding="utf-8") == "ORIGINAL CONTENT"
                print(f"  replace, CANCEL      : kept={kept}")
                problems += 0 if kept else 1

                # 4) the user presses CONFIRM — now, and only now, it changes
                run({"action": "create", "title": "x", "content": "NEW", "format": "txt",
                     "filename": "existing", "overwrite": "yes"})
                gate.resolve(True)
                replaced = False
                for _ in range(60):
                    if existing.read_text(encoding="utf-8") != "ORIGINAL CONTENT":
                        replaced = True
                        break
                    time.sleep(0.05)
                print(f"  replace, CONFIRM     : replaced={replaced} "
                      f"(the only path that writes over a file)")
                problems += 0 if replaced else 1
            finally:
                gate._show_cb, gate._hide_cb, gate._log_cb = saved_gate
                gate._pending = None
        except ImportError:
            print("  replace gate         : core/confirm not importable here — skipped")
    finally:
        globals()["_cfg"] = saved_cfg
        import shutil as _sh
        _sh.rmtree(gate_dir, ignore_errors=True)

    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
