"""
telegram_control.py — send, read and search Telegram messages.

WHY THIS IS SEPARATE FROM telegram_remote
    telegram_remote.py already exists and is a REMOTE CONTROL channel: it takes
    typed orders from your phone and drives this computer. This plugin is the
    opposite direction — it is a MESSAGING tool: send a message to a contact,
    read what came in, search, reply to a specific message.

    Both talk to the same Bot API, and that is a real hazard: Telegram keeps ONE
    update cursor per bot. If two processes long-poll getUpdates at the same
    time, whoever reads first consumes the update and the other never sees it.
    So this plugin CHECKS for telegram_remote and refuses to poll while it is
    installed — rather than silently stealing its messages. Sending is always
    safe and is never blocked by that check.

SETTINGS (Plugin Manager → Telegram)
    bot_token       from @BotFather. Stored like every other secret here.
    default_chat    where "tell Ahmed" goes when no chat id is given.
    allowed_chats   comma-separated ids this plugin may send to. Empty = any.
    max_results     how many messages "inbox" reports (clamped 1-50).

    Telegram bot tokens look like 123456789:AA... — they are redacted from every
    log line this file writes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "telegram_control",
    "description": (
        "Sends and reads Telegram messages: 'tell Ahmed on Telegram that I'm late', "
        "'send this photo to the family group on Telegram', 'what did I miss on "
        "Telegram', 'reply to that message'. Also searches previous Telegram "
        "conversations. Do NOT use this to control the computer from your phone — "
        "that is telegram_remote. Do NOT use it for WhatsApp (use whatsapp_control) "
        "or email (use email_control)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "send | send_file | inbox | search | reply | status | chats",
            },
            "to": {"type": "STRING",
                   "description": "Chat id or @username to send to (or a saved contact name)"},
            "message": {"type": "STRING", "description": "The text to send"},
            "file": {"type": "STRING", "description": "Absolute path of a photo or document"},
            "query": {"type": "STRING", "description": "Text to search for in cached history"},
            "message_id": {"type": "INTEGER", "description": "Message id to reply to"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "telegram_control",
    "title": "Telegram / تيليجرام",
    "fields": [
        {"key": "bot_token", "type": "password", "label": "Bot token / توكن البوت",
         "default": "", "placeholder": "123456789:AA..."},
        {"key": "default_chat", "type": "text", "label": "Default chat id / المحادثة الافتراضية",
         "default": "", "placeholder": "123456789 or @my_channel"},
        {"key": "allowed_chats", "type": "text", "label": "Allowed chat ids / المسموح بها (فارغ = الكل)",
         "default": "", "placeholder": "123456789, -1001234567890"},
        {"key": "max_results", "type": "text", "label": "Messages read at once / عدد الرسائل",
         "default": "10"},
    ],
}

API = "https://api.telegram.org/bot{token}/{method}"
CACHE_DIR = Path(__file__).resolve().parent.parent / "config" / ".plugin_cache" / "telegram"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ── settings & safety ─────────────────────────────────────────────────────────

def _cfg() -> dict[str, Any]:
    """Declared defaults, overlaid with whatever the user saved in the app."""
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(PLUGIN_SETTINGS["namespace"]) or {}
        out.update({k: v for k, v in stored.items() if v not in (None, "")})
    except Exception:
        pass
    return out


def _redact(text: str) -> str:
    """Never let a bot token reach a log line, a file, or the model's context."""
    token = str(_cfg().get("bot_token") or "")
    txt = str(text)
    if token and token in txt:
        txt = txt.replace(token, "<token>")
    # shape-based safety net for a token pasted into an error message
    import re
    return re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b", "<token>", txt)


def _split_ids(raw: str) -> set[str]:
    return {p.strip() for p in str(raw or "").replace(";", ",").split(",") if p.strip()}


def _num(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(value).strip())))
    except Exception:
        return default


def _requests():
    try:
        import requests
        return requests
    except Exception:
        return None


def _call(method: str, payload: dict | None = None, files: dict | None = None,
          timeout: int = 20) -> tuple[bool, Any, str]:
    """One Bot API call. Returns (ok, json, spoken_failure). Never raises."""
    token = str(_cfg().get("bot_token") or "").strip()
    if not token:
        return False, None, (
            "Telegram is not set up yet, sir. Open the plugin settings and paste a bot "
            "token from @BotFather — it takes about a minute and costs nothing."
        )
    req = _requests()
    if req is None:
        return False, None, "Sir, the requests library is missing — run python setup.py once."
    url = API.format(token=token, method=method)
    try:
        if files:
            r = req.post(url, data=payload or {}, files=files, timeout=timeout)
        else:
            r = req.post(url, json=payload or {}, timeout=timeout)
    except Exception as e:
        return False, None, f"Sir, Telegram is unreachable: {_redact(str(e))[:120]}"
    try:
        body = r.json()
    except Exception:
        return False, None, f"Sir, Telegram answered with something unreadable (HTTP {r.status_code})."
    if not body.get("ok"):
        desc = str(body.get("description") or f"HTTP {r.status_code}")
        if "chat not found" in desc.lower():
            return False, body, ("That chat id is not one this bot can see, sir. Message the bot "
                                 "once from that account, then try again.")
        if "unauthorized" in desc.lower():
            return False, body, "Telegram rejected the bot token, sir — please paste it again."
        return False, body, f"Telegram refused that: {_redact(desc)[:140]}"
    return True, body.get("result"), ""


def _remote_is_running() -> bool:
    """True when telegram_remote is installed — it owns the single update cursor."""
    try:
        here = Path(__file__).resolve().parent
        return (here / "telegram_remote.py").exists()
    except Exception:
        return False


# ── history cache (so 'search' has something to search) ───────────────────────

def _cache_file(chat_id: str) -> Path:
    safe = "".join(c for c in str(chat_id) if c.isalnum() or c in "-_")[:40] or "unknown"
    return CACHE_DIR / f"{safe}.json"


def _remember(chat_id: str, messages: list[dict]) -> None:
    """Append new messages to the local cache, newest last, capped at 400."""
    try:
        path = _cache_file(chat_id)
        old: list[dict] = []
        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8"))
        seen = {m.get("message_id") for m in old}
        for m in messages:
            if m.get("message_id") not in seen:
                old.append(m)
        path.write_text(json.dumps(old[-400:], ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _recall(chat_id: str | None = None) -> list[dict]:
    try:
        if chat_id:
            p = _cache_file(chat_id)
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        out: list[dict] = []
        for p in CACHE_DIR.glob("*.json"):
            out.extend(json.loads(p.read_text(encoding="utf-8")))
        return out
    except Exception:
        return []


# ── actions ───────────────────────────────────────────────────────────────────

def _resolve_target(raw: str) -> str:
    """A chat id, an @username — or a saved contact name (contact_manager)."""
    target = str(raw or "").strip()
    if not target:
        return str(_cfg().get("default_chat") or "").strip()
    if target.startswith("@") or target.lstrip("-").isdigit():
        return target
    try:
        import importlib.util
        path = Path(__file__).resolve().parent / "contact_manager.py"
        if path.exists():
            spec = importlib.util.spec_from_file_location("_cm_lookup", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            hit = mod.find_one(target)
            if hit and hit.get("telegram"):
                return str(hit["telegram"])
    except Exception:
        pass
    return target


def _guard_target(chat_id: str) -> str:
    allowed = _split_ids(_cfg().get("allowed_chats", ""))
    if allowed and chat_id not in allowed:
        return (f"Sir, {chat_id} is not on the allowed list for Telegram. Add it in the "
                f"plugin settings first — that list is what stops a mistyped id from "
                f"reaching a stranger.")
    return ""


def _action_send(parameters: dict, player) -> str:
    chat_id = _resolve_target(parameters.get("to", ""))
    text = str(parameters.get("message") or "").strip()
    if not chat_id:
        return "Who should I send that to, sir? Give me a chat id or set a default chat."
    if not text:
        return "What should I say, sir?"
    bad = _guard_target(chat_id)
    if bad:
        return bad
    if len(text) > 4096:                       # Telegram's hard per-message limit
        text = text[:4093] + "..."
    ok, res, err = _call("sendMessage", {"chat_id": chat_id, "text": text})
    if not ok:
        return err
    if player:
        try:
            player.write_log(f"JARVIS: Telegram → {chat_id}: {text[:60]}")
        except Exception:
            pass
    return f"Sent on Telegram to {chat_id}, sir."


def _action_send_file(parameters: dict, player) -> str:
    chat_id = _resolve_target(parameters.get("to", ""))
    raw = str(parameters.get("file") or "").strip().strip('"')
    if not chat_id:
        return "Where should it go, sir?"
    if not raw:
        return "Which file, sir?"
    bad = _guard_target(chat_id)
    if bad:
        return bad
    path = Path(raw).expanduser()
    if not path.exists() or not path.is_file():
        return f"Sir, I cannot find {raw}."
    size = path.stat().st_size
    if size > 50 * 1024 * 1024:
        return (f"That file is {size/1024/1024:.0f} MB and Telegram's bot limit is 50 MB, "
                f"sir — I have not sent it.")
    photos = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    is_photo = path.suffix.lower() in photos and size < 10 * 1024 * 1024
    method = "sendPhoto" if is_photo else "sendDocument"
    field = "photo" if is_photo else "document"
    try:
        with path.open("rb") as fh:
            ok, res, err = _call(method, {"chat_id": chat_id},
                                 files={field: (path.name, fh)}, timeout=120)
    except Exception as e:
        return f"Sir, I could not read that file: {_redact(str(e))[:100]}"
    if not ok:
        return err
    return f"Sent {path.name} on Telegram to {chat_id}, sir."


def _action_inbox(parameters: dict, player) -> str:
    if _remote_is_running():
        cached = _recall()
        if cached:
            return _format_inbox(sorted(cached, key=lambda m: m.get("date", 0))[-10:], player)
        return ("Telegram remote is installed and owns the update stream, sir, so I will not "
                "poll and steal its messages. Ask telegram_remote for what you missed, or "
                "remove that plugin if you want this one to read incoming messages instead.")
    limit = _num(_cfg().get("max_results"), 10, 1, 50)
    ok, res, err = _call("getUpdates", {"limit": limit, "timeout": 0})
    if not ok:
        return err
    msgs: list[dict] = []
    if isinstance(res, list):
        for upd in res:
            for key in ("message", "edited_message", "channel_post"):
                m = upd.get(key)
                if isinstance(m, dict):
                    msgs.append(_slim(m))
    if not msgs:
        return "Nothing new on Telegram, sir."
    for chat in {m["chat_id"] for m in msgs}:
        _remember(chat, [m for m in msgs if m["chat_id"] == chat])
    return _format_inbox(msgs[-limit:], player)


def _slim(m: dict) -> dict:
    chat = m.get("chat") or {}
    return {
        "message_id": m.get("message_id"),
        "chat_id": str(chat.get("id") or ""),
        "from": (chat.get("title") or chat.get("username")
                 or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
                 or str(chat.get("id") or "?")),
        "text": str(m.get("text") or m.get("caption") or "").strip(),
        "date": int(m.get("date") or 0),
    }


def _format_inbox(msgs: list[dict], player) -> str:
    lines = []
    for m in msgs:
        when = time.strftime("%d %b %H:%M", time.localtime(m.get("date") or time.time()))
        who = m.get("from", "?")
        body = (m.get("text") or "(no text)")[:90]
        lines.append(f"{when} · {who} · id {m.get('message_id')}: {body}")
    text = "\n".join(lines)
    if player:
        try:
            player.show_content("Telegram", text)
        except Exception:
            pass
    return f"{len(msgs)} Telegram message(s), sir:\n{text}"


def _action_search(parameters: dict, player) -> str:
    needle = str(parameters.get("query") or "").strip().lower()
    if not needle:
        return "What should I look for, sir?"
    rows = [m for m in _recall() if needle in (m.get("text") or "").lower()
            or needle in (m.get("from") or "").lower()]
    if not rows:
        return (f"I found nothing for '{needle}', sir. Telegram's Bot API cannot search "
                f"history by itself — I can only search what I have seen since you "
                f"installed me. That is a limit of the API, not a bug.")
    rows.sort(key=lambda m: m.get("date", 0), reverse=True)
    shown = rows[:10]
    text = "\n".join(
        f"{time.strftime('%d %b %H:%M', time.localtime(m.get('date') or 0))} · "
        f"{m.get('from')} · id {m.get('message_id')}: {(m.get('text') or '')[:90]}"
        for m in shown)
    if player:
        try:
            player.show_content(f"Telegram — {needle}", text)
        except Exception:
            pass
    return f"{len(rows)} match(es) for '{needle}', sir:\n{text}"


def _action_reply(parameters: dict, player) -> str:
    chat_id = _resolve_target(parameters.get("to", ""))
    mid = parameters.get("message_id")
    text = str(parameters.get("message") or "").strip()
    if not chat_id or mid in (None, "") or not text:
        return "I need a chat, the message id and the text to reply with, sir."
    bad = _guard_target(chat_id)
    if bad:
        return bad
    try:
        mid_int = int(str(mid).strip())
    except Exception:
        return "That message id does not look like a number, sir."
    ok, res, err = _call("sendMessage", {"chat_id": chat_id, "text": text,
                                         "reply_to_message_id": mid_int,
                                         "allow_sending_without_reply": True})
    if not ok:
        return err
    return f"Replied on Telegram, sir."


def _action_chats(parameters: dict, player) -> str:
    seen: dict[str, str] = {}
    for m in _recall():
        if m.get("chat_id"):
            seen[m["chat_id"]] = m.get("from") or "?"
    for m in _cached_names():
        seen.setdefault(m[0], m[1])
    if not seen:
        return "I have no Telegram chats yet, sir — nothing has come in since I was installed."
    text = "\n".join(f"{cid}  →  {name}" for cid, name in seen.items())
    if player:
        try:
            player.show_content("Telegram chats", text)
        except Exception:
            pass
    return f"{len(seen)} chat(s), sir:\n{text}"


def _cached_names() -> list[tuple[str, str]]:
    names = []
    for p in CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data:
                names.append((p.stem, data[-1].get("from") or "?"))
        except Exception:
            pass
    return names


def _action_status(parameters: dict, player) -> str:
    cfg = _cfg()
    token = str(cfg.get("bot_token") or "").strip()
    if not token:
        return ("Telegram is not configured yet, sir. Open the plugin settings and paste a "
                "bot token from @BotFather — free, and takes about a minute.")
    ok, me, err = _call("getMe")
    if not ok:
        return err
    who = (me or {}).get("username") or "?"
    notes = [f"Telegram is connected as @{who}, sir."]
    if _remote_is_running():
        notes.append("telegram_remote is also installed — it owns incoming messages, so I "
                     "will only send, not read.")
    allowed = _split_ids(cfg.get("allowed_chats", ""))
    notes.append(f"Allowed chats: {len(allowed)}." if allowed
                 else "No allow-list set — I will send to any chat id you give me.")
    return " ".join(notes)


# ── entry point ───────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one Telegram action. Never raises."""
    try:
        action = str((parameters or {}).get("action") or "status").strip().lower()
        table = {
            "send": _action_send, "message": _action_send, "text": _action_send,
            "send_file": _action_send_file, "sendfile": _action_send_file,
            "file": _action_send_file, "photo": _action_send_file,
            "inbox": _action_inbox, "read": _action_inbox, "unread": _action_inbox,
            "search": _action_search, "find": _action_search,
            "reply": _action_reply,
            "chats": _action_chats, "list": _action_chats,
            "status": _action_status, "test": _action_status,
        }
        fn = table.get(action)
        if fn is None:
            return (f"Sir, '{action}' is not something I can do on Telegram. I can send, "
                    f"send a file, read the inbox, search, reply, list chats, or report status.")
        return fn(parameters or {}, player) or "Done, sir."
    except Exception as e:                                  # noqa: BLE001
        return f"Sir, the Telegram plugin hit a problem: {_redact(str(e))[:160]}"


# ── self-test: python telegram_control.py ────────────────────────────────────

def _selftest() -> int:
    print("telegram_control — self-test")
    problems = 0
    cfg = _cfg()
    print(f"  settings read        : {'ok' if isinstance(cfg, dict) else 'FAIL'}")
    print(f"  bot token present    : {'yes' if cfg.get('bot_token') else 'no (expected on a fresh install)'}")
    print(f"  default chat         : {cfg.get('default_chat') or '(empty)'}")
    red = _redact("token 123456789:AAFakeTokenValueForTesting1234567890 here")
    print(f"  redaction            : {'ok' if '123456789:AA' not in red else 'FAIL'}")
    problems += 0 if "123456789:AA" not in red else 1
    print(f"  allow-list parsing   : {sorted(_split_ids('1, @two;3'))}")
    print(f"  remote conflict check: {'telegram_remote IS installed' if _remote_is_running() else 'clear'}")
    print(f"  status -> {run({'action': 'status'})[:100]}")
    print(f"  bad action -> {run({'action': 'nonsense'})[:80]}")
    print(f"  send w/o config -> {run({'action': 'send', 'to': '1', 'message': 'x'})[:80]}")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
