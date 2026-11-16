"""
note_manager.py — your notes, kept on your machine, findable by meaning.

WHAT A NOTE IS HERE
    title · content · tags · category · created · updated. Stored in one JSON
    file under config/notes/, written atomically so a power cut never leaves you
    half a file. Nothing is sent anywhere to save a note.

HOW IT CONNECTS TO THE THINGS AROUND IT
    Memory system   A note is NOT written into the assistant's memory by
                    default, and that is deliberate: memory is injected into
                    every prompt the model sees and it has a size limit, so
                    filling it with notes would push out the things the
                    assistant actually needs to remember about you. Use "pin"
                    when a note deserves to be remembered permanently — that
                    calls memory_manager.remember() and mirrors just that one.

    Semantic search Every add, edit and delete updates the shared search index,
                    so notes are searchable by meaning alongside indexed files.

    Other plugins   research_assistant and deep_research save their reports here
                    through add_note() — that is why the API exists.

NOTHING IS EVER DELETED QUIETLY
    Deleting goes through the confirmation gate in core/confirm.py, the same one
    the rest of the app uses. If the gate cannot be shown, the note stays. There
    is no path through this file that removes a note without you pressing yes.

SETTINGS (Plugin Manager → Notes)
    store_path   blank = config/notes/notes.json
    template     blank | meeting | idea | todo | research | journal
    max_notes    500 — oldest unpinned notes are never auto-deleted, only reported
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "note_manager",
    "description": (
        "Saves, edits, tags and finds your notes: 'make a note about the water plant', "
        "'what did I write about the trip', 'add a tag to that note', 'delete the note "
        "about groceries'. Notes live on this machine only."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "add | edit | delete | find | list | show | tag | templates | status"},
            "title": {"type": "STRING", "description": "Note title"},
            "content": {"type": "STRING", "description": "Note body"},
            "query": {"type": "STRING", "description": "What to search for"},
            "tags": {"type": "STRING", "description": "Comma-separated tags"},
            "tag": {"type": "STRING", "description": "One tag to add or remove"},
            "category": {"type": "STRING", "description": "Optional group"},
            "id": {"type": "STRING", "description": "Note id or a unique part of its title"},
            "template": {"type": "STRING", "description": "meeting | idea | todo | research | journal"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "note_manager",
    "title": "Notes / الملاحظات",
    "fields": [
        {"key": "store_path", "type": "text", "label": "Notes file (blank = config/notes/notes.json)",
         "default": ""},
        {"key": "template", "type": "choice", "label": "Default template",
         "default": "blank", "choices": ["blank", "meeting", "idea", "todo", "research", "journal"]},
        {"key": "max_notes", "type": "text", "label": "Warn above this many notes (500)",
         "default": "500"},
    ],
}

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "notes" / "notes.json"

TEMPLATES: dict[str, str] = {
    "blank": "",
    "meeting": "Attendees:\n\nAgenda:\n\nDecisions:\n\nFollow-ups:\n",
    "idea": "The idea:\n\nWhy it could work:\n\nFirst step:\n\nRisks:\n",
    "todo": "To do:\n- \n- \n\nWaiting on:\n- \n",
    "research": "Question:\n\nWhat I found:\n\nSources:\n\nWhat is still unclear:\n",
    "journal": "What happened:\n\nHow it felt:\n\nWhat I would do differently:\n",
}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("note_manager") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def store_path() -> Path:
    raw = str(_cfg().get("store_path") or "").strip()
    p = Path(raw).expanduser() if raw else DEFAULT_PATH
    try:
        return p.resolve()
    except Exception:
        return DEFAULT_PATH


def _load() -> dict[str, Any]:
    path = store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("notes"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        return {"notes": [], "damaged": True}
    return {"notes": []}


def _save(data: dict[str, Any]) -> bool:
    """Atomic: temp file, then replace. Never leaves a half-written notes file."""
    path = store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _norm(text: str) -> str:
    t = (text or "").lower().replace("\u0640", "")
    for ch in "\u0622\u0623\u0625":
        t = t.replace(ch, "\u0627")
    return t.replace("\u0649", "\u064a").replace("\u0629", "\u0647")


def _now() -> float:
    return time.time()


def _stamp(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return "?"


def _sibling(name: str):
    """Load a sibling plugin by path — the pattern the rest of the suite uses."""
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


def _reindex_full() -> int:
    """Rebuild the note half of the search index — used after a delete."""
    sem = _sibling("semantic_search")
    if sem is None or not hasattr(sem, "index_docs"):
        return 0
    notes = _load().get("notes", [])
    docs = [{"id": f"note:{n['id']}", "source": f"note:{n['id']}", "title": n.get("title", ""),
             "text": f"{n.get('title', '')}\n{n.get('content', '')}\n{' '.join(n.get('tags', []))}",
             "kind": "note"} for n in notes]
    try:
        return int(sem.index_docs(docs, replace_source=""))
    except Exception:
        return 0


def add_note(title: str, content: str, tags: list[str] | None = None,
             category: str = "", source: str = "") -> dict[str, Any]:
    """
    The reusable API — this is what the research plugins call to save a report.
    Returns the stored note. Never raises.
    """
    title = str(title or "").strip()[:200]
    content = str(content or "").strip()
    if not title and not content:
        return {"ok": False, "reason": "there is nothing to save"}
    if not title:
        title = (content.splitlines()[0] if content else "Untitled")[:80]
    data = _load()
    note = {
        "id": uuid.uuid4().hex[:10],
        "title": title,
        "content": content,
        "tags": [t.strip() for t in (tags or []) if str(t).strip()][:20],
        "category": str(category or "").strip()[:60],
        "source": str(source or "").strip()[:120],
        "created": _now(),
        "updated": _now(),
        "pinned": False,
    }
    data.setdefault("notes", []).append(note)
    if not _save(data):
        return {"ok": False, "reason": "I could not write the notes file"}
    sem = _sibling("semantic_search")
    if sem is not None:
        try:
            sem.index_docs([{"id": f"note:{note['id']}", "source": f"note:{note['id']}",
                             "title": note["title"],
                             "text": f"{note['title']}\n{note['content']}", "kind": "note"}])
        except Exception:
            pass
    return {"ok": True, "note": note}


def find_notes(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """
    Search notes: by meaning through the search index when it exists, then a
    plain text search as a safety net. Returns [] honestly when nothing matches.
    """
    query = str(query or "").strip()
    if not query:
        return []
    notes = {n["id"]: n for n in _load().get("notes", [])}
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    sem = _sibling("semantic_search")
    if sem is not None and hasattr(sem, "search"):
        try:
            for r in sem.search(query, limit=limit * 2, scope="notes"):
                nid = str(r.get("source", "")).replace("note:", "")
                note = notes.get(nid)
                if note and nid not in seen:      # by id, never by dict equality
                    seen.add(nid)
                    hits.append({**note, "score": r.get("score"), "how": r.get("how")})
        except Exception:
            pass
    words = [w for w in _norm(query).split() if len(w) > 2]
    for nid, note in notes.items():
        if nid in seen:
            continue
        blob = _norm(f"{note.get('title', '')} {note.get('content', '')} "
                     f"{' '.join(note.get('tags', []))}")
        if all(w in blob for w in words) or _norm(query) in blob:
            hits.append({**note, "score": None, "how": "text"})
    return hits[:limit]


def _resolve(identifier: str) -> dict[str, Any] | None:
    """Find one note by id, or by a unique part of its title."""
    ident = str(identifier or "").strip()
    if not ident:
        return None
    notes = _load().get("notes", [])
    for n in notes:
        if n["id"] == ident:
            return n
    matches = [n for n in notes if _norm(ident) in _norm(n.get("title", ""))]
    return matches[0] if len(matches) == 1 else (matches[0] if matches and len(ident) > 5 else None)


def _overview(notes: list[dict[str, Any]]) -> str:
    if not notes:
        return "You have no notes yet, sir."
    out = []
    for n in notes[:20]:
        tags = f"  #{' #'.join(n['tags'])}" if n.get("tags") else ""
        pin = " ★" if n.get("pinned") else ""
        first = (n.get("content") or "").splitlines()[0][:60] if n.get("content") else ""
        out.append(f"{n['id']}  {n['title']}{pin}{tags}\n      {_stamp(n.get('updated', 0))}"
                   + (f" · {first}" if first else ""))
    return "\n".join(out)


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one note action. Never raises, never deletes silently."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "list").strip().lower()
        cfg = _cfg()

        if action in ("add", "save", "new", "remember"):
            title = str(p.get("title") or "").strip()
            content = str(p.get("content") or "")
            template = str(p.get("template") or "").strip().lower()
            if not title and not content:
                return ("What should the note say, sir?")
            if template and template in TEMPLATES and not content.strip():
                content = TEMPLATES[template]
            tags = [t.strip() for t in str(p.get("tags") or "").split(",") if t.strip()]
            result = add_note(title, content, tags, str(p.get("category") or ""))
            if not result["ok"]:
                return f"Sir, {result['reason']}."
            note = result["note"]
            if player:
                try:
                    player.write_log(f"[Note] {note['id']} {note['title']}")
                except Exception:
                    pass
            return f"Noted, sir — {note['id']} · {note['title']}" \
                   + (f" · #{' #'.join(note['tags'])}" if note["tags"] else "")

        if action in ("find", "search", "what"):
            query = str(p.get("query") or p.get("title") or "").strip()
            if not query:
                return "What should I look for, sir?"
            hits = find_notes(query, limit=int(float(p.get("limit") or 5)))
            if not hits:
                return (f"No note of yours mentions '{query}', sir — and I will not offer a "
                        f"near-miss as if it were a match.")
            return f"{len(hits)} note(s) matching '{query}':\n\n" + _overview(hits)

        if action in ("list", "all"):
            notes = sorted(_load().get("notes", []), key=lambda n: -n.get("updated", 0))
            warn = ""
            try:
                limit = int(str(cfg.get("max_notes") or "500"))
                if len(notes) > limit:
                    warn = f"\n(that is more than the {limit} you set — nothing was deleted.)"
            except Exception:
                pass
            return _overview(notes) + warn

        if action in ("show", "open", "read"):
            note = _resolve(str(p.get("id") or p.get("title") or ""))
            if not note:
                return "I could not find that note, sir — give me its id or a clearer title."
            return (f"{note['title']}  ({note['id']})\n"
                    f"created {_stamp(note.get('created', 0))} · updated {_stamp(note.get('updated', 0))}"
                    + (f"\ncategory: {note['category']}" if note.get("category") else "")
                    + (f"\ntags: #{' #'.join(note['tags'])}" if note.get("tags") else "")
                    + f"\n\n{note['content']}")

        if action in ("edit", "update", "append"):
            note = _resolve(str(p.get("id") or p.get("title") or ""))
            if not note:
                return "I could not find that note to edit, sir."
            data = _load()
            for n in data["notes"]:
                if n["id"] != note["id"]:
                    continue
                if p.get("content"):
                    n["content"] = (n["content"] + "\n" + str(p["content"])) \
                        if action == "append" else str(p["content"])
                if p.get("title"):
                    n["title"] = str(p["title"])[:200]
                n["updated"] = _now()
            if not _save(data):
                return "Sir, I could not write the change to the notes file."
            _reindex_full()
            return f"Updated {note['id']}, sir."

        if action in ("tag", "untag", "add_tag", "remove_tag"):
            note = _resolve(str(p.get("id") or p.get("title") or ""))
            if not note:
                return "Which note, sir?"
            raw = str(p.get("tag") or p.get("tags") or "").strip()
            if not raw:
                return "Which tag, sir?"
            wanted = [t.strip() for t in raw.split(",") if t.strip()]
            data = _load()
            for n in data["notes"]:
                if n["id"] != note["id"]:
                    continue
                tags = list(n.get("tags", []))
                for t in wanted:
                    if action in ("tag", "add_tag") and t not in tags:
                        tags.append(t)
                    elif action in ("untag", "remove_tag"):
                        tags = [x for x in tags if x != t]
                n["tags"] = tags[:20]
                n["updated"] = _now()
            _save(data)
            _reindex_full()
            verb = "Tagged" if action in ("tag", "add_tag") else "Untagged"
            return f"{verb} {note['id']}, sir: #{' #'.join(wanted)}"

        if action in ("pin", "remember_forever"):
            note = _resolve(str(p.get("id") or p.get("title") or ""))
            if not note:
                return "Which note should I keep in memory, sir?"
            try:
                from memory import memory_manager
                memory_manager.remember(f"note_{note['id']}", note["title"], "notes")
            except Exception as e:
                return f"Sir, I could not reach the memory system: {str(e)[:90]}"
            data = _load()
            for n in data["notes"]:
                if n["id"] == note["id"]:
                    n["pinned"] = True
            _save(data)
            return (f"Pinned, sir — '{note['title']}' is now in the assistant's memory too, so "
                    f"it will be remembered without being searched for.")

        if action in ("delete", "remove", "forget"):
            note = _resolve(str(p.get("id") or p.get("title") or ""))
            if not note:
                return "I could not find that note, sir — nothing was deleted."

            def _do_delete() -> str:
                data = _load()
                data["notes"] = [n for n in data.get("notes", []) if n["id"] != note["id"]]
                if not _save(data):
                    return "the notes file could not be written, so nothing was deleted"
                sem = _sibling("semantic_search")
                if sem is not None and hasattr(sem, "remove_source"):
                    try:
                        sem.remove_source(f"note:{note['id']}")
                    except Exception:
                        pass
                return f"Deleted '{note['title']}' ({note['id']})"

            try:
                from core import confirm
                answer = confirm.request(key=f"note_delete_{note['id']}",
                                         title=f"Delete the note '{note['title']}'?",
                                         detail=(note.get("content") or "")[:300],
                                         run=_do_delete)
                if "[CONFIRMATION_PENDING]" in answer:
                    return answer
                # confirm.py's own refusal sentence already ends with "so I have
                # not done it" — appending "Nothing was deleted." made the reply
                # read as two denials of the same thing.
                return f"Sir, {answer}"
            except Exception:
                return (f"Sir, I could not put a confirmation on screen, so I have NOT deleted "
                        f"'{note['title']}'. It is still there.")

        if action in ("templates", "template"):
            lines = [f"  {name:<10} {body.splitlines()[0] if body else '(empty)'}"
                     for name, body in TEMPLATES.items()]
            return "Templates I can start a note with:\n" + "\n".join(lines)

        if action in ("tags", "categories"):
            notes = _load().get("notes", [])
            counts: dict[str, int] = {}
            for n in notes:
                for t in n.get("tags", []):
                    counts[t] = counts.get(t, 0) + 1
            if not counts:
                return "You have not tagged anything yet, sir."
            return "Your tags:\n" + "\n".join(f"  #{t} ({c})" for t, c in
                                              sorted(counts.items(), key=lambda x: -x[1]))

        if action in ("status", "info", "stats"):
            notes = _load().get("notes", [])
            path = store_path()
            sem = _sibling("semantic_search")
            search = "available" if sem is not None else "not installed"
            return (f"Notes      : {len(notes)}\n"
                    f"Pinned     : {sum(1 for n in notes if n.get('pinned'))}\n"
                    f"File       : {path}\n"
                    f"Searchable : {search}\n"
                    f"Deleting   : only with your confirmation on screen, always")

        return (f"Sir, '{action}' is not a note action I have. I can add, find, list, show, "
                f"edit, tag, pin, delete, or show the templates.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the note manager hit a problem: {str(e)[:150]}"


# ── self-test: python note_manager.py ────────────────────────────────────────

def _selftest() -> int:
    print("note_manager — self-test")
    problems = 0

    probe = Path(__file__).resolve().parent / "_notes_probe.json"
    global _cfg
    real_cfg = _cfg
    _cfg = lambda: {**real_cfg(), "store_path": str(probe)}
    try:
        print(f"  empty store         : {run({'action': 'list'})}")
        problems += 0 if "no notes yet" in run({"action": "list"}) else 1

        r1 = add_note("Water plant", "The new plant opens on 14 March 2026 in Mansoura.",
                      ["water", "projects"], "work")
        r2 = add_note("مشتريات", "أشتري زيت للسيارة من البنزينة", ["سيارة"])
        print(f"  add two notes       : {r1['ok']} and {r2['ok']} (ids {r1['note']['id']}, {r2['note']['id']})")
        problems += 0 if r1["ok"] and r2["ok"] else 1

        found = find_notes("water", limit=3)
        print(f"  find 'water'        : {len(found)} hit(s) → {found[0]['title'] if found else '—'} "
              f"(by {found[0].get('how') if found else '—'})")
        problems += 0 if found and found[0]["title"] == "Water plant" else 1

        ar = find_notes("زيت", limit=3)
        print(f"  find 'زيت' (Arabic)  : {len(ar)} hit(s) → {ar[0]['title'] if ar else '—'}")
        problems += 0 if ar else 1

        nothing = find_notes("xylophone submarine", limit=3)
        print(f"  no match is honest  : {len(nothing)} hit(s) for nonsense")
        problems += 0 if not nothing else 1

        print(f"  tag                 : {run({'action': 'tag', 'title': 'Water plant', 'tag': 'urgent'})}")
        note = _resolve(r1["note"]["id"])
        problems += 0 if "urgent" in (note or {}).get("tags", []) else 1

        print(f"  edit                : {run({'action': 'edit', 'id': r1['note']['id'], 'content': 'Revised text'})}")
        problems += 0 if _resolve(r1["note"]["id"])["content"] == "Revised text" else 1

        print(f"  template            : {run({'action': 'add', 'title': 'Standup', 'template': 'meeting'})[:56]}")
        problems += 0 if "Attendees" in (_resolve('Standup') or {}).get("content", "") else 1

        # deletion always goes through the gate — with no interface it must refuse
        before = len(_load()["notes"])
        answer = run({"action": "delete", "id": r2["note"]["id"]})
        after = len(_load()["notes"])
        print(f"  delete w/o UI       : refused={before == after} — {answer[:66]}")
        problems += 0 if before == after else 1
        # The count is the real proof (the note is still in the file). The wording
        # must merely be a refusal — confirm.py says "so I have not done it", and
        # this file used to append "Nothing was deleted." on top of that, which
        # read as two denials of one thing. Any honest refusal passes here; a
        # sentence claiming success would not.
        refused = any(w in answer.lower() for w in
                      ("not deleted", "nothing was deleted", "have not done it", "not been done",
                       "cannot confirm"))
        problems += 0 if refused and "deleted 'x'" not in answer.lower() else 1

        print(f"  pin → memory        : {run({'action': 'pin', 'id': r1['note']['id']})[:70]}")
        print(f"  missing note        : {run({'action': 'delete', 'id': 'nope999'})}")
        problems += 0 if "could not find" in run({"action": "delete", "id": "nope999"}) else 1
        print(f"  status              : {run({'action': 'status'}).splitlines()[0]}")
        print(f"  bad action          : {run({'action': 'sing'})[:70]}")
        problems += 0 if "not a note action" in run({"action": "sing"}) else 1
    finally:
        _cfg = real_cfg
        # leave the shared search index exactly as we found it
        sem = _sibling("semantic_search")
        if sem is not None and hasattr(sem, "remove_source"):
            for nid in (r1["note"]["id"], r2["note"]["id"]):
                try:
                    sem.remove_source(f"note:{nid}")
                except Exception:
                    pass
            for extra in _load().get("notes", []):
                try:
                    sem.remove_source(f"note:{extra['id']}")
                except Exception:
                    pass
        probe.unlink(missing_ok=True)

    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
