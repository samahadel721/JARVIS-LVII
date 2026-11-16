"""
knowledge_base.py — indexing the folders you allow, and nothing else.

THE RULE THAT MATTERS MOST
    This plugin does not browse your computer. It indexes exactly the folders
    you list in its settings, and a path outside them is refused even when you
    ask for it by name. That is not a limitation to be worked around; it is the
    whole point of having the setting. Empty folder list = it indexes nothing
    and says so.

    Symlinks are not followed unless you turn that on, because a symlink is a
    door out of the folder you authorised. Hidden files, .git, node_modules and
    __pycache__ are skipped.

WHERE THE TEXT GOES
    Nowhere. Extraction is local for every format: plain reading for text, and
    pdfplumber/PyPDF2/python-docx/python-pptx for the rest — the same libraries
    the project's own file reader uses. No file is ever uploaded, and embeddings
    (which would send text to Gemini) are a separate, explicit switch in the
    Semantic search plugin, off by default.

WHAT IT DOES WITH THE MEMORY SYSTEM
    It does not replace it and it does not fill it. After an index run it writes
    ONE line into memory saying what it has — a count and a folder — because
    knowing it has a knowledge base is useful context for the assistant. That is
    the only thing it ever puts there, and it can be switched off.

SETTINGS (Plugin Manager → Knowledge base)
    roots        the folders you authorise — empty means nothing is indexed
    extensions   txt, md, pdf, docx, … which file types to read
    max_file_mb  2 — larger files are skipped and named in the report
    max_files    5000 — a hard ceiling per run, so an index never runs away
    ocr_images   off | on — read text out of images too (needs the OCR plugin)
    follow_symlinks  off — on lets a link point outside your folders
    remember_index   on — write the one-line summary into memory
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "knowledge_base",
    "description": (
        "Reads and remembers the folders you have authorised, so questions can be "
        "answered from your own documents: 'index my documents folder', 'what do my "
        "files say about the water plant', 'how many files are indexed'. It only ever "
        "touches the folders listed in its settings."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "index | search | list | show | remove | rebuild | roots | status | export"},
            "path": {"type": "STRING", "description": "A folder or file inside an authorised root"},
            "query": {"type": "STRING", "description": "What to search for"},
            "limit": {"type": "NUMBER", "description": "How many results, default 5"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "knowledge_base",
    "title": "Knowledge base / قاعدة المعرفة",
    "fields": [
        {"key": "roots", "type": "text", "label": "Authorised folders (comma separated)",
         "default": "", "placeholder": "C:/Users/me/Documents, D:/Work"},
        {"key": "extensions", "type": "text", "label": "File types",
         "default": "txt,md,rst,log,csv,json,py,js,ts,html,css,yaml,yml,toml,ini,cfg,pdf,docx,pptx"},
        {"key": "max_file_mb", "type": "text", "label": "Largest file to read, MB", "default": "2"},
        {"key": "max_files", "type": "text", "label": "Most files per run", "default": "5000"},
        {"key": "chunk_chars", "type": "text", "label": "Characters per chunk", "default": "1200"},
        {"key": "ocr_images", "type": "choice", "label": "Read text from images too (slow)",
         "default": "off", "choices": ["off", "on"]},
        {"key": "follow_symlinks", "type": "choice", "label": "Follow symlinks (can escape the folder)",
         "default": "off", "choices": ["off", "on"]},
        {"key": "remember_index", "type": "choice", "label": "Write a one-line summary into memory",
         "default": "on", "choices": ["off", "on"]},
    ],
}

INDEX_PATH = Path(__file__).resolve().parent.parent / "config" / "knowledge" / "index.json"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
SKIP_DIRS = {"__pycache__", ".git", ".svn", "node_modules", ".venv", "venv", "dist",
             "build", ".idea", ".vscode", "$recycle.bin", "system volume information"}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("knowledge_base") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _int_cfg(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(_cfg().get(key) or default))))
    except Exception:
        return default


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("on", "yes", "true", "1")


def roots() -> list[Path]:
    """The authorised folders, resolved. Empty list means nothing is allowed."""
    raw = str(_cfg().get("roots") or "")
    out: list[Path] = []
    for part in re.split(r"[,\n;]", raw):
        part = part.strip().strip('"')
        if not part:
            continue
        try:
            p = Path(part).expanduser().resolve()
        except Exception:
            continue
        if p.exists() and p not in out:
            out.append(p)
    return out


def _extensions() -> set[str]:
    raw = str(_cfg().get("extensions") or "")
    exts = {"." + e.strip().lower().lstrip(".") for e in raw.split(",") if e.strip()}
    return exts or {".txt", ".md", ".pdf", ".docx"}


def is_authorized(path: str | Path) -> tuple[bool, str]:
    """
    Is this path inside a folder the user authorised?

    Resolves every symlink and '..' first, then compares against the roots — so
    '../../windows/system32' fails no matter how it is written.
    """
    try:
        p = Path(path).expanduser().resolve()
    except Exception as e:
        return False, f"I could not make sense of that path: {str(e)[:60]}"
    allowed = roots()
    if not allowed:
        return False, ("no folders are authorised yet — add them in the Knowledge base "
                       "settings and I will index exactly those")
    for root in allowed:
        try:
            p.relative_to(root)
            return True, ""
        except ValueError:
            continue
    return False, (f"'{p}' is outside every folder you authorised. I will not read it — "
                   f"authorised: {', '.join(str(r) for r in allowed)}")


def _iter_files(folder: Path) -> list[Path]:
    """Every readable file under a folder, minus the noise and the escapes."""
    follow = _truthy(_cfg().get("follow_symlinks"))
    exts = _extensions()
    want_images = _truthy(_cfg().get("ocr_images"))
    out: list[Path] = []
    for current, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs
                   if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            p = Path(current) / name
            ext = p.suffix.lower()
            if ext not in exts and not (want_images and ext in IMAGE_EXTS):
                continue
            if p.is_symlink() and not follow:
                continue
            out.append(p)
            if len(out) >= _int_cfg("max_files", 5000, 1, 100_000):
                return out
    return out


# ── extraction: local, always ─────────────────────────────────────────────────

def _repo_detect_type(path: Path) -> str:
    """Reuse the project's own classifier when it is reachable, else do it here."""
    try:
        import importlib.util
        target = Path(__file__).resolve().parent.parent / "actions" / "file_processor.py"
        if target.exists():
            spec = importlib.util.spec_from_file_location("_jarvis_fp", target)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return str(module._detect_type(path))
    except Exception:
        pass
    ext = path.suffix.lower().lstrip(".")
    if ext == "pdf":
        return "pdf"
    if ext in ("docx", "doc"):
        return "docx"
    if ext == "pptx":
        return "pptx"
    if ext in ("txt", "md", "rst", "log", "csv", "json", "py", "js", "yaml", "yml", "toml", "html"):
        return "text"
    return "unknown"


def extract_text(path: str | Path) -> tuple[bool, str]:
    """
    Pull the text out of one file, locally. Returns (ok, text_or_reason).

    This is the suite's single extractor: the summariser and the translator
    borrow it rather than each writing their own. Nothing here calls a network.
    """
    p = Path(path)
    try:
        if not p.is_file():
            return False, f"'{p.name}' is not a file"
    except Exception as e:
        return False, f"path problem: {str(e)[:60]}"
    kind = _repo_detect_type(p)
    ext = p.suffix.lower()

    if ext in (".txt", ".md", ".rst", ".log", ".csv", ".json", ".py", ".js", ".ts", ".html",
               ".css", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".sql") or kind == "text":
        try:
            return True, p.read_text(encoding="utf-8", errors="ignore")
        except PermissionError:
            return False, f"I do not have permission to read {p.name}"
        except Exception as e:
            return False, f"I could not read {p.name}: {str(e)[:70]}"

    if kind == "docx" or ext == ".docx":
        try:
            from docx import Document
            doc = Document(str(p))
            parts = [para.text for para in doc.paragraphs]
            for table in doc.tables:
                for row in table.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            return True, "\n".join(parts)
        except ImportError:
            return False, (f"reading {p.name} needs python-docx (pip install python-docx) — "
                           f"it is in the project's requirements")
        except PermissionError:
            return False, f"I do not have permission to read {p.name}"
        except Exception as e:
            return False, f"I could not read {p.name}: {str(e)[:70]}"

    if kind == "pdf" or ext == ".pdf":
        try:
            import pdfplumber
            text = []
            with pdfplumber.open(str(p)) as pdf:
                for page in pdf.pages[:200]:
                    text.append(page.extract_text() or "")
            joined = "\n".join(text).strip()
            if joined:
                return True, joined
        except ImportError:
            pass
        except Exception:
            pass
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(str(p))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:200])
            if text.strip():
                return True, text
            return False, (f"{p.name} has no text layer — it is a scan, so it would need "
                           f"the OCR plugin to read it")
        except ImportError:
            return False, (f"reading {p.name} needs pdfplumber or PyPDF2 — both are in the "
                           f"project's requirements")
        except PermissionError:
            return False, f"I do not have permission to read {p.name}"
        except Exception as e:
            return False, f"I could not read {p.name}: {str(e)[:70]}"

    if ext == ".pptx" or kind == "pptx":
        try:
            from pptx import Presentation
            prs = Presentation(str(p))
            parts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text:
                        parts.append(shape.text)
            return True, "\n".join(parts)
        except ImportError:
            return False, f"reading {p.name} needs python-pptx (it is in the requirements)"
        except Exception as e:
            return False, f"I could not read {p.name}: {str(e)[:70]}"

    if ext in IMAGE_EXTS:
        return _read_image(p)

    return False, f"'{ext or p.name}' is not a format I can read"


def _read_image(p: Path) -> tuple[bool, str]:
    """Images only when the user asked for it — OCR is slow, and it is optional."""
    if not _truthy(_cfg().get("ocr_images")):
        return False, "images are skipped (turn on 'read text from images' to include them)"
    ocr = _sibling("ocr")
    if ocr is None or not hasattr(ocr, "extract"):
        return False, "the OCR plugin is not next to me, so I cannot read this image"
    try:
        result = ocr.extract(p)
    except Exception as e:
        return False, f"OCR failed on {p.name}: {str(e)[:60]}"
    if result.get("ok"):
        return True, f"[text read from the image {p.name}]\n{result['text']}"
    return False, f"{result.get('reason', 'the image could not be read')}"


def _sibling(name: str):
    import sys
    cached = sys.modules.get(f"_jarvis_{name}")
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parent / f"{name}.py"
    if not path.exists():
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"_jarvis_{name}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop(f"_jarvis_{name}", None)
        return None


def chunk_text(text: str, size: int = 1200) -> list[str]:
    """
    Split on paragraph boundaries, then on length. Chunks overlap by a sentence's
    worth so a fact sitting on a boundary is still findable from either side.
    """
    text = (text or "").strip()
    if not text:
        return []
    size = max(200, size)
    blocks: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= size:
            current = f"{current}\n\n{para}".strip()
            continue
        if current:
            blocks.append(current)
        while len(para) > size:
            cut = para.rfind(" ", 0, size)
            cut = cut if cut > size * 0.6 else size
            blocks.append(para[:cut].strip())
            para = para[max(cut - 120, 0):].strip()      # small overlap
        current = para
    if current:
        blocks.append(current)
    return [b for b in blocks if b.strip()]


# ── the index on disk ─────────────────────────────────────────────────────────

def _load() -> dict[str, Any]:
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("files"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        return {"files": [], "damaged": True}
    return {"files": []}


def _save(data: dict[str, Any]) -> None:
    data["updated"] = time.time()
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(INDEX_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, INDEX_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _fingerprint(p: Path) -> str:
    """Fast identity check: size and modified time, plus a hash of the first 8 KB."""
    try:
        stat = p.stat()
        head = p.open("rb").read(8192)
        digest = hashlib.sha1(head).hexdigest()[:12]
        return f"{stat.st_size}:{int(stat.st_mtime)}:{digest}"
    except Exception:
        return ""


def index_paths(paths: list[Path], replace: bool = False) -> dict[str, Any]:
    """
    Index a list of files. Returns a report — never an exception, never a claim
    that a file was read when it was not.
    """
    data = _load()
    if replace:
        data = {"files": []}
    known = {f["path"]: f for f in data.get("files", [])}
    size_cap = _int_cfg("max_file_mb", 2, 1, 200) * 1024 * 1024
    chunk_size = _int_cfg("chunk_chars", 1200, 200, 8000)

    added, updated, skipped, failed = 0, 0, 0, []
    for p in paths:
        try:
            if not p.is_file():
                continue
            stat = p.stat()
            if stat.st_size > size_cap:
                skipped += 1
                failed.append(f"{p.name}: larger than {size_cap // (1024*1024)} MB")
                continue
            fp = _fingerprint(p)
            old = known.get(str(p))
            if old and old.get("fingerprint") == fp and not replace:
                skipped += 1
                continue
            ok, text = extract_text(p)
            if not ok:
                failed.append(f"{p.name}: {text[:70]}")
                continue
            chunks = chunk_text(text, chunk_size)
            if not chunks:
                failed.append(f"{p.name}: no readable text inside")
                continue
            entry = {"path": str(p), "name": p.name, "kind": p.suffix.lower().lstrip("."),
                     "size": stat.st_size, "mtime": int(stat.st_mtime), "fingerprint": fp,
                     "chunks": chunks, "words": len(text.split()), "indexed": time.time()}
            if old:
                updated += 1
            else:
                added += 1
            known[str(p)] = entry
        except PermissionError:
            skipped += 1
            failed.append(f"{p.name}: permission denied")
        except Exception as e:
            skipped += 1
            failed.append(f"{p.name}: {str(e)[:60]}")

    data["files"] = list(known.values())
    try:
        _save(data)
    except Exception as e:
        return {"ok": False, "reason": f"I could not write the index: {str(e)[:80]}",
                "added": 0, "updated": 0, "skipped": 0, "failed": failed}
    _push_to_search(data["files"])
    return {"ok": True, "added": added, "updated": updated, "skipped": skipped,
            "failed": failed, "total": len(data["files"])}


def _push_to_search(files: list[dict[str, Any]]) -> int:
    """Hand the chunks to the search engine. Missing engine is not an error."""
    sem = _sibling("semantic_search")
    if sem is None or not hasattr(sem, "index_docs"):
        return 0
    docs = []
    for f in files:
        for i, chunk in enumerate(f.get("chunks", [])):
            docs.append({"id": f"{f['path']}#{i}", "source": f["path"], "title": f.get("name", ""),
                         "text": chunk, "kind": "file"})
    try:
        return int(sem.index_docs(docs, replace_source=""))
    except Exception:
        return 0


def rebuild() -> dict[str, Any]:
    """Forget everything and read the authorised folders again from scratch."""
    paths: list[Path] = []
    for root in roots():
        if root.is_file():
            paths.append(root)
        else:
            paths.extend(_iter_files(root))
    return index_paths(paths, replace=True)


def _remember(report: dict[str, Any]) -> None:
    """One line into memory — the only thing this plugin ever puts there."""
    if not _truthy(_cfg().get("remember_index")):
        return
    try:
        from memory import memory_manager
        folders = ", ".join(r.name for r in roots()[:3]) or "none"
        memory_manager.remember("knowledge_base",
                                f"{report.get('total', 0)} file(s) indexed from {folders}",
                                "projects")
    except Exception:
        pass


def search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search the indexed text. Delegates to the search plugin so there is one engine."""
    sem = _sibling("semantic_search")
    if sem is None or not hasattr(sem, "search"):
        return []
    try:
        return list(sem.search(query, limit=limit, scope="files"))
    except Exception:
        return []


def stats() -> dict[str, Any]:
    data = _load()
    files = data.get("files", [])
    return {"files": len(files), "chunks": sum(len(f.get("chunks", [])) for f in files),
            "words": sum(int(f.get("words", 0)) for f in files),
            "roots": [str(r) for r in roots()], "updated": data.get("updated", 0),
            "damaged": bool(data.get("damaged"))}


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one knowledge-base action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "status").strip().lower()

        if action in ("index", "scan", "read"):
            if not roots():
                return ("I have no authorised folders, sir, so I will not read anything. Add "
                        "them in the Knowledge base settings — one or more folders — and I "
                        "will index exactly those and nothing else.")
            target = str(p.get("path") or "").strip()
            paths: list[Path] = []
            if target:
                ok, why = is_authorized(target)
                if not ok:
                    return f"Sir, {why}."
                tp = Path(target).expanduser().resolve()
                paths = [tp] if tp.is_file() else _iter_files(tp)
            else:
                for root in roots():
                    paths.extend([root] if root.is_file() else _iter_files(root))
            if player:
                try:
                    player.write_log(f"[KB] indexing {len(paths)} file(s)…")
                except Exception:
                    pass
            report = index_paths(paths)
            if not report.get("ok"):
                return f"Sir, {report['reason']}."
            _remember(report)
            msg = (f"Indexed, sir: {report['added']} new, {report['updated']} changed, "
                   f"{report['skipped']} unchanged or skipped — {report['total']} file(s) in "
                   f"the base now.")
            if report["failed"]:
                msg += f"\n{len(report['failed'])} could not be read:\n  " + \
                       "\n  ".join(report["failed"][:6])
                if len(report["failed"]) > 6:
                    msg += f"\n  … and {len(report['failed']) - 6} more"
            return msg

        if action in ("search", "find", "ask"):
            query = str(p.get("query") or "").strip()
            if not query:
                return "What should I look for, sir?"
            if not _load().get("files"):
                return ("Nothing is indexed yet, sir — say 'index my documents' once the "
                        "folder is authorised in my settings.")
            hits = search(query, limit=int(float(p.get("limit") or 5)))
            if not hits:
                return (f"Nothing in the indexed files matches '{query}', sir. I will not "
                        f"present an unrelated file as an answer.")
            lines = [f"{len(hits)} match(es) for '{query}' in your files:"]
            for i, h in enumerate(hits, 1):
                body = " ".join(str(h.get("text", "")).split())[:240]
                lines.append(f"\n{i}. {h.get('title')}  [{h.get('score')}, by {h.get('how')}]"
                             f"\n   {h.get('source')}\n   {body}")
            return "\n".join(lines)

        if action in ("list", "files"):
            files = sorted(_load().get("files", []), key=lambda f: -f.get("mtime", 0))
            if not files:
                return "Nothing is indexed yet, sir."
            lines = [f"{len(files)} file(s) indexed:"]
            for f in files[:15]:
                lines.append(f"  {f['name']}  ({f.get('kind')}, {f.get('words', 0):,} words)")
            if len(files) > 15:
                lines.append(f"  … and {len(files) - 15} more")
            return "\n".join(lines)

        if action in ("show", "read_file", "contents"):
            target = str(p.get("path") or "").strip()
            if not target:
                return "Which file, sir?"
            ok, why = is_authorized(target)
            if not ok:
                return f"Sir, {why}."
            pth = Path(target).expanduser().resolve() if not Path(target).exists() \
                else Path(target).resolve()
            entry = next((f for f in _load().get("files", []) if f["path"] == str(pth)), None)
            if entry is None:
                ok2, text = extract_text(pth)
                if not ok2:
                    return f"Sir, {text}."
                return f"{pth.name}:\n\n{text[:4000]}"
            return f"{entry['name']} ({entry['words']:,} words):\n\n" + \
                   "\n\n".join(entry["chunks"])[:4000]

        if action in ("remove", "forget"):
            target = str(p.get("path") or "").strip()
            if not target:
                return "Which file should I forget, sir?"
            data = _load()
            before = len(data.get("files", []))
            data["files"] = [f for f in data.get("files", [])
                             if Path(f["path"]).name != Path(target).name
                             and f["path"] != target]
            if len(data["files"]) == before:
                return f"I have nothing indexed from '{target}', sir."
            _save(data)
            sem = _sibling("semantic_search")
            if sem is not None and hasattr(sem, "remove_source"):
                try:
                    sem.remove_source(str(Path(target).expanduser().resolve()))
                except Exception:
                    pass
            return (f"Forgotten: {before - len(data['files'])} file(s) — the file itself is "
                    f"untouched, only my index entry is gone.")

        if action == "rebuild":
            if not roots():
                return "There is nothing authorised to rebuild from, sir."
            report = rebuild()
            if not report.get("ok"):
                return f"Sir, {report['reason']}."
            _remember(report)
            return (f"Rebuilt from scratch, sir: {report['total']} file(s) "
                    f"({len(report['failed'])} could not be read).")

        if action in ("roots", "folders"):
            allowed = roots()
            if not allowed:
                return ("No folders are authorised, sir — so I read nothing at all. Add them "
                        "in my settings.")
            return "I am allowed to read these folders:\n" + \
                   "\n".join(f"  {r}" for r in allowed)

        if action in ("export", "document"):
            query = str(p.get("query") or "").strip()
            if not query:
                return "What should the document be about, sir?"
            hits = search(query, limit=int(float(p.get("limit") or 8)))
            if not hits:
                return f"Nothing indexed matches '{query}', sir, so I have nothing to export."
            docgen = _sibling("document_generator")
            if docgen is None:
                return "The document generator is not next to me, sir, so I cannot export."
            body = f"## Query\n{query}\n\n## Matches\n\n"
            for h in hits:
                body += f"### {h.get('title')}\n{h.get('source')}\n\n{h.get('text')}\n\n"
            result = docgen.generate(f"Knowledge base: {query}", body, fmt="md",
                                     filename=f"kb_{query[:30]}")
            if not result.get("ok"):
                return f"Sir, {result['reason']}."
            return f"Exported, sir — saved to:\n{result['path']}"

        if action in ("status", "info", "stats"):
            s = stats()
            if not s["roots"]:
                return ("Nothing indexed and no folders authorised, sir. Add a folder in the "
                        "Knowledge base settings and say 'index my documents'.")
            return (f"Authorised folders : {len(s['roots'])}\n"
                    f"Files indexed      : {s['files']}\n"
                    f"Text chunks        : {s['chunks']:,}\n"
                    f"Words              : {s['words']:,}\n"
                    f"Images read        : {'on (through the OCR plugin)' if _truthy(_cfg().get('ocr_images')) else 'off'}\n"
                    f"Symlinks           : {'followed' if _truthy(_cfg().get('follow_symlinks')) else 'not followed'}"
                    + (f"\n(Warning: the index file is damaged and reads as empty.)"
                       if s["damaged"] else ""))

        return (f"Sir, '{action}' is not a knowledge-base action I have. I can index, search, "
                f"list, show, remove, rebuild, export, or show the authorised folders.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the knowledge base hit a problem: {str(e)[:150]}"


# ── self-test: python knowledge_base.py ──────────────────────────────────────

def _selftest() -> int:
    print("knowledge_base — self-test")
    problems = 0

    tmp_root = Path(__file__).resolve().parent / "_kb_probe_root"
    outside = Path(__file__).resolve().parent / "_kb_probe_outside"
    for d in (tmp_root, outside):
        d.mkdir(exist_ok=True)
    (tmp_root / "plant.md").write_text(
        "# Water plant\n\nThe new plant opens in Mansoura on 14 March 2026.\n"
        "It cost 240 million pounds and serves 400,000 residents.\n", encoding="utf-8")
    (tmp_root / "note.txt").write_text("Bread recipe: 40 minutes at 200 degrees.",
                                       encoding="utf-8")
    (tmp_root / "big.txt").write_text("x" * (3 * 1024 * 1024), encoding="utf-8")
    (tmp_root / "sub").mkdir(exist_ok=True)
    (tmp_root / "sub" / "deep.txt").write_text("Deep file about pumps and valves.",
                                               encoding="utf-8")
    (outside / "secret.txt").write_text("Private file that must NOT be indexed.",
                                        encoding="utf-8")

    global _cfg
    real_cfg = _cfg
    backup = INDEX_PATH.read_text(encoding="utf-8") if INDEX_PATH.exists() else None
    _cfg = lambda: {**real_cfg(), "roots": str(tmp_root), "max_file_mb": "2"}

    try:
        print(f"  authorised roots    : {[r.name for r in roots()]}")
        problems += 0 if roots() else 1

        inside_ok, _ = is_authorized(tmp_root / "plant.md")
        out_ok, why = is_authorized(outside / "secret.txt")
        trav_ok, _ = is_authorized(str(tmp_root / ".." / "_kb_probe_outside" / "secret.txt"))
        print(f"  inside allowed      : {inside_ok}  outside refused: {not out_ok}  "
              f"'..' refused: {not trav_ok}")
        problems += 0 if inside_ok and not out_ok and not trav_ok else 1
        print(f"  refusal message     : {why[:76]}")

        # no folder authorised → nothing may be read, whatever the path
        _cfg = lambda: {**real_cfg(), "roots": ""}
        locked, locked_why = is_authorized(tmp_root / "plant.md")
        print(f"  empty roots = locked: {not locked} — {locked_why[:60]}")
        problems += 0 if not locked else 1
        _cfg = lambda: {**real_cfg(), "roots": str(tmp_root), "max_file_mb": "2"}

        report = index_paths([tmp_root / "plant.md", tmp_root / "note.txt",
                              tmp_root / "big.txt", tmp_root / "sub" / "deep.txt"])
        print(f"  index               : added={report['added']} skipped={report['skipped']} "
              f"failed={len(report['failed'])} total={report['total']}")
        problems += 0 if report["added"] == 3 and report["total"] == 3 else 1
        print(f"  oversize handled    : {[f for f in report['failed']][:1]}")
        problems += 0 if any("larger than" in f for f in report["failed"]) else 1

        again = index_paths([tmp_root / "plant.md"])
        print(f"  re-index unchanged  : added={again['added']} skipped={again['skipped']} "
              f"(no needless re-reading)")
        problems += 0 if again["added"] == 0 else 1

        ok, text = extract_text(tmp_root / "plant.md")
        print(f"  extract_text()      : ok={ok} chars={len(text)}")
        problems += 0 if ok and "240 million" in text else 1
        miss_ok, miss = extract_text(outside / "secret.txt")
        print(f"  extract is local    : ok={miss_ok} (a path is a path here — the guard is at "
              f"the index and action level)")

        hits = search("water plant")
        print(f"  search →            : {len(hits)} hit(s) → {hits[0].get('title') if hits else '—'}")
        problems += 0 if hits and "plant.md" in str(hits[0].get("source")) else 1
        none = search("xylophone submarine racing")
        print(f"  no match is honest  : {len(none)} hit(s)")
        problems += 0 if not none else 1

        chunks = chunk_text("para one. " * 400, 500)
        print(f"  chunking            : {len(chunks)} chunk(s) from a 4k text, "
              f"sizes {[len(c) for c in chunks][:3]}")
        problems += 0 if len(chunks) >= 8 else 1

        print(f"  run: index          : {run({'action': 'index'}).splitlines()[0][:78]}")
        print(f"  run: search         : {run({'action': 'search', 'query': 'pumps'}).splitlines()[0][:78]}")
        print(f"  run: status         : {run({'action': 'status'}).splitlines()[1][:60]}")
        print(f"  run: list           : {run({'action': 'list'}).splitlines()[0][:60]}")
        print(f"  remove              : {run({'action': 'remove', 'path': 'note.txt'})[:74]}")
        print(f"  bad action          : {run({'action': 'sing'})[:70]}")
        problems += 0 if "not a knowledge-base action" in run({"action": "sing"}) else 1
        print(f"  outside via action  : {run({'action': 'index', 'path': str(outside)})[:80]}")
        problems += 0 if "outside every folder" in run({"action": "index", "path": str(outside)}) else 1
    finally:
        _cfg = real_cfg
        sem = _sibling("semantic_search")
        if sem is not None and hasattr(sem, "remove_source"):
            for f in _load().get("files", []):
                try:
                    sem.remove_source(f["path"])
                except Exception:
                    pass
        if backup is None:
            INDEX_PATH.unlink(missing_ok=True)
        else:
            INDEX_PATH.write_text(backup, encoding="utf-8")
        for d in (tmp_root, outside):
            for f in sorted(d.rglob("*"), reverse=True):
                try:
                    f.unlink() if f.is_file() else f.rmdir()
                except Exception:
                    pass
            try:
                d.rmdir()
            except Exception:
                pass

    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
