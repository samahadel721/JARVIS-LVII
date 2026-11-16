"""
universal_messenger.py — one door for every messaging platform.

WHY THIS EXISTS
    With telegram_control, whatsapp_control, email_control and sms_control all
    installed, the model has four near-identical tools to choose between, and
    picking the wrong one is a real failure mode ("send Ahmed a message" — which
    one?). This plugin gives the model a single fallback: say the platform, and
    the routing happens here.

    It does NOT reimplement anything. Each request is handed to the plugin that
    owns that platform, through the same run() the loader calls. If that plugin
    is missing, this says which ones are present instead of pretending.

ROUTING TABLE
    telegram → telegram_control      whatsapp → whatsapp_control
    email    → email_control         sms      → sms_control

    Additions are one line each: drop the platform's plugin file next to this
    one and add it to PLATFORMS. Nothing else changes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "universal_messenger",
    "description": (
        "One tool for sending and reading messages on any platform when the user names "
        "the platform or you are unsure which messaging plugin fits: 'send Ahmed a "
        "message on WhatsApp', 'message mama on Telegram', 'email the office', 'text "
        "this number'. Prefer the dedicated plugin (telegram_control, whatsapp_control, "
        "email_control, sms_control) when the platform is obvious — this routes to it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "send | media | inbox | reply | platforms"},
            "platform": {"type": "STRING",
                         "description": "telegram | whatsapp | email | sms"},
            "recipient": {"type": "STRING",
                          "description": "Name, number, email address or chat id"},
            "message": {"type": "STRING", "description": "The text to send"},
            "file": {"type": "STRING", "description": "Absolute path for attachments"},
            "conversation": {"type": "STRING", "description": "Conversation to read"},
            "message_id": {"type": "STRING", "description": "Message id to reply to"},
        },
        "required": ["action", "platform"],
    },
}

PLUGINS_DIR = Path(__file__).resolve().parent

# platform -> (plugin file, tool name inside that plugin)
PLATFORMS: dict[str, tuple[str, str]] = {
    "telegram": ("telegram_control.py", "telegram_control"),
    "whatsapp": ("whatsapp_control.py", "whatsapp_control"),
    "email":    ("email_control.py",    "email_control"),
    "sms":      ("sms_control.py",      "sms_control"),
}

ALIASES = {
    "tg": "telegram", "telegram_control": "telegram", "telegram_remote": "telegram",
    "wa": "whatsapp", "whatsapp_control": "whatsapp", "wp": "whatsapp",
    "mail": "email", "gmail": "email", "outlook": "email", "email_control": "email",
    "sms_control": "sms", "text": "sms", "message": "sms", "phone": "sms",
}

_MOD_CACHE: dict[str, Any] = {}


def _load(filename: str) -> Any:
    """Import a sibling plugin file by path. Never cached across failures."""
    if filename in _MOD_CACHE:
        return _MOD_CACHE[filename]
    path = PLUGINS_DIR / filename
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(f"_um_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        return None
    _MOD_CACHE[filename] = mod
    return mod


def _canonical(name: str) -> str:
    key = str(name or "").strip().lower().replace(" ", "").replace("-", "")
    return ALIASES.get(key, key)


def _installed() -> dict[str, str]:
    """platform -> plugin file, for the platforms whose plugin is actually here."""
    return {p: f for p, (f, _t) in PLATFORMS.items() if (PLUGINS_DIR / f).exists()}


def _available_lines() -> str:
    have = _installed()
    if not have:
        return ("none of the messaging plugins are installed yet — I would need "
                "telegram_control, whatsapp_control, email_control or sms_control in the "
                "plugins folder")
    return ", ".join(sorted(have))


# ── routing ───────────────────────────────────────────────────────────────────

def _forward(platform: str, arguments: dict, player) -> str:
    """Hand a request to the plugin that owns the platform. Never raises."""
    entry = PLATFORMS.get(platform)
    if entry is None:
        return (f"Sir, I do not have a '{platform}' messenger. The ones I have are: "
                f"{_available_lines()}.")
    filename, tool = entry
    mod = _load(filename)
    if mod is None:
        return (f"Sir, the {platform} plugin ({filename}) is not installed, so I cannot send "
                f"that. Available: {_available_lines()}.")
    runner = getattr(mod, "run", None)
    if not callable(runner):
        return f"Sir, the {tool} plugin is present but has no run() — it may be a helper file."

    # Each plugin declares its own parameter names; translate rather than duplicate.
    payload: dict[str, Any] = {"action": arguments.get("action", "send")}
    for key in ("message", "caption", "file", "query", "subject", "body"):
        if arguments.get(key):
            payload[key] = arguments[key]
    if platform == "email":
        payload.setdefault("to", arguments.get("recipient", ""))
        if arguments.get("subject"):
            payload["subject"] = arguments["subject"]
        if arguments.get("body"):
            payload["body"] = arguments["body"]
        elif arguments.get("message"):
            payload["body"] = arguments["message"]
    else:
        payload.setdefault("to", arguments.get("recipient", ""))
        if arguments.get("message_id"):
            payload["message_id"] = arguments["message_id"]
        if arguments.get("conversation") and not payload.get("to"):
            payload["to"] = arguments["conversation"]

    try:
        return str(runner(payload, player, None)) or f"Done on {platform}, sir."
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the {platform} plugin failed while handling that: {str(e)[:140]}"


def _action_platforms(parameters: dict, player) -> str:
    have = _installed()
    missing = [p for p in PLATFORMS if p not in have]
    lines = [f"{p}: ready ({PLATFORMS[p][0]})" for p in sorted(have)]
    lines += [f"{p}: not installed" for p in sorted(missing)]
    text = "\n".join(lines)
    if player:
        try:
            player.show_content("Messaging platforms", text)
        except Exception:
            pass
    return text


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Route one request. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "send").strip().lower()
        platform = _canonical(p.get("platform", ""))

        if action in {"platforms", "list", "status", "help"}:
            return _action_platforms(p, player)
        if not platform:
            return (f"Which platform, sir? I can route to {_available_lines()}.")
        if action in {"media", "file", "photo", "attachment"}:
            action = "send_file"
        if action in {"inbox", "read", "unread", "messages"}:
            action = "inbox"
        third = {"send", "send_file", "inbox", "reply", "status", "search", "draft",
                 "drafts", "check", "capabilities"}
        if action not in third:
            return (f"Sir, '{action}' is not something I can route. I can send, send media, "
                    f"read an inbox, reply, or list platforms.")
        return _forward(platform, {**p, "action": action}, player)
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the universal messenger failed: {str(e)[:140]}"


# ── self-test: python universal_messenger.py ─────────────────────────────────

def _selftest() -> int:
    print("universal_messenger — self-test")
    problems = 0
    have = _installed()
    print(f"  platforms installed : {sorted(have) or 'none'}")
    print(f"  aliases resolve     : {[_canonical(a) for a in ('tg', 'wa', 'mail', 'text', 'nope')]}")
    if _canonical("tg") != "telegram" or _canonical("nope") != "nope":
        problems += 1
    print(f"  unknown platform    -> {run({'action': 'send', 'platform': 'myspace', 'message': 'x'})[:80]}")
    print(f"  missing plugin      -> {run({'action': 'send', 'platform': 'email', 'message': 'x'})[:80]}")
    print(f"  bad action          -> {run({'action': 'sing', 'platform': 'telegram'})[:70]}")
    print(f"  platforms list      -> {run({'action': 'platforms'}).splitlines()[0][:70]}")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
