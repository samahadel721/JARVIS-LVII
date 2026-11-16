"""
email_control.py — send mail through any provider, with a draft mode and a gate.

HOW THIS DIFFERS FROM gmail_control
    gmail_control is Gmail-only and does two things: reads the inbox over IMAP
    and sends. This one is the general sender: any provider, attachments, and —
    the part that matters for an assistant — a DRAFT MODE. If you keep auto_send
    off, nothing leaves the machine until you have seen the draft and approved
    it. An assistant that can mail your boss on a misheard sentence is not a
    feature; it is a liability.

    The two coexist deliberately. gmail_control answers "do I have mail"; this
    answers "send this, carefully".

SETTINGS (Plugin Manager → Email)
    provider      gmail | outlook | yahoo | custom  (fills the server for you)
    address       your address — also the envelope sender
    password      an APP PASSWORD, never your main account password
    display_name  what recipients see
    smtp_host / smtp_port   only needed for the custom provider
    auto_send     OFF by default. While off, every send is a draft you approve.
    drafts_folder where drafts are kept (default: config/mail_drafts)

APP PASSWORDS
    Gmail, Outlook and Yahoo all refuse plain-password SMTP logins, by design.
    Each issues a 16-character app password that can be revoked on its own
    without touching the real password. The plugin tells you where to get one
    rather than letting you wonder why the login failed.

SECURITY
    Credentials come from the app's own settings store. They are never written
    to a draft file, never echoed into a log line, and never returned to the
    model — the draft JSON holds the recipients and the text, nothing else.
"""

from __future__ import annotations

import json
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "email_control",
    "description": (
        "Sends email through Gmail, Outlook, Yahoo or any SMTP server: 'email the office "
        "that I'll be late', 'send this file to ahmed@example.com', 'write a draft to my "
        "landlord'. Supports attachments, and drafts a message for approval instead of "
        "sending when auto-send is off. Do NOT use it to READ Gmail — that is "
        "gmail_control. Do NOT use it for Telegram or WhatsApp."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "send | draft | drafts | show | send_draft | delete_draft | status"},
            "to": {"type": "STRING", "description": "Recipient address(es), comma separated"},
            "subject": {"type": "STRING", "description": "Subject line"},
            "body": {"type": "STRING", "description": "Message body"},
            "cc": {"type": "STRING", "description": "CC recipients"},
            "attachment": {"type": "STRING", "description": "Absolute path of a file to attach"},
            "draft_id": {"type": "STRING", "description": "Which draft to show or send"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "email_control",
    "title": "Email / البريد الإلكتروني",
    "fields": [
        {"key": "provider", "type": "choice", "label": "Provider / المزوّد",
         "default": "gmail", "choices": ["gmail", "outlook", "yahoo", "custom"]},
        {"key": "address", "type": "text", "label": "Your address / عنوانك",
         "default": "", "placeholder": "you@example.com"},
        {"key": "password", "type": "password", "label": "App password / كلمة مرور التطبيق",
         "default": "", "placeholder": "16 characters"},
        {"key": "display_name", "type": "text", "label": "Display name / الاسم الظاهر",
         "default": ""},
        {"key": "smtp_host", "type": "text", "label": "SMTP host (custom only)",
         "default": "", "placeholder": "smtp.example.com"},
        {"key": "smtp_port", "type": "text", "label": "SMTP port (custom only)",
         "default": "465", "placeholder": "465 or 587"},
        {"key": "auto_send", "type": "toggle",
         "label": "Send without asking / أرسل بلا سؤال (غير مفضّل)", "default": False},
        {"key": "default_to", "type": "text", "label": "Default recipient / المستلم الافتراضي",
         "default": ""},
    ],
}

SERVERS = {
    "gmail":   {"host": "smtp.gmail.com",       "port": 465, "tls": "ssl",
                "help": "create an app password at myaccount.google.com/apppasswords"},
    "outlook": {"host": "smtp-mail.outlook.com", "port": 587, "tls": "starttls",
                "help": "create an app password in your Microsoft account security settings"},
    "yahoo":   {"host": "smtp.mail.yahoo.com",   "port": 465, "tls": "ssl",
                "help": "create an app password in Yahoo Account Security"},
}

BASE = Path(__file__).resolve().parent.parent
DRAFTS = BASE / "config" / "mail_drafts"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


# ── settings ──────────────────────────────────────────────────────────────────

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
    pw = str(_cfg().get("password") or "")
    txt = str(text)
    if pw and pw in txt:
        txt = txt.replace(pw, "<hidden>")
    return txt


def _server() -> tuple[str, int, str, str]:
    """(host, port, tls_mode, help_text) for the configured provider."""
    cfg = _cfg()
    provider = str(cfg.get("provider") or "gmail").strip().lower()
    if provider == "custom":
        host = str(cfg.get("smtp_host") or "").strip()
        try:
            port = int(str(cfg.get("smtp_port") or "465").strip())
        except Exception:
            port = 465
        return host, port, ("ssl" if port == 465 else "starttls"), ""
    entry = SERVERS.get(provider, SERVERS["gmail"])
    return entry["host"], entry["port"], entry["tls"], entry["help"]


# ── drafts ────────────────────────────────────────────────────────────────────

def _draft_path(draft_id: str) -> Path:
    safe = "".join(c for c in str(draft_id) if c.isalnum() or c in "-_") or "draft"
    return DRAFTS / f"{safe}.json"


def _save_draft(to: str, subject: str, body: str, cc: str = "",
                attachment: str = "") -> str:
    DRAFTS.mkdir(parents=True, exist_ok=True)
    did = f"draft-{time.strftime('%Y%m%d-%H%M%S')}"
    payload = {"id": did, "to": to, "cc": cc, "subject": subject, "body": body,
               "attachment": attachment, "created": time.strftime("%Y-%m-%d %H:%M"),
               "sent": False}
    _draft_path(did).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    return did


def _load_draft(draft_id: str) -> dict | None:
    p = _draft_path(draft_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ── sending ───────────────────────────────────────────────────────────────────

def _validate_addresses(raw: str) -> tuple[list[str], str]:
    parts = [a.strip() for a in re.split(r"[,;]", str(raw or "")) if a.strip()]
    bad = [a for a in parts if not EMAIL_RE.match(a)]
    return parts, (", ".join(bad) if bad else "")


def _build_message(cfg: dict, to: list[str], subject: str, body: str,
                   cc: list[str], attachment: str) -> tuple[EmailMessage | None, str]:
    msg = EmailMessage()
    sender = str(cfg.get("address") or "").strip()
    name = str(cfg.get("display_name") or "").strip()
    msg["From"] = f"{name} <{sender}>" if name else sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject or "(no subject)"
    msg.set_content(body or "")

    if attachment:
        path = Path(str(attachment).strip().strip('"')).expanduser()
        if not path.is_file():
            return None, f"I cannot find the attachment {path.name}"
        size = path.stat().st_size
        if size > 25 * 1024 * 1024:
            return None, (f"that attachment is {size/1024/1024:.0f} MB and most providers "
                          f"refuse anything over 25 MB")
        try:
            data = path.read_bytes()
        except Exception as e:
            return None, f"I could not read {path.name}: {e}"
        import mimetypes
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
    return msg, ""


def _deliver(to: list[str], subject: str, body: str, cc: list[str],
             attachment: str) -> str:
    """The actual SMTP conversation. Returns a spoken sentence either way."""
    cfg = _cfg()
    sender = str(cfg.get("address") or "").strip()
    password = str(cfg.get("password") or "").strip()
    if not sender or not password:
        return ("Email is not set up yet, sir. The settings page needs your address and an "
                "app password — not your normal password, the 16-character one your "
                "provider issues for apps.")
    host, port, tls, help_text = _server()
    if not host:
        return ("Sir, the custom email provider is selected but no SMTP host is set in the "
                "settings.")
    msg, problem = _build_message(cfg, to, subject, body, cc, attachment)
    if msg is None:
        return f"Sir, I have not sent it: {problem}."
    try:
        ctx = ssl.create_default_context()
        if tls == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
                s.login(sender, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(sender, password)
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        extra = f" — {help_text}" if help_text else ""
        return (f"The mail server rejected those credentials, sir. That usually means an app "
                f"password is needed rather than the account password{extra}.")
    except smtplib.SMTPRecipientsRefused:
        return f"Sir, the server refused the recipient address {', '.join(to)}."
    except Exception as e:
        low = str(e).lower()
        if "timed out" in low or "unreachable" in low or "getaddrinfo" in low:
            return f"Sir, I could not reach {host} — check the internet connection."
        return f"Sir, sending failed: {_redact(str(e))[:150]}"
    who = ", ".join(to)
    return f"Email sent to {who}, sir."


# ── actions ───────────────────────────────────────────────────────────────────

def _action_send(parameters: dict, player) -> str:
    cfg = _cfg()
    to_raw = str(parameters.get("to") or cfg.get("default_to") or "").strip()
    to, bad = _validate_addresses(to_raw)
    if not to:
        return "Who should I email, sir?"
    if bad:
        return f"Sir, '{bad}' does not look like an email address — I have not sent anything."
    subject = str(parameters.get("subject") or "").strip()
    body = str(parameters.get("body") or parameters.get("message") or "").strip()
    if not body:
        return "What should the email say, sir?"
    cc, bad_cc = _validate_addresses(parameters.get("cc", ""))
    if bad_cc:
        return f"Sir, the CC address '{bad_cc}' is not valid."
    attachment = str(parameters.get("attachment") or parameters.get("file") or "")

    if not cfg.get("auto_send", False):
        did = _save_draft(to_raw, subject, body, parameters.get("cc", ""), attachment)
        preview = f"To: {to_raw}\nSubject: {subject or '(none)'}\n\n{body[:500]}"
        if player:
            try:
                player.show_content("Email draft — awaiting your approval", preview)
            except Exception:
                pass
        # Confirmation banner when the app is running; otherwise the draft stands
        # and nothing is sent — never a silent send.
        try:
            from core import confirm
            answer = confirm.request(
                key="email_send",
                title="Send this email?",
                detail=preview[:600],
                run=lambda: _mark_and_send(did))
            if "[CONFIRMATION_PENDING]" in answer:
                return answer
            # The gate could not be shown (no interface bound). The draft exists;
            # say exactly that, so the user is never left thinking mail went out.
            return (f"Sir, {answer} I have saved it as draft {did} and nothing was sent — "
                    f"say 'send draft {did}' when you have read it.")
        except Exception:
            return (f"I have written it as a draft, sir, and it is NOT sent. Say 'send draft "
                    f"{did}' when you have read it. Preview:\n{preview[:300]}")

    return _deliver(to, subject, body, cc, attachment)


def _mark_and_send(draft_id: str) -> str:
    d = _load_draft(draft_id)
    if not d:
        return f"Sir, I cannot find draft {draft_id}."
    to, bad = _validate_addresses(d.get("to", ""))
    if not to or bad:
        return f"Sir, that draft has no valid recipient ({bad or 'empty'})."
    cc, _ = _validate_addresses(d.get("cc", ""))
    result = _deliver(to, d.get("subject", ""), d.get("body", ""), cc,
                      d.get("attachment", ""))
    if result.startswith("Email sent"):
        d["sent"] = True
        try:
            _draft_path(draft_id).write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
        except Exception:
            pass
    return result


def _action_draft(parameters: dict, player) -> str:
    to_raw = str(parameters.get("to") or _cfg().get("default_to") or "").strip()
    subject = str(parameters.get("subject") or "").strip()
    body = str(parameters.get("body") or parameters.get("message") or "").strip()
    if not to_raw or not body:
        return "I need a recipient and some text to write the draft, sir."
    did = _save_draft(to_raw, subject, body, parameters.get("cc", ""),
                      parameters.get("attachment", ""))
    return f"Draft saved as {did}, sir — nothing has been sent."


def _action_drafts(parameters: dict, player) -> str:
    if not DRAFTS.exists():
        return "There are no drafts, sir."
    rows = []
    for p in sorted(DRAFTS.glob("*.json"), reverse=True)[:15]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            mark = "sent" if d.get("sent") else "unsent"
            rows.append(f"{d.get('id')} · {d.get('created')} · {mark} · to {d.get('to')} · "
                        f"{(d.get('subject') or '(no subject)')[:40]}")
        except Exception:
            continue
    if not rows:
        return "There are no drafts, sir."
    text = "\n".join(rows)
    if player:
        try:
            player.show_content("Email drafts", text)
        except Exception:
            pass
    return f"{len(rows)} draft(s), sir:\n{text}"


def _action_show(parameters: dict, player) -> str:
    d = _load_draft(str(parameters.get("draft_id") or ""))
    if not d:
        return "Sir, I cannot find that draft."
    text = (f"To: {d.get('to')}\nCc: {d.get('cc') or '-'}\nSubject: {d.get('subject') or '-'}\n"
            f"Attachment: {d.get('attachment') or '-'}\n\n{d.get('body')}")
    if player:
        try:
            player.show_content(f"Draft {d.get('id')}", text)
        except Exception:
            pass
    return text[:600]


def _action_send_draft(parameters: dict, player) -> str:
    did = str(parameters.get("draft_id") or "").strip()
    if not did:
        return "Which draft, sir?"
    return _mark_and_send(did)


def _action_delete_draft(parameters: dict, player) -> str:
    did = str(parameters.get("draft_id") or "").strip()
    p = _draft_path(did)
    if not p.exists():
        return "Sir, I cannot find that draft."
    try:
        p.unlink()
    except Exception as e:
        return f"Sir, I could not delete it: {e}"

    def _go() -> str:
        return f"Deleted draft {did}, sir."
    try:
        from core import confirm
        return confirm.request(key="email_delete_draft", title="Delete this draft?",
                               detail=did, run=_go)
    except Exception:
        return _go()


def _action_status(parameters: dict, player) -> str:
    cfg = _cfg()
    host, port, tls, help_text = _server()
    ready = bool(str(cfg.get("address") or "").strip() and str(cfg.get("password") or "").strip())
    lines = [
        f"Provider : {cfg.get('provider')}  ({host or 'no host set'}:{port}, {tls})",
        f"Address  : {cfg.get('address') or '(not set)'}",
        f"Password : {'set' if cfg.get('password') else 'NOT set'}"
        + (f"  ← {help_text}" if not cfg.get("password") and help_text else ""),
        f"Auto-send: {'ON' if cfg.get('auto_send') else 'OFF (drafts only — I will ask)'}",
        f"Drafts   : {len(list(DRAFTS.glob('*.json'))) if DRAFTS.exists() else 0}",
        f"Ready    : {'yes' if ready else 'no'}",
    ]
    text = "\n".join(lines)
    if player:
        try:
            player.show_content("Email status", text)
        except Exception:
            pass
    return text


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one email action. Never raises."""
    try:
        action = str((parameters or {}).get("action") or "status").strip().lower()
        table = {
            "send": _action_send, "email": _action_send, "compose": _action_send,
            "draft": _action_draft, "write": _action_draft,
            "drafts": _action_drafts, "list": _action_drafts,
            "show": _action_show, "read_draft": _action_show,
            "send_draft": _action_send_draft, "approve": _action_send_draft,
            "delete_draft": _action_delete_draft,
            "status": _action_status, "test": _action_status,
        }
        fn = table.get(action)
        if fn is None:
            return (f"Sir, '{action}' is not an email action I have. I can send, draft, list "
                    f"drafts, show one, send one, or report status.")
        return fn(parameters or {}, player) or "Done, sir."
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the email plugin hit a problem: {_redact(str(e))[:150]}"


# ── self-test: python email_control.py ──────────────────────────────────────

def _selftest() -> int:
    print("email_control — self-test")
    problems = 0
    cfg = _cfg()
    host, port, tls, help_text = _server()
    print(f"  provider → server    : {cfg.get('provider')} → {host}:{port} ({tls})")
    print(f"  credential redaction : {_redact('pw=' + (str(cfg.get('password')) or 'x'))}")
    ok, bad = _validate_addresses("a@b.com, bad-address, c@d.org")
    print(f"  address validation   : good={ok} bad={bad!r}")
    if bad != "bad-address":
        problems += 1
    did = _save_draft("someone@example.com", "Test", "This is a draft from the self-test.")
    print(f"  draft written        : {did} → {_draft_path(did).exists()}")
    problems += 0 if _draft_path(did).exists() else 1
    body = _draft_path(did).read_text(encoding="utf-8")
    if str(cfg.get("password") or "zz-no-password") in body:
        print("  FAIL: password leaked into the draft file"); problems += 1
    else:
        print("  no secret in draft   : ok")
    _draft_path(did).unlink(missing_ok=True)
    print(f"  send w/o config      -> {run({'action': 'send', 'to': 'a@b.com', 'body': 'x'})[:90]}")
    print(f"  bad address          -> {run({'action': 'send', 'to': 'nope', 'body': 'x'})[:80]}")
    print(f"  status               -> {run({'action': 'status'}).splitlines()[0][:70]}")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
