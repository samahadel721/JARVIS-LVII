"""
sms_control.py — real SMS, through a provider you explicitly choose.

THE HONEST PART FIRST
    There is no free cloud provider for SMS. Every one of them charges per
    message, and any plugin claiming otherwise is either lying or routing
    through something you did not authorise. What IS free is using the phone in
    your pocket: a macro on the phone sends the message from your own SIM and
    costs whatever your plan already costs — usually nothing extra.

    So this plugin ships three providers and refuses to guess between them:

    macrodroid  A free Android app. You build a two-step macro (webhook trigger
                → send SMS) and paste its URL here. Your phone sends the message.
                Free, and nothing passes through a third party's billing.
    webhook     Any other gateway you already run, with a token you supply.
    twilio      The paid, official route, if you happen to have an account.

    With no provider selected, nothing sends and the plugin says so. It never
    falls back to a default provider, because the wrong provider means the
    message goes out from a number you did not intend — or costs money you did
    not agree to.

SETTINGS (Plugin Manager → SMS)
    provider        none | macrodroid | webhook | twilio
    webhook_url     the macro's URL (macrodroid or webhook)
    webhook_token   sent when your gateway requires one
    twilio_sid / twilio_token / twilio_from   only for the twilio provider
    default_to      used when no recipient is given
    hourly_limit    hard cap, default 20 — a runaway loop cannot become 200 texts

SECURITY
    Every credential comes from the settings store. Nothing is logged, nothing
    is echoed back to the model, and the phone numbers in the history file are
    stored as given but never printed in full inside log lines.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "sms_control",
    "description": (
        "Sends a real SMS from the user's own phone or a configured gateway: 'text mama "
        "that I'm outside', 'send an SMS to 01012345678 saying I'll be late'. Only works "
        "after a provider is chosen in the settings — it never sends through an unset "
        "provider. Do NOT use for WhatsApp (use whatsapp_control) or Telegram."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "send | status | history | providers"},
            "to": {"type": "STRING", "description": "Phone number or saved contact name"},
            "message": {"type": "STRING", "description": "The text of the SMS"},
            "count": {"type": "INTEGER", "description": "How many recent messages to show"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "sms_control",
    "title": "SMS / الرسائل النصية",
    "fields": [
        {"key": "provider", "type": "choice", "label": "Provider / المزوّد",
         "default": "none", "choices": ["none", "macrodroid", "webhook", "twilio"]},
        {"key": "webhook_url", "type": "text", "label": "Webhook URL / رابط الويب هوك",
         "default": "", "placeholder": "https://trigger.macrodroid.com/..."},
        {"key": "webhook_token", "type": "password", "label": "Webhook token (إن وُجد)",
         "default": ""},
        {"key": "twilio_sid", "type": "text", "label": "Twilio SID", "default": ""},
        {"key": "twilio_token", "type": "password", "label": "Twilio token", "default": ""},
        {"key": "twilio_from", "type": "text", "label": "Twilio sender number",
         "default": "", "placeholder": "+1..."},
        {"key": "default_to", "type": "text", "label": "Default recipient / المستلم الافتراضي",
         "default": ""},
        {"key": "hourly_limit", "type": "text", "label": "Hourly cap / الحد في الساعة",
         "default": "20"},
        {"key": "confirm_send", "type": "toggle", "label": "Ask before sending / اسألني",
         "default": True},
    ],
}

BASE = Path(__file__).resolve().parent.parent
HISTORY = BASE / "config" / "sms_history.json"
DIGITS = re.compile(r"\D+")


# ── settings, secrets, limits ─────────────────────────────────────────────────

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
    for key in ("webhook_token", "twilio_token", "twilio_sid"):
        v = str(cfg.get(key) or "")
        if v and v in txt:
            txt = txt.replace(v, "<hidden>")
    return txt


def _normalise(number: str) -> str:
    """Keep the leading + if present; strip everything else that is not a digit."""
    raw = str(number or "").strip()
    plus = raw.startswith("+") or raw.startswith("00")
    digits = DIGITS.sub("", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    cc = DIGITS.sub("", str(_cfg().get("country_code") or "20")) or "20"
    if cc == "20" and digits.startswith("0") and len(digits) == 11:
        digits = "20" + digits[1:]
    return ("+" if plus else "") + digits


def _mask(number: str) -> str:
    """A phone number safe to put in a log line."""
    n = str(number or "")
    return n[:5] + "…" + n[-3:] if len(n) > 9 else "…"


def _history_read() -> list[dict]:
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
    except Exception:
        return []


def _history_write(rows: list[dict]) -> None:
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(json.dumps(rows[-200:], ensure_ascii=False, indent=2),
                           encoding="utf-8")
    except Exception:
        pass


def _rate_ok() -> tuple[bool, str]:
    """A hard hourly cap. Protects against a loop, not against the user."""
    try:
        limit = max(1, min(500, int(str(_cfg().get("hourly_limit") or 20).strip())))
    except Exception:
        limit = 20
    cutoff = time.time() - 3600
    recent = [r for r in _history_read() if float(r.get("ts", 0)) >= cutoff]
    if len(recent) >= limit:
        return False, (f"the hourly cap of {limit} messages is reached — I will not send more "
                       f"until the hour rolls over")
    return True, ""


# ── providers ─────────────────────────────────────────────────────────────────

def _post_json(url: str, payload: dict, headers: dict | None = None,
               form: bool = False) -> tuple[bool, str]:
    try:
        import requests
    except Exception:
        return False, "the requests library is missing — run python setup.py once"
    try:
        if form:
            r = requests.post(url, data=payload, headers=headers or {}, timeout=30)
        else:
            r = requests.post(url, json=payload, headers=headers or {}, timeout=30)
    except Exception as e:
        return False, f"could not reach the gateway: {_redact(str(e))[:100]}"
    if r.status_code >= 400:
        return False, f"the gateway answered HTTP {r.status_code}: {_redact(r.text)[:120]}"
    return True, ""


def _send_macrodroid(to: str, text: str) -> tuple[bool, str]:
    cfg = _cfg()
    url = str(cfg.get("webhook_url") or "").strip()
    if not url:
        return False, "the MacroDroid webhook URL is empty in the settings"
    payload = {"number": to, "message": text, "to": to, "text": text}
    token = str(cfg.get("webhook_token") or "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return _post_json(url, payload, headers, form=bool(token))


def _send_webhook(to: str, text: str) -> tuple[bool, str]:
    cfg = _cfg()
    url = str(cfg.get("webhook_url") or "").strip()
    if not url:
        return False, "the webhook URL is empty in the settings"
    token = str(cfg.get("webhook_token") or "").strip()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"} \
        if token else {}
    return _post_json(url, {"to": to, "message": text}, headers)


def _send_twilio(to: str, text: str) -> tuple[bool, str]:
    cfg = _cfg()
    sid = str(cfg.get("twilio_sid") or "").strip()
    token = str(cfg.get("twilio_token") or "").strip()
    sender = str(cfg.get("twilio_from") or "").strip()
    if not (sid and token and sender):
        return False, "the Twilio SID, token and sender number are not all set"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    ok, err = _post_json(url, {"To": to, "From": sender, "Body": text},
                         headers={}, form=True)
    if not ok:
        return False, err
    return True, ""


def _provider_ready() -> tuple[bool, str]:
    """
    Is a provider actually configured? Asked BEFORE the confirmation gate: there
    is no sense putting "send this SMS?" on screen when nothing could send it,
    because the user would confirm and then watch it fail.
    """
    cfg = _cfg()
    provider = str(cfg.get("provider") or "none").strip().lower()
    if provider == "none":
        return False, ("no SMS provider is selected — and I will not guess. Pick one in "
                       "the plugin settings: MacroDroid (free, sends from your own phone), a "
                       "webhook, or Twilio (paid).")
    if provider in ("macrodroid", "webhook"):
        if not str(cfg.get("webhook_url") or "").strip():
            return False, (f"the {provider} provider is selected but its webhook URL is still "
                           f"empty in the settings")
        return True, ""
    if provider == "twilio":
        missing = [k for k in ("twilio_sid", "twilio_token", "twilio_from")
                   if not str(cfg.get(k) or "").strip()]
        if missing:
            return False, f"the Twilio provider is selected but {', '.join(missing)} is empty"
        return True, ""
    return False, f"'{provider}' is not a provider I know"


def _provider_send(to: str, text: str) -> tuple[bool, str]:
    provider = str(_cfg().get("provider") or "none").strip().lower()
    if provider == "none":
        return False, ("no SMS provider is selected — and I will not guess. Pick one in "
                       "the plugin settings: MacroDroid (free, sends from your own phone), a "
                       "webhook, or Twilio (paid).")
    if provider == "macrodroid":
        return _send_macrodroid(to, text)
    if provider == "webhook":
        return _send_webhook(to, text)
    if provider == "twilio":
        return _send_twilio(to, text)
    return False, f"'{provider}' is not a provider I know"


# ── actions ───────────────────────────────────────────────────────────────────

def _resolve(receiver: str) -> str:
    raw = str(receiver or "").strip()
    if not raw:
        return str(_cfg().get("default_to") or "").strip()
    if DIGITS.search(raw):
        return raw
    try:
        import importlib.util
        path = Path(__file__).resolve().parent / "contact_manager.py"
        if path.exists():
            spec = importlib.util.spec_from_file_location("_cm_sms", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            hit = mod.find_one(raw)
            if hit and hit.get("phone"):
                return str(hit["phone"])
    except Exception:
        pass
    return raw


def _do_send(to_raw: str, to: str, text: str) -> str:
    ok, why = _rate_ok()
    if not ok:
        return f"Sir, {why}."
    sent, failure = _provider_send(to, text)
    rows = _history_read()
    rows.append({"ts": time.time(), "to": _mask(to), "text": text[:80],
                 "provider": _cfg().get("provider"), "sent": bool(sent),
                 "error": "" if sent else str(failure)[:120]})
    _history_write(rows)
    if not sent:
        return f"Sir, the SMS was NOT sent: {failure}."
    return f"SMS sent to {to_raw}, sir."


def _action_send(parameters: dict, player) -> str:
    to_raw = _resolve(parameters.get("to", ""))
    text = str(parameters.get("message") or "").strip()
    if not to_raw:
        return "What number should I text, sir?"
    if not text:
        return "What should the SMS say, sir?"
    to = _normalise(to_raw)
    if len(DIGITS.sub("", to)) < 8:
        return f"Sir, '{to_raw}' does not look like a phone number — I have not sent anything."
    if len(text) > 1600:
        text = text[:1597] + "..."
    ready, why = _provider_ready()
    if not ready:
        return f"Sir, {why}."
    if _cfg().get("confirm_send", True):
        try:
            from core import confirm
            answer = confirm.request(key="sms_send", title=f"Send SMS to {to_raw}?",
                                     detail=text[:400], run=lambda: _do_send(to_raw, to, text))
            if "[CONFIRMATION_PENDING]" in answer:
                return answer
            return f"{answer} Nothing has been sent."
        except Exception:
            # Fail CLOSED. The line used to be `return _do_send(...)` — so if the
            # gate itself broke, the SMS went out anyway. That is a fail-open on
            # an action that cannot be taken back, which is the one direction a
            # gate must never fail in. Nothing is sent unless the user was
            # actually asked.
            return ("Sir, I could not put a confirmation on screen, so I have NOT sent it — "
                    "nothing went out. Ask me again when the interface is up.")
    return _do_send(to_raw, to, text)


def _action_history(parameters: dict, player) -> str:
    try:
        count = max(1, min(50, int(str(parameters.get("count") or 10).strip())))
    except Exception:
        count = 10
    rows = _history_read()[-count:]
    if not rows:
        return "I have not sent any SMS, sir."
    lines = []
    for r in rows:
        when = time.strftime("%d %b %H:%M", time.localtime(float(r.get("ts", 0))))
        mark = "sent" if r.get("sent") else f"FAILED ({r.get('error', '')[:40]})"
        lines.append(f"{when} · {r.get('to')} · {mark} · {(r.get('text') or '')[:50]}")
    text = "\n".join(reversed(lines))
    if player:
        try:
            player.show_content("SMS history", text)
        except Exception:
            pass
    return f"Last {len(lines)} SMS attempt(s), sir:\n{text}"


def _action_providers(parameters: dict, player) -> str:
    text = (
        "macrodroid — FREE. Install MacroDroid on your Android phone, make a macro with a\n"
        "             webhook trigger and a 'Send SMS' action, then paste the trigger URL\n"
        "             here. The message leaves from your own SIM.\n"
        "webhook    — any gateway you already run; needs its URL and, if it uses one, a token.\n"
        "twilio     — official and paid: a per-message fee plus the number rental.\n"
        "none       — nothing sends; the plugin stays inert."
    )
    if player:
        try:
            player.show_content("SMS providers", text)
        except Exception:
            pass
    return text


def _action_status(parameters: dict, player) -> str:
    cfg = _cfg()
    provider = str(cfg.get("provider") or "none")
    configured = {
        "macrodroid": bool(str(cfg.get("webhook_url") or "").strip()),
        "webhook": bool(str(cfg.get("webhook_url") or "").strip()),
        "twilio": all(str(cfg.get(k) or "").strip()
                      for k in ("twilio_sid", "twilio_token", "twilio_from")),
        "none": False,
    }.get(provider, False)
    ok, why = _rate_ok()
    lines = [
        f"Provider : {provider}",
        f"Ready    : {'yes' if configured else 'no — ' + ('not selected' if provider == 'none' else 'missing details in the settings')}",
        f"Cap      : {'ok' if ok else why}",
        f"Confirm  : {'on' if cfg.get('confirm_send', True) else 'off'}",
        f"Sent     : {sum(1 for r in _history_read() if r.get('sent'))} message(s) in history",
    ]
    text = "\n".join(lines)
    if player:
        try:
            player.show_content("SMS status", text)
        except Exception:
            pass
    return text


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one SMS action. Never raises."""
    try:
        action = str((parameters or {}).get("action") or "status").strip().lower()
        table = {
            "send": _action_send, "text": _action_send, "sms": _action_send,
            "history": _action_history, "log": _action_history,
            "providers": _action_providers, "help": _action_providers,
            "status": _action_status, "test": _action_status,
        }
        fn = table.get(action)
        if fn is None:
            return (f"Sir, '{action}' is not an SMS action I have. I can send, show history, "
                    f"list providers, or report status.")
        return fn(parameters or {}, player) or "Done, sir."
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the SMS plugin hit a problem: {_redact(str(e))[:150]}"


# ── self-test: python sms_control.py ────────────────────────────────────────

def _selftest() -> int:
    print("sms_control — self-test")
    problems = 0
    for raw in ("01012345678", "+20 101 234 5678", "00201012345678"):
        got = _normalise(raw)
        print(f"  {raw!r:20} → {got!r:16} masked {_mask(got)!r}")
        if not got.endswith("1012345678"):
            problems += 1
    ready, why = _provider_ready()
    print(f"  provider none ready? : {ready}  ({why[:70]})")
    problems += 0 if not ready else 1
    answer = run({"action": "send", "to": "01012345678", "message": "x"})
    print(f"  send with no provider-> {answer[:95]}")
    if "no SMS provider is selected" not in answer:
        problems += 1
    print(f"  bad number           -> {run({'action': 'send', 'to': 'abc', 'message': 'x'})[:70]}")
    print(f"  status               -> {run({'action': 'status'}).splitlines()[0]}")
    rows = _history_read()
    print(f"  history read         : {len(rows)} row(s)")

    # ── the gate must fail CLOSED ─────────────────────────────────────────────
    # A broken gate must never become a sent message. This test replaces the
    # gate with one that raises, and with one that parks the request, and checks
    # in both cases that the send function was never reached. The line this
    # guards used to be `except Exception: return _do_send(...)`, which sent the
    # SMS precisely when nobody had been asked to confirm it.
    import sys as _sys
    import types as _types
    real_cfg, real_ready, real_send = _cfg, _provider_ready, _do_send
    calls: list = []
    globals()["_cfg"] = lambda: {**real_cfg(), "confirm_send": True}
    globals()["_provider_ready"] = lambda: (True, "")
    globals()["_do_send"] = lambda *a, **k: (calls.append(a) or "SENT")
    try:
        import core as _core
        saved_confirm = getattr(_core, "confirm", None)
        try:
            def _raiser(**kwargs):
                raise RuntimeError("gate broke")
            _core.confirm = _types.SimpleNamespace(request=_raiser)
            _sys.modules["core.confirm"] = _core.confirm
            answer = run({"action": "send", "to": "01012345678", "message": "x"})
            print(f"  gate raises          : sent={bool(calls)} — {answer.splitlines()[0][:56]}")
            problems += 0 if not calls and "NOT sent" in answer else 1

            _core.confirm = _types.SimpleNamespace(
                request=lambda **kw: "[CONFIRMATION_PENDING] waiting")
            _sys.modules["core.confirm"] = _core.confirm
            answer2 = run({"action": "send", "to": "01012345678", "message": "x"})
            print(f"  gate parks request   : sent={bool(calls)} (waiting on the banner)")
            problems += 0 if not calls and "[CONFIRMATION_PENDING]" in answer2 else 1
        finally:
            if saved_confirm is not None:
                _core.confirm = saved_confirm
            _sys.modules.pop("core.confirm", None)
    except ImportError:
        print("  gate fail-closed     : core not importable here — skipped")
    finally:
        globals()["_cfg"], globals()["_provider_ready"], globals()["_do_send"] = \
            real_cfg, real_ready, real_send

    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
