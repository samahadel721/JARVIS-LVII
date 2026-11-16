"""
contact_manager.py — one local address book the other plugins can ask about.

WHY A SEPARATE CONTACTS FILE
    Every messaging plugin needs the same lookup: "mama" is a phone number for
    SMS, a chat id for Telegram, an address for email. Without a shared book,
    each plugin either asks the user every time or invents its own half-list.
    This is the one place that answer lives, and the others read it.

    It is a plain JSON file you own. Nothing is scraped from anywhere: contacts
    arrive because you typed them or because you approved an import of a file
    you chose. Reading someone's phone book without asking is exactly the kind
    of thing this project is supposed to be an alternative to.

STORAGE
    config/contacts/contacts.json — written atomically (a temp file then a
    rename), so an interrupted write cannot leave a half-file behind.

FIELDS
    name, aliases (list), phone, email, telegram, whatsapp, notes
    Aliases are what make it useful by voice: "ماما", "mama", "mum" all resolve
    to one record. Arabic and English spellings sit in the same list.

SETTINGS (Plugin Manager → Contacts)
    ask_before_delete   on by default; deleting goes through the confirm banner
    auto_save_unknown   off by default — a name is never written down because it
                        appeared in a message
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "contact_manager",
    "description": (
        "The user's own address book: 'save Ahmed's number 01012345678', 'what is mama's "
        "number', 'who do I know called Mariam', 'add ahmed@example.com to Ahmed's card'. "
        "Other messaging tools look contacts up here by name, so saving someone once makes "
        "'message Ahmed' work everywhere. Do NOT use it to look up strangers or to import "
        "contacts the user has not approved."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "add | find | list | update | delete | status"},
            "name": {"type": "STRING", "description": "Contact name"},
            "aliases": {"type": "STRING",
                        "description": "Other names this person answers to, comma separated"},
            "phone": {"type": "STRING", "description": "Phone number"},
            "email": {"type": "STRING", "description": "Email address"},
            "telegram": {"type": "STRING", "description": "Telegram chat id or @username"},
            "whatsapp": {"type": "STRING", "description": "WhatsApp number, if different"},
            "notes": {"type": "STRING", "description": "Anything worth remembering"},
            "query": {"type": "STRING", "description": "What to search for"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "contact_manager",
    "title": "Contacts / جهات الاتصال",
    "fields": [
        {"key": "ask_before_delete", "type": "toggle", "label": "Ask before deleting / اسألني قبل الحذف",
         "default": True},
        {"key": "auto_save_unknown", "type": "toggle",
         "label": "Auto-save names seen in messages / حفظ الأسماء تلقائيًا (غير مفضّل)",
         "default": False},
    ],
}

BASE = Path(__file__).resolve().parent.parent
STORE = BASE / "config" / "contacts" / "contacts.json"

FIELDS = ("name", "aliases", "phone", "email", "telegram", "whatsapp", "notes")


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(PLUGIN_SETTINGS["namespace"]) or {}
        out.update({k: v for k, v in stored.items() if v not in (None, "")})
    except Exception:
        pass
    return out


# ── storage ───────────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    try:
        if STORE.exists():
            data = json.loads(STORE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _save(rows: list[dict]) -> bool:
    """Atomic: write a temp file in the same directory, then replace."""
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(STORE.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STORE)
        return True
    except Exception:
        return False


def _norm(text: str) -> str:
    """Loose matching: case, spacing, Arabic diacritics and alef/ya variants."""
    t = str(text or "").strip().lower()
    t = re.sub(r"[\u064B-\u0652\u0640]", "", t)          # harakat + tatweel
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t)


def _aliases(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,;]", str(value or ""))
    return [s.strip() for s in items if str(s).strip()]


# ── lookup used by the other plugins ─────────────────────────────────────────

def find_one(query: str) -> dict | None:
    """
    The function telegram_control, whatsapp_control and sms_control import.
    Exact name first, then alias, then substring — so 'Ahmed' does not match
    'Ahmed's father' before 'Ahmed'.
    """
    q = _norm(query)
    if not q:
        return None
    rows = _load()
    for r in rows:
        if _norm(r.get("name", "")) == q:
            return r
    for r in rows:
        if q in [_norm(a) for a in _aliases(r.get("aliases"))]:
            return r
    for r in rows:
        if q in _norm(r.get("name", "")) or any(q in _norm(a) for a in _aliases(r.get("aliases"))):
            return r
    return None


def find_all(query: str) -> list[dict]:
    q = _norm(query)
    if not q:
        return _load()
    out = []
    for r in _load():
        hay = " ".join([str(r.get("name", "")), str(r.get("notes", "")),
                        str(r.get("phone", "")), str(r.get("email", "")),
                        str(r.get("telegram", "")), str(r.get("whatsapp", "")),
                        " ".join(_aliases(r.get("aliases"))), str(r.get("aliases", ""))])
        if q in _norm(hay):
            out.append(r)
    return out


# ── actions ───────────────────────────────────────────────────────────────────

def _describe(r: dict) -> str:
    bits = [r.get("name") or "?"]
    if r.get("aliases"):
        bits.append(f"({', '.join(_aliases(r['aliases']))})" if isinstance(r["aliases"], list)
                    else f"({r['aliases']})")
    for key, label in (("phone", "☎"), ("email", "✉"), ("telegram", "TG"),
                       ("whatsapp", "WA")):
        if r.get(key):
            bits.append(f"{label} {r[key]}")
    if r.get("notes"):
        bits.append(f"— {r['notes']}")
    return " ".join(bits)


def _action_add(parameters: dict, player) -> str:
    name = str(parameters.get("name") or "").strip()
    if not name:
        return "What is the name, sir?"
    rows = _load()
    existing = find_one(name)
    contact = {
        "name": name,
        "aliases": _aliases(parameters.get("aliases")),
        "phone": str(parameters.get("phone") or "").strip(),
        "email": str(parameters.get("email") or "").strip(),
        "telegram": str(parameters.get("telegram") or "").strip(),
        "whatsapp": str(parameters.get("whatsapp") or "").strip(),
        "notes": str(parameters.get("notes") or "").strip(),
    }
    if existing:
        # Merge rather than duplicate: filling in a missing number is the common case.
        for key in ("aliases",):
            contact[key] = sorted({*_aliases(existing.get(key)), *contact[key]})
        for key in ("phone", "email", "telegram", "whatsapp", "notes"):
            contact[key] = contact[key] or str(existing.get(key) or "")
        rows[rows.index(existing)] = contact
        verb = "Updated"
    else:
        rows.append(contact)
        verb = "Saved"
    if not _save(rows):
        return "Sir, I could not write the contacts file — check that the config folder is writable."
    return f"{verb} {name} — {_describe(contact)}"


def _action_find(parameters: dict, player) -> str:
    query = str(parameters.get("query") or parameters.get("name") or "").strip()
    if not query:
        return "Who am I looking for, sir?"
    hits = find_all(query)
    if not hits:
        return (f"I have nobody called '{query}', sir. Say 'add {query}' with a number and "
                f"I will remember them.")
    text = "\n".join(_describe(r) for r in hits[:12])
    if player:
        try:
            player.show_content(f"Contacts — {query}", text)
        except Exception:
            pass
    return f"{len(hits)} match(es) for '{query}', sir:\n{text}"


def _action_list(parameters: dict, player) -> str:
    rows = _load()
    if not rows:
        return "The address book is empty, sir."
    text = "\n".join(_describe(r) for r in sorted(rows, key=lambda x: _norm(x.get("name", ""))))
    if player:
        try:
            player.show_content(f"Contacts ({len(rows)})", text)
        except Exception:
            pass
    return f"{len(rows)} contact(s), sir:\n{text[:1200]}"


def _action_update(parameters: dict, player) -> str:
    target = str(parameters.get("name") or "").strip()
    hit = find_one(target)
    if not hit:
        return f"Sir, I have nobody called '{target}'. Say 'add' to create them."
    changed = []
    for key in ("phone", "email", "telegram", "whatsapp", "notes"):
        value = str(parameters.get(key) or "").strip()
        if value:
            hit[key] = value
            changed.append(key)
    if parameters.get("aliases"):
        hit["aliases"] = sorted({*_aliases(hit.get("aliases")),
                                 *_aliases(parameters.get("aliases"))})
        changed.append("aliases")
    if not changed:
        return f"Nothing to change on {hit.get('name')}, sir — which field?"
    rows = _load()
    for i, r in enumerate(rows):
        if _norm(r.get("name", "")) == _norm(hit.get("name", "")):
            rows[i] = hit
            break
    if not _save(rows):
        return "Sir, I could not write the contacts file."
    return f"Updated {hit.get('name')}: {', '.join(changed)}."


def _action_delete(parameters: dict, player) -> str:
    target = str(parameters.get("name") or parameters.get("query") or "").strip()
    hit = find_one(target)
    if not hit:
        return f"Sir, I have nobody called '{target}'."

    def _go() -> str:
        rows = [r for r in _load() if _norm(r.get("name", "")) != _norm(hit.get("name", ""))]
        if not _save(rows):
            return "Sir, I could not write the contacts file."
        return f"Deleted {hit.get('name')}, sir."

    if _cfg().get("ask_before_delete", True):
        try:
            from core import confirm
            return confirm.request(key="contact_delete", title="Delete this contact?",
                                   detail=_describe(hit), run=_go)
        except Exception:
            return _go()
    return _go()


def _action_status(parameters: dict, player) -> str:
    rows = _load()
    with_phone = sum(1 for r in rows if r.get("phone"))
    with_tg = sum(1 for r in rows if r.get("telegram"))
    lines = [
        f"Contacts : {len(rows)}",
        f"With phone: {with_phone}   with Telegram: {with_tg}",
        f"File     : {STORE}",
        f"Exists   : {'yes' if STORE.exists() else 'not created yet'}",
        f"Delete   : {'asks first' if _cfg().get('ask_before_delete', True) else 'immediate'}",
    ]
    text = "\n".join(lines)
    if player:
        try:
            player.show_content("Contacts status", text)
        except Exception:
            pass
    return text


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one contacts action. Never raises."""
    try:
        action = str((parameters or {}).get("action") or "list").strip().lower()
        table = {
            "add": _action_add, "save": _action_add, "remember": _action_add,
            "find": _action_find, "search": _action_find, "who": _action_find,
            "list": _action_list, "all": _action_list,
            "update": _action_update, "edit": _action_update, "set": _action_update,
            "delete": _action_delete, "remove": _action_delete, "forget": _action_delete,
            "status": _action_status,
        }
        fn = table.get(action)
        if fn is None:
            return (f"Sir, '{action}' is not something I do with contacts. I can add, find, "
                    f"list, update, delete, or report status.")
        return fn(parameters or {}, player) or "Done, sir."
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the contacts plugin hit a problem: {str(e)[:150]}"


# ── self-test: python contact_manager.py ────────────────────────────────────

def _selftest() -> int:
    print("contact_manager — self-test")
    problems = 0
    backup = STORE.read_text(encoding="utf-8") if STORE.exists() else None
    try:
        demo = {"name": "أحمد المصري", "aliases": ["Ahmed", "أحمد"], "phone": "01012345678",
                "email": "ahmed@example.com", "telegram": "123456", "whatsapp": "",
                "notes": "جار"}
        others = _load()
        others = [r for r in others if _norm(r.get("name", "")) != _norm(demo["name"])]
        _save(others + [demo])
        for probe, expect in (("أحمد", True), ("ahmed", True), ("AHMED", True),
                              ("احمد", True), ("أحمَد", True), ("زينب", False)):
            got = find_one(probe) is not None
            print(f"  find {probe!r:10} → {'found' if got else 'not found':<10}"
                  f"{'ok' if got == expect else 'FAIL'}")
            problems += 0 if got == expect else 1
        print(f"  add action           -> {run({'action': 'add', 'name': 'تجربة', 'phone': '01000000000'})[:60]}")
        print(f"  duplicate merge      -> {run({'action': 'add', 'name': 'أحمد المصري', 'notes': 'جار'})[:70]}")
        print(f"  missing person       -> {run({'action': 'find', 'query': 'لا يوجد'})[:60]}")
        print(f"  status               -> {run({'action': 'status'}).splitlines()[0]}")
    finally:
        if backup is not None:
            STORE.write_text(backup, encoding="utf-8")
        elif STORE.exists():
            STORE.unlink()
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
