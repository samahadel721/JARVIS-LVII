"""
whatsapp_control.py — WhatsApp messaging that does not lie about sending.

THE POINT OF THIS PLUGIN
    The core Send Message action already tries WhatsApp, but when no verified
    driver is present it types the message blind and reports success without
    ever looking at the screen. This plugin supplies the verified driver that
    the project expects (plugins/_whatsapp_core.py), so that path becomes real:
    the conversation is checked to be the right person's before anything is
    typed, and "sent" is only claimed after the input box is confirmed empty.

TWO WAYS IT CAN WORK — whichever you configure
    1. Official Cloud API (works on any OS, needs a Meta app + token).
       Free tier covers 1,000 user-initiated conversations per month.
    2. WhatsApp Desktop on this Windows machine (no account setup at all —
       it drives the app you are already signed into).

    Neither is a bypass: one is the documented API, the other automates your own
    logged-in app. This plugin contains no session extraction, no QR capture and
    no reading of WhatsApp's encrypted store.

SETTINGS (Plugin Manager → WhatsApp)
    api_token        Cloud API token. Leave empty to use the desktop app.
    phone_number_id  Cloud API sender id. Both are needed for transport 1.
    country_code     used to turn 01012345678 into an international number.

GRACEFUL BY DESIGN
    WhatsApp is genuinely harder to integrate than Telegram, and this plugin
    says which half is missing instead of failing at the last step. Unsupported
    capabilities return an explicit reason, never a silent success.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "whatsapp_control",
    "description": (
        "Sends WhatsApp messages: 'send Ahmed a WhatsApp saying I'm on my way', "
        "'whatsapp mama that I arrived', 'can you message this number on WhatsApp'. "
        "Uses the official Cloud API when configured, otherwise the WhatsApp Desktop "
        "app on this machine. Do NOT use for Telegram (use telegram_control), SMS "
        "(use sms_control) or email (use email_control)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "send | send_file | check | status | capabilities"},
            "to": {"type": "STRING",
                   "description": "Contact name, phone number, or saved contact"},
            "message": {"type": "STRING", "description": "The text to send"},
            "file": {"type": "STRING", "description": "Absolute path of a file to send"},
            "caption": {"type": "STRING", "description": "Caption for a file"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "whatsapp_control",
    "title": "WhatsApp / واتساب",
    "fields": [
        {"key": "api_token", "type": "password",
         "label": "Cloud API token / توكن الواجهة الرسمية (اتركه فارغًا لاستخدام التطبيق)",
         "default": "", "placeholder": "EAAG..."},
        {"key": "phone_number_id", "type": "text",
         "label": "Cloud API phone number id / معرّف الرقم",
         "default": "", "placeholder": "123456789012345"},
        {"key": "country_code", "type": "text",
         "label": "Default country code / كود الدولة",
         "default": "20", "placeholder": "20"},
        {"key": "confirm_send", "type": "toggle",
         "label": "Ask before sending / اسألني قبل الإرسال",
         "default": True},
    ],
}

_HERE = Path(__file__).resolve().parent


def _core():
    """The transport layer. Imported lazily so a broken driver never stops load."""
    try:
        spec = importlib.util.spec_from_file_location("_whatsapp_core_local",
                                                      _HERE / "_whatsapp_core.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(PLUGIN_SETTINGS["namespace"]) or {}
        out.update({k: v for k, v in stored.items() if v not in (None, "")})
    except Exception:
        pass
    return out


def _redact(text: str) -> str:
    cfg = _cfg()
    txt = str(text)
    for key in ("api_token", "phone_number_id"):
        v = str(cfg.get(key) or "")
        if v and v in txt:
            txt = txt.replace(v, "<hidden>")
    return txt


def _resolve(receiver: str) -> str:
    """A phone number, or a saved contact name from contact_manager."""
    raw = str(receiver or "").strip()
    if not raw:
        return ""
    if any(c.isdigit() for c in raw):
        return raw
    try:
        path = _HERE / "contact_manager.py"
        if path.exists():
            spec = importlib.util.spec_from_file_location("_cm_wa", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            hit = mod.find_one(raw)
            if hit and hit.get("whatsapp"):
                return str(hit["whatsapp"])
            if hit and hit.get("phone"):
                return str(hit["phone"])
    except Exception:
        pass
    return raw


def _confirm(title: str, detail: str, run) -> str:
    """
    Route through the app's own confirmation banner when it is available.

    The exception branch used to be `return run()` — the message went out
    exactly when the gate could not be reached, i.e. precisely when nobody had
    been asked. A gate that fails open is not a gate: if the confirmation cannot
    be put on screen, nothing is sent and the user is told so. (The same mistake
    was found and fixed in sms_control.py; this file was the second and last
    place it appeared.)
    """
    try:
        from core import confirm
        return confirm.request(key="whatsapp_send", title=title, detail=detail, run=run)
    except Exception:
        return ("Sir, I could not put a confirmation on screen, so I have NOT sent it — "
                "nothing went out.")


def _do_send(receiver: str, message: str) -> str:
    core = _core()
    if core is None:
        return "Sir, the WhatsApp transport file is missing from the plugins folder."
    transport, why = core.get()
    if transport is None:
        return (f"I cannot send on WhatsApp yet, sir: {why}. The settings page shows both "
                f"options — a Cloud API token, or just opening WhatsApp Desktop on this "
                f"machine.")
    sent, failure = transport.send_message_to(receiver, message)
    if not sent:
        return f"Sir, that WhatsApp message was NOT sent: {failure}."
    return f"Sent on WhatsApp to {receiver}, sir."


# ── actions ───────────────────────────────────────────────────────────────────

def _action_send(parameters: dict, player) -> str:
    to = _resolve(parameters.get("to", ""))
    text = str(parameters.get("message") or "").strip()
    if not to:
        return "Who should I message on WhatsApp, sir?"
    if not text:
        return "What should the message say, sir?"
    if len(text) > 4096:
        text = text[:4093] + "..."
    if _cfg().get("confirm_send", True):
        return _confirm("Send on WhatsApp",
                        f"To: {to}\n\n{text[:400]}",
                        lambda: _do_send(to, text))
    return _do_send(to, text)


def _action_send_file(parameters: dict, player) -> str:
    core = _core()
    if core is None:
        return "Sir, the WhatsApp transport file is missing."
    transport, why = core.get()
    if transport is None:
        return f"I cannot send files on WhatsApp, sir: {why}"
    if not hasattr(transport, "send_media"):
        return "That WhatsApp transport cannot send files, sir."
    to = _resolve(parameters.get("to", ""))
    path = str(parameters.get("file") or "").strip().strip('"')
    if not to or not path:
        return "I need both a recipient and a file, sir."
    sent, failure = transport.send_media(to, path, str(parameters.get("caption") or ""))
    if not sent:
        return f"Sir, the file was NOT sent on WhatsApp: {failure}."
    return f"Sent {Path(path).name} on WhatsApp, sir."


def _action_check(parameters: dict, player) -> str:
    """Dry run: can this plugin actually reach this person right now?"""
    core = _core()
    if core is None:
        return "Sir, the WhatsApp transport file is missing."
    info = core.available()
    to = _resolve(parameters.get("to", ""))
    lines = [f"Transport: {info.get('transport') or 'none'}"]
    if info.get("reason"):
        lines.append(f"Why not: {info['reason']}")
    if to:
        try:
            number = core.normalise_number(to)
        except Exception:
            number = ""
        num_line = (number if len(number) >= 8
                    else "(that does not look like a phone number)")
        lines.append(f"Recipient {to} → {num_line}")
    text = "\n".join(lines)
    if player:
        try:
            player.show_content("WhatsApp check", text)
        except Exception:
            pass
    return text


def _action_status(parameters: dict, player) -> str:
    core = _core()
    if core is None:
        return "Sir, the WhatsApp transport file is missing from the plugins folder."
    info = core.available()
    if info.get("transport"):
        return f"WhatsApp is ready, sir — {info.get('reason') or info['transport']}."
    return (f"WhatsApp is not set up yet, sir: {info.get('reason')}. You can fix that either "
            f"way — paste a Cloud API token in the settings, or just open WhatsApp Desktop "
            f"on this machine and I will drive it.")


def _action_capabilities(parameters: dict, player) -> str:
    cfg = _cfg()
    cloud = bool(str(cfg.get("api_token") or "").strip()
                 and str(cfg.get("phone_number_id") or "").strip())
    rows = [
        f"Send text      : yes ({'Cloud API' if cloud else 'desktop app'})",
        f"Send files     : {'yes' if cloud else 'only via Cloud API'}",
        "Read incoming  : no — both transports are send-only here. Meta delivers incoming "
        "messages to a webhook URL, which a local plugin cannot host. Use telegram_control "
        "if you want incoming messages read aloud.",
        "Search history : no — the Cloud API exposes no history endpoint, and the desktop "
        "app's store is encrypted by design. This is a real limit, not an oversight.",
    ]
    text = "\n".join(rows)
    if player:
        try:
            player.show_content("WhatsApp capabilities", text)
        except Exception:
            pass
    return text


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one WhatsApp action. Never raises."""
    try:
        action = str((parameters or {}).get("action") or "status").strip().lower()
        table = {
            "send": _action_send, "message": _action_send, "text": _action_send,
            "send_file": _action_send_file, "file": _action_send_file,
            "media": _action_send_file, "photo": _action_send_file,
            "check": _action_check, "test": _action_check, "dry_run": _action_check,
            "status": _action_status,
            "capabilities": _action_capabilities, "help": _action_capabilities,
        }
        fn = table.get(action)
        if fn is None:
            return (f"Sir, '{action}' is not something I can do on WhatsApp. I can send a "
                    f"message, send a file, check the connection, or report status.")
        return fn(parameters or {}, player) or "Done, sir."
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the WhatsApp plugin hit a problem: {_redact(str(e))[:160]}"


# ── self-test: python whatsapp_control.py ────────────────────────────────────

def _selftest() -> int:
    print("whatsapp_control — self-test")
    problems = 0
    core = _core()
    print(f"  transport module found : {'yes' if core else 'NO'}")
    problems += 0 if core else 1
    if core:
        info = core.available()
        for k, v in info.items():
            print(f"  {k:23}: {v}")
        t, why = core.get()
        print(f"  transport              : {getattr(t, 'kind', None) or 'none'} — {why[:70]}")
    print(f"  status   -> {run({'action': 'status'})[:90]}")
    print(f"  bad act  -> {run({'action': 'zzz'})[:70]}")
    sent = run({"action": "check", "to": "01012345678"})
    print(f"  number normalisation   : {[l for l in sent.splitlines() if '→' in l]}")

    # ── the gate must fail CLOSED ─────────────────────────────────────────────
    # A message must never leave because the gate itself broke. This test hands
    # _confirm a gate that raises, and a run() that records being called, and
    # checks the record stays empty.
    import sys as _sys
    import types as _types
    calls: list = []
    sentinel = object()
    real_confirm = _confirm
    try:
        import core as _core_module          # NOT "_core": that name is already a local in this test
        saved = getattr(_core_module, "confirm", sentinel)

        def _raiser(**kwargs):
            raise RuntimeError("gate broke")

        _core_module.confirm = _types.SimpleNamespace(request=_raiser)
        _sys.modules["core.confirm"] = _core_module.confirm
        answer = _confirm("Send?", "detail", lambda: (calls.append(1), "SENT")[1])
        print(f"  gate raises            : sent={bool(calls)} — {answer.splitlines()[0][:52]}")
        problems += 0 if not calls and "NOT sent" in answer else 1

        _core_module.confirm = _types.SimpleNamespace(
            request=lambda **kw: "[CONFIRMATION_PENDING] waiting")
        _sys.modules["core.confirm"] = _core_module.confirm
        answer2 = _confirm("Send?", "detail", lambda: (calls.append(1), "SENT")[1])
        print(f"  gate parks request     : sent={bool(calls)} (waiting on the banner)")
        problems += 0 if not calls and "[CONFIRMATION_PENDING]" in answer2 else 1

        if saved is not sentinel:
            _core_module.confirm = saved
        _sys.modules.pop("core.confirm", None)
    except ImportError:
        print("  gate fail-closed       : core not importable here — skipped")
    finally:
        globals()["_confirm"] = real_confirm

    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
