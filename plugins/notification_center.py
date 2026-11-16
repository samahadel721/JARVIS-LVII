"""
notification_center.py — one place notifications go, so they can be prioritised.

WHY A CENTRE INSTEAD OF JUST LOGGING
    Any plugin can already write a line to the activity log. What none of them
    can do is say "this one matters" — and by the time the fifth plugin logs
    something, the important line and the trivial one look identical.

    This plugin gives every notification a CATEGORY and a PRIORITY, keeps a
    history with a read/unread state, and exposes a two-line API other plugins
    can call:

        from notification_center import publish          # by file path
        publish("Download finished", "ubuntu.iso", category="download",
                priority="low", source="my_plugin")

    Urgent ones are also spoken through the player; the rest wait until asked
    for. Nothing is ever pushed at the user by default — the same rule
    telegram_remote uses for its away alerts.

CATEGORIES   system | message | reminder | download | security | info
PRIORITIES   low | normal | high | urgent

SETTINGS (Plugin Manager → Notifications)
    speak_urgent   say urgent notifications out loud (default on)
    speak_high     say high ones too (default off — the quiet default matters)
    keep           how many to remember (default 200)
    quiet_hours    e.g. "23-7" — no speaking in that window, still recorded
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "notification_center",
    "description": (
        "Shows, queues and prioritises Mark-LV notifications: 'do I have any "
        "notifications', 'what did I miss', 'any urgent alerts', 'clear my "
        "notifications'. Other plugins and background jobs publish into it. Do NOT use it "
        "to send a message to another person — that is telegram_control, whatsapp_control, "
        "email_control or sms_control."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "notify | list | urgent | clear | history | status"},
            "title": {"type": "STRING", "description": "Short headline"},
            "body": {"type": "STRING", "description": "The detail"},
            "category": {"type": "STRING",
                         "description": "system | message | reminder | download | security | info"},
            "priority": {"type": "STRING", "description": "low | normal | high | urgent"},
            "count": {"type": "INTEGER", "description": "How many to show"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "notification_center",
    "title": "Notifications / الإشعارات",
    "fields": [
        {"key": "speak_urgent", "type": "toggle", "label": "Say urgent ones aloud / انطق العاجل",
         "default": True},
        {"key": "speak_high", "type": "toggle", "label": "Say high ones too / انطق المهم أيضًا",
         "default": False},
        {"key": "keep", "type": "text", "label": "How many to remember / عدد المحفوظ",
         "default": "200"},
        {"key": "quiet_hours", "type": "text",
         "label": "Quiet hours / ساعات الهدوء", "default": "23-7",
         "placeholder": "23-7 or empty"},
    ],
}

BASE = Path(__file__).resolve().parent.parent
STORE = BASE / "config" / "notifications" / "notifications.json"

CATEGORIES = ("system", "message", "reminder", "download", "security", "info")
PRIORITIES = ("low", "normal", "high", "urgent")
_ICON = {"system": "⚙", "message": "✉", "reminder": "⏰", "download": "⬇",
         "security": "🔒", "info": "•"}
_URGENT_FIRST = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(PLUGIN_SETTINGS["namespace"]) or {}
        out.update({k: v for k, v in stored.items() if v not in (None, "")})
    except Exception:
        pass
    return out


def _load() -> list[dict]:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(rows: list[dict]) -> bool:
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(STORE.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STORE)
        return True
    except Exception:
        return False


def _in_quiet_hours() -> bool:
    raw = str(_cfg().get("quiet_hours") or "").strip()
    if not raw or "-" not in raw:
        return False
    try:
        start_s, end_s = raw.split("-", 1)
        start, end = int(start_s.strip()), int(end_s.strip())
    except Exception:
        return False
    hour = time.localtime().tm_hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end          # wraps past midnight, e.g. 23-7


# ── the API other plugins call ───────────────────────────────────────────────

def publish(title: str, body: str = "", category: str = "info",
            priority: str = "normal", source: str = "", speak: bool | None = None) -> dict:
    """
    Record a notification. Returns the stored row so a caller can show it too.

    Safe to call from any plugin, at any time, with junk in any field — it
    normalises rather than raises, because a notification system that throws is
    worse than no notification system.
    """
    cat = str(category or "info").strip().lower()
    if cat not in CATEGORIES:
        cat = "info"
    pri = str(priority or "normal").strip().lower()
    if pri not in PRIORITIES:
        pri = "normal"
    row = {
        "id": f"n{int(time.time() * 1000)}",
        "ts": time.time(),
        "title": str(title or "").strip()[:160] or "(untitled)",
        "body": str(body or "").strip()[:2000],
        "category": cat,
        "priority": pri,
        "source": str(source or "").strip()[:60],
        "read": False,
    }
    rows = _load()
    rows.append(row)
    try:
        keep = max(20, min(5000, int(str(_cfg().get("keep") or 200).strip())))
    except Exception:
        keep = 200
    _save(rows[-keep:])

    if speak is None:
        speak = (pri == "urgent" and _cfg().get("speak_urgent", True)) or \
                (pri == "high" and _cfg().get("speak_high", False))
    if speak and not _in_quiet_hours():
        try:
            _say(f"{row['title']}. {row['body']}".strip())
        except Exception:
            pass
    return row


def _say(text: str) -> bool:
    """Ask the running app to speak. Silently a no-op when nothing is listening."""
    try:
        from core import notification_bridge          # pragma: no cover - optional
        if hasattr(notification_bridge, "speak"):
            return bool(notification_bridge.speak(text))
    except Exception:
        pass
    return False


def unread_count() -> int:
    return sum(1 for r in _load() if not r.get("read"))


# ── actions ───────────────────────────────────────────────────────────────────

def _fmt(row: dict) -> str:
    when = time.strftime("%d %b %H:%M", time.localtime(float(row.get("ts", 0))))
    icon = _ICON.get(str(row.get("category")), "•")
    flag = {"urgent": "!!", "high": "!", "normal": "", "low": "·"}.get(
        str(row.get("priority")), "")
    mark = "" if row.get("read") else " •"
    who = f" [{row.get('source')}]" if row.get("source") else ""
    line = f"{icon} {when}{who}{mark} {flag} {row.get('title')}"
    if row.get("body"):
        line += f"\n     {row['body'][:160]}"
    return line


def _action_notify(parameters: dict, player) -> str:
    row = publish(str(parameters.get("title") or ""),
                  str(parameters.get("body") or ""),
                  str(parameters.get("category") or "info"),
                  str(parameters.get("priority") or "normal"),
                  source="jarvis")
    if player:
        try:
            player.write_log(f"SYS: {row['title']}")
        except Exception:
            pass
    return f"Noted, sir: {row['title']}."


def _action_list(parameters: dict, player, only_urgent: bool = False) -> str:
    try:
        count = max(1, min(50, int(str(parameters.get("count") or 10).strip())))
    except Exception:
        count = 10
    rows = _load()
    if only_urgent:
        rows = [r for r in rows if r.get("priority") in ("urgent", "high")]
    unread = [r for r in rows if not r.get("read")]
    pool = unread or rows
    if not pool:
        return ("Nothing urgent, sir." if only_urgent
                else "No notifications, sir.")
    pool.sort(key=lambda r: (_URGENT_FIRST.get(str(r.get("priority")), 9),
                             -float(r.get("ts", 0))))
    shown = pool[:count]
    text = "\n".join(_fmt(r) for r in shown)
    if player:
        try:
            player.show_content("Notifications", text)
        except Exception:
            pass
    head = f"{len(shown)} of {len(pool)}"
    return f"{head} notification(s), sir:\n{text}"


def _action_clear(parameters: dict, player) -> str:
    """Marks everything read. Deliberately not a delete — history is the point."""
    rows = _load()
    changed = 0
    for r in rows:
        if not r.get("read"):
            r["read"] = True
            changed += 1
    if changed and not _save(rows):
        return "Sir, I could not write the notifications file."
    return f"Marked {changed} notification(s) as read, sir." if changed \
        else "There was nothing unread, sir."


def _action_history(parameters: dict, player) -> str:
    rows = _load()
    if not rows:
        return "There is no notification history, sir."
    by_cat: dict[str, int] = {}
    by_pri: dict[str, int] = {}
    for r in rows:
        by_cat[str(r.get("category"))] = by_cat.get(str(r.get("category")), 0) + 1
        by_pri[str(r.get("priority"))] = by_pri.get(str(r.get("priority")), 0) + 1
    lines = [f"Total   : {len(rows)}   unread: {unread_count()}",
             "By type : " + ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items())),
             "By level: " + ", ".join(f"{k}={v}" for k, v in sorted(by_pri.items())),
             "Quiet   : " + ("yes, right now" if _in_quiet_hours() else "no")]
    last = sorted(rows, key=lambda r: -float(r.get("ts", 0)))[:5]
    if last:
        lines.append("Latest  :")
        lines += ["  " + _fmt(r).replace("\n", " ")[:110] for r in last]
    text = "\n".join(lines)
    if player:
        try:
            player.show_content("Notification history", text)
        except Exception:
            pass
    return text


def _action_status(parameters: dict, player) -> str:
    cfg = _cfg()
    lines = [
        f"Stored      : {len(_load())}   unread: {unread_count()}",
        f"Speak urgent: {'on' if cfg.get('speak_urgent', True) else 'off'}",
        f"Speak high  : {'on' if cfg.get('speak_high', False) else 'off'}",
        f"Quiet hours : {cfg.get('quiet_hours') or '(none)'}"
        + (" — active now" if _in_quiet_hours() else ""),
        f"File        : {STORE}",
    ]
    text = "\n".join(lines)
    if player:
        try:
            player.show_content("Notifications status", text)
        except Exception:
            pass
    return text


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one notification action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "list").strip().lower()
        if action in {"notify", "add", "create", "push", "publish"}:
            return _action_notify(p, player)
        if action in {"list", "show", "unread", "what"}:
            return _action_list(p, player)
        if action in {"urgent", "alerts", "important"}:
            return _action_list(p, player, only_urgent=True)
        if action in {"clear", "dismiss", "read", "ack"}:
            return _action_clear(p, player)
        if action in {"history", "stats", "summary"}:
            return _action_history(p, player)
        if action in {"status", "test"}:
            return _action_status(p, player)
        return (f"Sir, '{action}' is not a notification action I have. I can notify, list, "
                f"show urgent ones, clear, show history, or report status.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the notification centre hit a problem: {str(e)[:150]}"


# ── self-test: python notification_center.py ────────────────────────────────

def _selftest() -> int:
    print("notification_center — self-test")
    problems = 0
    backup = STORE.read_text(encoding="utf-8") if STORE.exists() else None
    try:
        for cat, pri, title in (("download", "low", "Download finished"),
                                ("security", "urgent", "Login from a new device"),
                                ("reminder", "high", "Prayer in ten minutes")):
            row = publish(title, "self-test", category=cat, priority=pri, source="selftest")
            print(f"  publish {cat:9} {pri:7} → {row['id']}  icon={_ICON[row['category']]}")
        bad = publish("Junk in, junk out", "x", category="nonsense", priority="nonsense")
        print(f"  normalise bad input  : category={bad['category']} priority={bad['priority']}")
        if bad["category"] != "info" or bad["priority"] != "normal":
            problems += 1
        print(f"  unread count         : {unread_count()}")
        print(f"  urgent list head     : {run({'action': 'urgent'}).splitlines()[0][:70]}")
        print(f"  bad action           -> {run({'action': 'sing'})[:70]}")
        cleared = run({"action": "clear"})
        print(f"  clear                -> {cleared[:60]}")
        if unread_count() != 0:
            problems += 1
            print("  FAIL: unread did not reach zero")
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
