"""
code_assistant.py — reading, searching and patching project code.

WHERE IT SITS NEXT TO dev_agent
    The project already has `actions/dev_agent.py`, which plans and writes whole
    projects from a description. That is one job. This plugin is the other one:
    looking at code that already exists — searching a project for what calls
    what, explaining a traceback, and producing a PATCH. It does not plan new
    applications, and it does not generate a file over your file.

THE PATCH RULE, WHICH IS THE WHOLE POINT
    Writing code into somebody's project is the single most destructive thing an
    assistant can do, and the failure mode is never dramatic — it is an unrelated
    file quietly overwritten. So:

      * `patch` never touches the file it came from. It writes a **new** file
        next to it as `<name>.proposed.py` (or a `.diff` if you prefer), and tells
        you the path. Applying it is your decision, with your editor and your
        undo history.
      * `write` — when you explicitly want a new file created — refuses to
        overwrite anything. An existing name becomes ` (2)`.
      * `edit` — changing a file in place — is the one action that modifies
        something, so it goes through the project's confirmation gate, requires
        the file to be inside a project root you configured, and records the
        original beside it (`<name>.bak-<timestamp>`) before writing. There is no
        path here that edits silently.

    Every file path is resolved and checked against the configured roots before
    it is read or written. There is no shell, no `eval`, no `exec`, no import of
    what it reads — the danger of "let the assistant run the code to see if it
    works" is a risk this plugin simply does not take.

EXPLAINING ERRORS
    A traceback is parsed, not guessed at: file, line, exception, and the frames
    of YOUR code are separated from library frames. If a model key is configured,
    the explanation may use it — and the answer says whether it came from the
    model or from the local rules, never blurring the two.

SETTINGS (Plugin Manager → Code assistant)
    roots            blank = the assistant's own folder (comma-separated for more)
    output_dir       blank = Documents/JARVIS_Code
    max_file_kb      512 — the largest file it will read for a patch
    allow_edit       yes — set to no to forbid in-place edits entirely
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "code_assistant",
    "description": (
        "Works on existing code: 'search the project for send_message', 'explain this error', "
        "'what calls open_app', 'propose a fix for this function'. It searches, explains and "
        "produces a patch WITHOUT touching your files — a proposed fix is written as a new "
        "'<name>.proposed.py' and you decide. Use dev_agent for building new projects."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "search | read | outline | explain | patch | write | edit | help"},
            "query": {"type": "STRING", "description": "What to search for"},
            "path": {"type": "STRING", "description": "File or folder to work on"},
            "code": {"type": "STRING", "description": "Code or a traceback to explain"},
            "instruction": {"type": "STRING", "description": "What the patch should change"},
            "content": {"type": "STRING", "description": "Full content for write"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "code_assistant",
    "title": "Code assistant / مساعد الأكواد",
    "fields": [
        {"key": "roots", "type": "text", "label": "Project folders (blank = the assistant's own)",
         "default": ""},
        {"key": "output_dir", "type": "text", "label": "Where proposals go (blank = Documents/JARVIS_Code)",
         "default": ""},
        {"key": "max_file_kb", "type": "text", "label": "Largest file to read, KB", "default": "512"},
        {"key": "allow_edit", "type": "text", "label": "Allow in-place edits (yes/no)", "default": "yes"},
    ],
}

_CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".md", ".txt",
                  ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sql", ".sh", ".bat", ".ps1",
                  ".c", ".h", ".cpp", ".cs", ".java", ".go", ".rs", ".kt", ".php", ".rb"}

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "env", "dist", "build",
              ".mypy_cache", ".pytest_cache", ".cache"}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("code_assistant") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def roots() -> list[Path]:
    """Where this plugin is allowed to read and write. Blank = its own project."""
    raw = str(_cfg().get("roots") or "").strip()
    candidates = [part.strip() for part in re.split(r"[,\n;]", raw) if part.strip()] or \
                 [str(Path(__file__).resolve().parent.parent)]
    out: list[Path] = []
    for part in candidates:
        try:
            path = Path(part).expanduser().resolve()
        except Exception:
            continue
        if path.exists() and path not in out:
            out.append(path)
    return out


def output_dir() -> Path:
    raw = str(_cfg().get("output_dir") or "").strip()
    base = Path(raw).expanduser() if raw else (Path.home() / "Documents" / "JARVIS_Code")
    try:
        return base.resolve()
    except Exception:
        return Path.home() / "Documents" / "JARVIS_Code"


def max_bytes() -> int:
    try:
        return max(16, min(20_000, int(str(_cfg().get("max_file_kb") or 512)))) * 1024
    except Exception:
        return 512 * 1024


def is_inside_roots(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except Exception:
        return False
    for root in roots():
        try:
            resolved.relative_to(root)
            return True
        except Exception:
            continue
    return False


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 500):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem} ({os.getpid()}){path.suffix}")


# ── reading and searching ─────────────────────────────────────────────────────

def walk_code(root: Path, limit: int = 4000) -> list[Path]:
    found: list[Path] = []
    for folder, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() in _CODE_SUFFIXES:
                found.append(Path(folder) / name)
                if len(found) >= limit:
                    return found
    return found


def search(query: str, path: str = "", limit: int = 40) -> dict[str, Any]:
    """Plain text search over the project's code files. Returns lines with numbers."""
    needle = str(query or "").strip()
    result: dict[str, Any] = {"ok": False, "hits": [], "scanned": 0, "reason": ""}
    if not needle:
        result["reason"] = "there is nothing to search for"
        return result
    base = Path(path).expanduser() if path else (roots()[0] if roots() else None)
    if base is None:
        result["reason"] = "no project folder is configured"
        return result
    if not is_inside_roots(base):
        result["reason"] = f"'{base}' is outside the folders I am allowed to read"
        return result
    files = walk_code(base) if base.is_dir() else [base]
    pattern = re.compile(re.escape(needle), re.I)
    for file in files:
        try:
            if file.stat().st_size > max_bytes():
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        result["scanned"] += 1
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                result["hits"].append((str(file), number, line.strip()[:140]))
                if len(result["hits"]) >= limit:
                    result["ok"] = True
                    return result
    result["ok"] = True
    return result


def outline(path: Path) -> list[str]:
    """Functions, classes and top-level constants with their line numbers — a map
    of a file, made by parsing it rather than by pattern-matching its text."""
    try:
        import ast
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except Exception as e:
        return [f"(I could not parse {path.name}: {str(e)[:80]})"]
    rows = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args][:6]
            rows.append(f"  line {node.lineno:>5}  def {node.name}({', '.join(args)})")
        elif isinstance(node, ast.ClassDef):
            rows.append(f"  line {node.lineno:>5}  class {node.name}")
            for inner in node.body:
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    rows.append(f"  line {inner.lineno:>5}      def {inner.name}(...)")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    rows.append(f"  line {node.lineno:>5}  {target.id} = …")
    return rows or ["  (no top-level definitions found)"]


# ── explaining an error ───────────────────────────────────────────────────────

_TRACEBACK = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
_EXCEPTION = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Exit|Interrupt|"
                        r"Fault|Denied|Timeout|Refused|NotFound|KeyError|ValueError|TypeError|"
                        r"IndexError|AttributeError|ImportError|ModuleNotFoundError))"
                        r"(?::\s*(.*))?$")


def parse_traceback(text: str) -> dict[str, Any]:
    """Structure out of a traceback. Returns what it FOUND, and says so when the
    text is not a traceback at all rather than inventing an error."""
    raw = str(text or "")
    frames = [{"file": f, "line": int(n), "func": fn} for f, n, fn in _TRACEBACK.findall(raw)]
    exception, message = "", ""
    for line in reversed([l.strip() for l in raw.splitlines() if l.strip()]):
        match = _EXCEPTION.match(line)
        if match:
            exception, message = match.group(1), (match.group(2) or "").strip()
            break
    user_frames, lib_frames = [], []
    for frame in frames:
        lowered = frame["file"].lower().replace("\\", "/")
        if any(marker in lowered for marker in ("site-packages", "lib/python", "\\python3",
                                                "python3.", "/usr/lib/")):
            lib_frames.append(frame)
        else:
            user_frames.append(frame)
    return {"is_traceback": bool(frames or exception), "exception": exception,
            "message": message, "user_frames": user_frames, "library_frames": lib_frames,
            "total_frames": len(frames)}


_KNOWN_ERRORS = {
    "ModuleNotFoundError": ("the library is not installed, or the name is misspelled. Install it "
                            "with pip, or check the import line's spelling"),
    "ImportError": ("something inside the imported module failed, or a name does not exist in it. "
                    "The line above tells you which name"),
    "FileNotFoundError": ("the path does not exist from the folder the program runs in. Print "
                          "the path at runtime — the guess and the reality often differ"),
    "PermissionError": ("the file or folder is locked or not yours. Close whatever holds it, or "
                        "run from a folder you own"),
    "KeyError": ("a dict was asked for a key it does not have. Print the keys, and use .get() "
                 "where the key is genuinely optional"),
    "IndexError": ("the code walked past the end of a list. The line number points at the access; "
                   "print len() just before it"),
    "TypeError": ("two types met that do not mix — usually a string where a number was expected, "
                  "or None where an object was. Print type(x) before the line"),
    "AttributeError": ("the object is not the type the code assumes — often None, because a "
                       "function returned nothing. Check what produced it"),
    "ValueError": ("the value is the right type but the wrong content — a bad number string, an "
                   "unpack of the wrong length"),
    "TimeoutError": ("something took longer than allowed. Raise the timeout, or retry with a "
                     "smaller job"),
    "ConnectionError": ("the network side refused or dropped. Check the address and whether the "
                        "service is up"),
    "IndentationError": ("lines mixed tabs and spaces. Make the file use one of them only"),
    "SyntaxError": ("the file does not parse. The reported line is where Python gave up, which is "
                    "often just after the real mistake — look at the line before it too"),
    "RecursionError": ("a function called itself without a stopping condition. Find where it "
                       "should return instead"),
    "UnboundLocalError": ("a name is assigned somewhere in the function, so Python treats it as "
                          "local — add 'global' or restructure"),
}


def explain_error(text: str) -> str:
    """Local rules first, always. A model may add to it, and if it does, the
    answer says which part came from where."""
    info = parse_traceback(text)
    lines: list[str] = []
    if not info["is_traceback"]:
        plus = ""
        if len(str(text or "")) > 400:
            plus = (" (that looks like code rather than an error — say 'outline' or 'patch' if "
                    "that is what you wanted)")
        return f"That does not read as a traceback to me, sir{plus}."

    if info["exception"]:
        hint = _KNOWN_ERRORS.get(info["exception"].split(".")[-1], "")
        lines.append(f"{info['exception']}"
                     + (f": {info['message']}" if info["message"] else ""))
        if hint:
            lines.append(f"  what it usually means: {hint}")
    if info["user_frames"]:
        last = info["user_frames"][-1]
        lines.append(f"  your code, last frame: {last['file']} line {last['line']} in "
                     f"{last['func']}()")
        if len(info["user_frames"]) > 1:
            lines.append("  the path that led there: "
                         + " → ".join(f"{Path(f['file']).name}:{f['line']}"
                                      for f in info["user_frames"][-4:]))
    if info["library_frames"]:
        counts: dict[str, int] = {}
        for frame in info["library_frames"]:
            key = frame["file"].split("site-packages")[-1].split("/")[-1] or frame["file"]
            counts[key] = counts.get(key, 0) + 1
        lines.append("  inside libraries (usually not where the bug is): "
                     + ", ".join(f"{k} ×{v}" for k, v in list(counts.items())[:4]))
    lines.append(f"  {info['total_frames']} frame(s) in total, {len(info['user_frames'])} of them "
                 f"yours.")
    lines.append("  (read from the traceback itself — no guessing.)")

    model = _ask_model("Explain this error in three short sentences, then one concrete fix:\n\n"
                       + str(text)[:4000], tier="fast")
    if model:
        lines.append("")
        lines.append("Model's reading (not verified against your code):")
        lines.append(model)
    else:
        lines.append("")
        lines.append("No AI key is configured, so the above is rules and reading only — which is "
                     "still most of the answer.")
    return "\n".join(lines)


def _ask_model(prompt: str, tier: str = "smart") -> str:
    """The project's own Gemini ladder, reused. Empty string when unavailable —
    and the caller always says which it got."""
    try:
        from core import gemini
        response = gemini.call(prompt, tier=gemini.SMART if tier == "smart" else gemini.FAST,
                               timeout_ms=45000)
        if response is None:
            return ""
        text = getattr(response, "text", None)
        return str(text).strip() if text else ""
    except Exception:
        return ""


# ── patching (never in place) ─────────────────────────────────────────────────

def propose_patch(path: Path, instruction: str, as_diff: bool = False) -> dict[str, Any]:
    """
    Produce a proposed version of a file. The file itself is never opened for
    writing here; the result lands in the output folder with a name that says
    what it is. Returns {ok, path, note}.
    """
    result: dict[str, Any] = {"ok": False, "path": "", "note": ""}
    if not is_inside_roots(path):
        result["note"] = (f"'{path}' is outside the folders I am allowed to work in — add it to "
                          f"my roots in settings if that is really where the code lives")
        return result
    if not path.exists() or not path.is_file():
        result["note"] = f"there is no file at {path}"
        return result
    if not instruction.strip():
        result["note"] = "tell me what the change should do, and I will propose it"
        return result
    try:
        size = path.stat().st_size
    except Exception:
        size = 0
    if size > max_bytes():
        result["note"] = (f"that file is {size // 1024} KB — larger than the {max_bytes() // 1024} "
                          f"KB I read for a patch")
        return result

    original = path.read_text(encoding="utf-8", errors="replace")
    answer = _ask_model(
        "You are patching one existing file. Reply with the COMPLETE new content of the file and "
        "nothing else — no explanation, no markdown fences.\n\n"
        f"The change requested: {instruction}\n\nFile ({path.name}):\n{original[:12000]}",
        tier="smart")
    if not answer:
        # No model: a diff-shaped proposal is still useful — difflib can show a
        # mechanical change the user described without any generation at all.
        result.update(ok=False, note=("there is no AI key configured, so I cannot write a fixed "
                                      "version — but I can show you the file's shape: say "
                                      "'outline'"))
        return result

    cleaned = re.sub(r"^```[a-zA-Z]*\n|```$", "", answer.strip()).strip()
    output_dir().mkdir(parents=True, exist_ok=True)
    if as_diff:
        diff = "\n".join(difflib.unified_diff(
            original.splitlines(), cleaned.splitlines(),
            fromfile=path.name, tofile=path.name + " (proposed)", lineterm=""))
        out = _unique(output_dir() / f"{path.stem}.proposed.diff")
        out.write_text(diff or "(no textual difference)", encoding="utf-8")
    else:
        out = _unique(output_dir() / f"{path.stem}.proposed{path.suffix}")
        out.write_text(cleaned, encoding="utf-8")
    result.update(ok=True, path=str(out),
                  note=("proposed — compare it, then apply it yourself. Your file was not touched"))
    return result


def write_new(path: Path, content: str) -> tuple[bool, str]:
    """Create a NEW file. An existing name is never overwritten; it becomes (2)."""
    if not is_inside_roots(path):
        return False, f"'{path}' is outside the folders I am allowed to write in"
    if path.exists():
        path = _unique(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content or ""), encoding="utf-8")
        return True, str(path)
    except Exception as e:
        return False, f"I could not write it: {str(e)[:90]}"


def edit_in_place(path: Path, content: str, player=None) -> str:
    """
    The only action in this file that modifies something that already existed.
    Confirmation gate first, backup beside the original, then write.
    """
    if str(_cfg().get("allow_edit") or "yes").lower() in ("no", "false", "0"):
        return ("In-place editing is switched off in my settings, sir. I can still write a "
                "proposed file for you to apply.")
    if not is_inside_roots(path) or not path.exists():
        return f"I will not edit '{path}' — it is outside my folders or does not exist."
    size = path.stat().st_size
    detail = (f"{path}\n{size} bytes will be replaced. A copy is kept beside it as "
              f"'{path.name}.bak-<time>' first.")

    def _do() -> str:
        try:
            backup = path.with_name(f"{path.name}.bak-{int(time.time())}")
            shutil.copy2(path, backup)
            path.write_text(str(content or ""), encoding="utf-8")
            return (f"Edited {path.name}, sir. The previous version is safe at {backup.name} — "
                    f"rename it back if you dislike the change.")
        except Exception as e:
            return f"Sir, the edit failed: {str(e)[:110]}"

    try:
        from core.confirm import request
        return str(request("code_assistant", f"Edit {path.name} in place", detail, _do))
    except Exception:
        return ("Sir, I need your yes to edit a file in place and I cannot ask right now — so I "
                "have not touched it.")


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """One code action. Reading is free; writing is either new, or gated."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "search").strip().lower()

        if action in ("help", "?"):
            return ("I work on existing code, sir: search a project, read a file, outline its "
                    "functions, explain a traceback, or propose a patch — which arrives as a NEW "
                    "file so nothing of yours is touched. For building a whole new project, "
                    "dev_agent is the right tool.")

        if action in ("search", "find", "grep"):
            query = str(p.get("query") or p.get("text") or "").strip()
            found = search(query, str(p.get("path") or ""))
            if not found["ok"]:
                return f"Sir, {found['reason']}."
            if not found["hits"]:
                return (f"Nothing matches '{query}' in {found['scanned']} file(s), sir.")
            lines = [f"{len(found['hits'])} match(es) for '{query}' in {found['scanned']} file(s):"]
            for file, number, line in found["hits"][:25]:
                lines.append(f"  {Path(file).name}:{number}  {line[:96]}")
            return "\n".join(lines)

        if action in ("read", "open", "cat"):
            path = Path(str(p.get("path") or "")).expanduser()
            if not str(p.get("path") or "").strip():
                return "Which file, sir?"
            if not is_inside_roots(path) or not path.exists():
                return f"I will not read '{path}' — it is outside my folders or does not exist."
            if path.stat().st_size > max_bytes():
                return f"That file is larger than the {max_bytes() // 1024} KB I read, sir."
            text = path.read_text(encoding="utf-8", errors="replace")
            shown = text[:3000]
            return (f"{path} ({len(text.splitlines())} lines):\n\n{shown}"
                    + ("\n… (truncated)" if len(text) > 3000 else ""))

        if action in ("outline", "structure", "map"):
            path = Path(str(p.get("path") or "")).expanduser()
            if not str(p.get("path") or "").strip() or not path.exists():
                return "Which file should I outline, sir?"
            if not is_inside_roots(path):
                return f"'{path}' is outside my folders, sir."
            body = outline(path)
            return f"{path.name}:\n" + "\n".join(body)

        if action in ("explain", "error", "traceback", "why"):
            text = str(p.get("code") or p.get("text") or "").strip()
            if not text:
                return ("Paste the error, sir — the traceback lines. I read the frames and the "
                        "exception and tell you which line of yours matters.")
            return explain_error(text)

        if action in ("patch", "fix", "propose"):
            raw = str(p.get("path") or "").strip()
            if not raw:
                return "Which file should I patch, sir?"
            proposal = propose_patch(Path(raw).expanduser(), str(p.get("instruction") or ""),
                                     as_diff=str(p.get("as_diff") or "").lower() in ("yes", "true", "1"))
            if not proposal["ok"]:
                return f"Sir, {proposal['note']}."
            if player:
                try:
                    player.write_log(f"[Code] proposal → {Path(proposal['path']).name}")
                except Exception:
                    pass
            return (f"{proposal['note']}: {proposal['path']}\nOpen it, compare, and apply it "
                    f"yourself — I keep no hand on your files.")

        if action in ("write", "create", "new_file"):
            raw = str(p.get("path") or "").strip()
            if not raw:
                return "Where should I write it, sir?"
            ok, result = write_new(Path(raw).expanduser(), str(p.get("content") or ""))
            return f"Written: {result}" if ok else f"Sir, {result}."

        if action in ("edit", "replace", "apply"):
            raw = str(p.get("path") or "").strip()
            if not raw:
                return "Which file should I edit, sir?"
            return edit_in_place(Path(raw).expanduser(), str(p.get("content") or ""), player)

        return (f"Sir, '{action}' is not a code action I have. I can search, read, outline, "
                f"explain, patch, write or edit.")
    except Exception as e:                                     # noqa: BLE001
        return f"Sir, the code assistant hit a problem: {str(e)[:150]}"


# ── self-test: python code_assistant.py ─────────────────────────────────────

def _selftest() -> int:
    print("code_assistant — self-test")
    problems = 0
    probe = Path(__file__).resolve().parent / "_code_probe"
    probe.mkdir(exist_ok=True)
    (probe / "target.py").write_text(
        "import os\n\n\ndef greet(name):\n    return f'hello {name}'\n\n\n"
        "class Greeter:\n    def hi(self):\n        return greet('x')\n\n\nMAX = 10\n",
        encoding="utf-8")
    (probe / "other.py").write_text("from target import greet\nprint(greet('a'))\n",
                                    encoding="utf-8")

    found = search("greet", str(probe))
    print(f"  البحث في المشروع        : {len(found['hits'])} نتيجة في {found['scanned']} ملف")
    problems += 0 if found["ok"] and len(found["hits"]) >= 3 else 1
    print(f"    مثال                 : {Path(found['hits'][0][0]).name}:{found['hits'][0][1]} "
          f"{found['hits'][0][2][:48]}")

    rows = outline(probe / "target.py")
    print(f"  خريطة الملف             : {len(rows)} سطرًا")
    joined = " ".join(rows)
    problems += 0 if ("def greet" in joined and "class Greeter" in joined and "MAX" in joined) else 1
    print("    " + " | ".join(r.strip()[:34] for r in rows[:4]))

    # explain: rules first, and the model part labelled if any
    traceback_text = (
        'Traceback (most recent call last):\n'
        '  File "/home/user/proj/app.py", line 12, in <module>\n'
        '    main()\n'
        '  File "/home/user/proj/app.py", line 8, in main\n'
        '    price = row["price"]\n'
        '  File "/usr/lib/python3.13/site-packages/pandas/core/frame.py", line 100, in getitem\n'
        '    raise KeyError\n'
        'KeyError: \'price\'')
    parsed = parse_traceback(traceback_text)
    print(f"  تحليل الأثر             : {parsed['exception']} · إطاراتك {len(parsed['user_frames'])} · "
          f"المكتبات {len(parsed['library_frames'])}")
    problems += 0 if (parsed["exception"] == "KeyError" and len(parsed["user_frames"]) == 2
                      and len(parsed["library_frames"]) == 1) else 1
    explanation = run({"action": "explain", "code": traceback_text})
    print(f"  الشرح                   : {explanation.splitlines()[0][:70]}")
    problems += 0 if "KeyError" in explanation and "not verified against your code" in explanation \
        or "no AI key" in explanation.lower() or "No AI key" in explanation else 1
    not_tb = run({"action": "explain", "code": "just some words"})
    print(f"  نصّ ليس خطأً             : {not_tb[:64]}")
    problems += 0 if "does not read as a traceback" in not_tb else 1
    problems += 0 if parse_traceback("ModuleNotFoundError: No module named 'x'")["exception"] \
        == "ModuleNotFoundError" else 1

    # patch: the promise is that the original is untouched and a proposal appears
    original = (probe / "target.py").read_bytes()
    proposal = propose_patch(probe / "target.py", "make greet uppercase")
    if proposal["ok"]:
        print(f"  رقعة مقترحة              : {Path(proposal['path']).name} "
              f"(الأصل لم يُمَس: {(probe / 'target.py').read_bytes() == original})")
        problems += 0 if (probe / "target.py").read_bytes() == original else 1
        problems += 0 if Path(proposal["path"]).exists() else 1
        Path(proposal["path"]).unlink()
    else:
        print(f"  رقعة بلا مفتاح ذكاء      : {proposal['note'][:74]}")
        problems += 0 if ("no AI key" in proposal["note"]) else 1
    print(f"  الأصل لم يُمَس أبدًا     : {(probe / 'target.py').read_bytes() == original}")
    problems += 0 if (probe / "target.py").read_bytes() == original else 1

    outside = run({"action": "patch", "path": "/etc/hosts", "instruction": "x"})
    print(f"  ملف خارج الجذور          : {outside[:70]}")
    problems += 0 if "outside the folders" in outside else 1

    # write: a new file is new; an existing one is never overwritten
    ok1, first = write_new(probe / "generated.py", "print(1)\n")
    ok2, second = write_new(probe / "generated.py", "print(2)\n")
    print(f"  كتابة جديدة              : {Path(first).name} ثم {Path(second).name} — "
          f"الأول محتواه: {Path(first).read_text().strip()}")
    problems += 0 if ok1 and ok2 and first != second and Path(first).read_text().strip() == "print(1)" else 1

    # edit: gated. No UI here → nothing changes.
    before = (probe / "target.py").read_text(encoding="utf-8")
    answer = run({"action": "edit", "path": str(probe / "target.py"), "content": "# replaced\n"})
    after = (probe / "target.py").read_text(encoding="utf-8")
    print(f"  تعديل بلا بوابة          : تغيّر={before != after} — {answer.splitlines()[0][:56]}")
    problems += 0 if before == after else 1
    if before == after:
        problems += 0 if ("cannot ask" in answer or "need your yes" in answer
                          or "cannot confirm" in answer) else 1

    # allow_edit=no must refuse before any gate
    real_cfg = _cfg
    globals()["_cfg"] = lambda: {**real_cfg(), "allow_edit": "no"}
    refused = run({"action": "edit", "path": str(probe / "target.py"), "content": "x"})
    globals()["_cfg"] = real_cfg
    print(f"  التعديل مطفأ في الإعداد  : {refused[:66]}")
    problems += 0 if "switched off" in refused else 1

    print(f"  run(action مجهول)       : {run({'action': 'sing'})[:58]}")
    problems += 0 if "not a code action" in run({"action": "sing"}) else 1
    print(f"  run بلا استعلام         : {run({'action': 'search'})[:60]}")
    problems += 0 if "nothing to search" in run({"action": "search"}) else 1

    # the code-level promises: no shell, no eval/exec, no import of read files
    import ast as _ast
    source = Path(__file__).read_text(encoding="utf-8")
    tree = _ast.parse(source)
    bads = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            if isinstance(node.func, _ast.Name) and node.func.id in ("eval", "exec", "compile", "__import__"):
                bads.append(node.func.id)
            if isinstance(node.func, _ast.Attribute) and node.func.attr in ("system", "popen"):
                bads.append("os." + node.func.attr)
        if isinstance(node, _ast.keyword) and node.arg == "shell":
            if not (isinstance(node.value, _ast.Constant) and node.value.value is False):
                bads.append("shell=")
    print(f"  لا shell/eval/exec      : {bads or 'لا شيء — الملف لا ينفّذ ما يقرؤه'}")
    problems += 0 if not bads else 1

    shutil.rmtree(probe, ignore_errors=True)
    for leftover in output_dir().glob("*"):
        if leftover.is_file() and leftover.name.startswith("target.proposed"):
            leftover.unlink()
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
