"""
plugins/gmail_control.py — read, search and send Gmail from the assistant.

Drop this single file into `plugins/`. Standard library only: imaplib, smtplib
and the email package ship with Python, so this needs no pip install, no Google
Cloud project, no OAuth consent screen and no browser round trip. It talks to
Gmail over IMAP and SMTP directly, the same way a mail client does.

WHY NOT THE GOOGLE API
    The official route needs a Cloud project, a client secret, a redirect URI,
    a consent screen and a refresh token that expires — a twenty-minute setup
    for somebody who only wants to hear "you have three unread messages".
    An app password is a sixteen-character string the user generates once, in
    two clicks, and can revoke at any moment without touching anything else.
    It is also the safer of the two: it is scoped to mail alone, and Google
    shows the last time it was used.

WHAT IT ANSWERS
    How many unread, what is in the inbox, search by sender or subject, read a
    message out, and send one.

SENDING IS GATED
    Everything else here reads. Sending an email cannot be taken back, so it
    goes through core/confirm.py — the CONFIRM banner the model cannot forge by
    writing a parameter. If the app is not running and nobody can be asked, the
    message is not sent.

PRIVACY
    The address and the app password live in the assistant's own config file on
    this machine and nowhere else. The password is never printed, never logged,
    and never included in an error message — `_redact()` exists so that a
    failure cannot leak it into a transcript by accident.
"""

from __future__ import annotations

import datetime as _dt
import email
import email.header
import email.utils
import html
import imaplib
import re
import smtplib
import ssl
from email.message import EmailMessage

PLUGIN = {
    "name": "gmail_control",
    "description": (
        "Reads, searches and sends Gmail: how many unread messages, what is in "
        "the inbox, a message read out, unread from someone, and sending mail. "
        "Use for: 'do I have new email', 'read me my last email', 'any mail "
        "from Ahmed', 'what is unread', 'send an email to X', 'reply that I "
        "will be late'. Do NOT use send_message for email — that one drives "
        "WhatsApp and Telegram."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "'unread' for the count (default), 'inbox' for the latest "
                    "messages, 'search' to filter by sender or subject, 'read' "
                    "to open one message in full, or 'send' to write one."
                ),
            },
            "from": {"type": "STRING", "description": "For 'search': sender name or address."},
            "subject": {"type": "STRING", "description": "For 'search': words in the subject."},
            "query": {"type": "STRING", "description": "For 'search': any text to look for."},
            "count": {"type": "INTEGER", "description": "How many messages (default 5, maximum 20)."},
            "to": {"type": "STRING", "description": "For 'send': the recipient's address."},
            "body": {"type": "STRING", "description": "For 'send': the message text."},
            "index": {
                "type": "INTEGER",
                "description": "For 'read': which of the listed messages, 1 is the newest.",
            },
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "gmail_control",
    "title": "Gmail / البريد الإلكتروني",
    "fields": [
        {"key": "address", "type": "text", "label": "Gmail address / البريد",
         "default": "", "placeholder": "you@gmail.com"},
        {"key": "app_password", "type": "password",
         "label": "App password / كلمة مرور التطبيق",
         "default": "", "placeholder": "16 characters, no spaces"},
        {"key": "preview_chars", "type": "text",
         "label": "Characters read aloud per message / طول المعاينة",
         "default": "400"},
        {"key": "mark_seen", "type": "toggle",
         "label": "Mark as read when I read it / تعليمها مقروءة",
         "default": False},
    ],
}

_IMAP_HOST = "imap.gmail.com"
_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 465
_TIMEOUT = 25

#: Gmail's own label for everything that has not been read.
_UNREAD_QUERY = "UNSEEN"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]{2,}")
_URL_RE = re.compile(r"https?://\S+")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _config() -> dict:
    values = {"address": "", "app_password": "", "preview_chars": "400",
              "mark_seen": False}
    try:
        from memory.config_manager import get_plugin_config
        for key, value in (get_plugin_config("gmail_control") or {}).items():
            if value not in (None, ""):
                values[key] = value
    except Exception:
        pass
    try:
        values["preview_chars"] = max(120, min(4000, int(float(values["preview_chars"]))))
    except (TypeError, ValueError):
        values["preview_chars"] = 400
    return values


def _redact(text: str, config: dict) -> str:
    """
    Never let a password reach a log, a traceback or a spoken answer.

    Some libraries echo the failing command in their exception text, and that
    command contains the password. Anything that formats an error for the user
    goes through here first.
    """
    secret = str(config.get("app_password") or "")
    message = str(text)
    if secret:
        message = message.replace(secret, "••••••••")
        message = message.replace(secret.replace(" ", ""), "••••••••")
    # An app password is 16 characters, usually shown in four groups of four.
    return re.sub(r"\b(?:[A-Za-z0-9]{4}\s?){3}[A-Za-z0-9]{4}\b", "••••••••", message)


def _credentials(config: dict) -> tuple[str, str] | None:
    address = str(config.get("address") or "").strip()
    password = str(config.get("app_password") or "").replace(" ", "").strip()
    if address and password:
        return address, password
    return None


def _setup_help() -> str:
    return (
        "Gmail is not set up yet, sir. Two things and I am ready: open the "
        "plugin settings for Gmail and put in your address and an app password. "
        "To make one: go to myaccount.google.com/apppasswords — it needs "
        "2-step verification switched on first — name it JARVIS, and copy the "
        "16 characters Google shows you. That password opens your mail only, "
        "and you can delete it any time without changing anything else."
    )


# ---------------------------------------------------------------------------
# Decoding — Arabic, base64, quoted-printable and broken charsets
# ---------------------------------------------------------------------------

def _header(value: str) -> str:
    """A MIME header as plain text, whatever encoding the sender used."""
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
    except Exception:
        return str(value)
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            for name in (charset, "utf-8", "latin-1"):
                if not name:
                    continue
                try:
                    out.append(chunk.decode(name, errors="replace"))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            else:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return _WS_RE.sub(" ", "".join(out)).strip()


def _part_text(part: email.message.Message) -> str | None:
    """
    The text of one MIME part, decoded — and this function exists because of a
    bug that read Arabic email out as garbage.

    `get_payload(decode=True)` is the obvious call, and for base64 or
    quoted-printable parts it is exactly right: it hands back bytes, and the
    charset says how to read them. But a message with **no**
    Content-Transfer-Encoding whose payload was already a string — which is how
    every message parsed by `message_from_string` arrives — comes back through
    that same call mangled into literal `\\u062a` escape sequences. The text
    was already correct; asking for it "decoded" was what broke it, and the
    assistant would then read the escapes aloud, character by character.

    So the transfer encoding is checked first. No encoding means the payload is
    already text and is taken as-is.
    """
    encoding = str(part.get("Content-Transfer-Encoding") or "").strip().lower()
    raw = part.get_payload(decode=False)

    if isinstance(raw, str) and encoding in ("", "7bit", "8bit", "binary"):
        return _unescape_survivors(raw)

    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return raw if isinstance(raw, str) else None
    if payload is None:
        return raw if isinstance(raw, str) else None
    if isinstance(payload, str):
        return _unescape_survivors(payload)

    charset = part.get_content_charset() or "utf-8"
    for name in (charset, "utf-8", "cp1256", "latin-1"):
        try:
            return _unescape_survivors(payload.decode(name, errors="replace"))
        except (LookupError, UnicodeDecodeError):
            continue
    return _unescape_survivors(payload.decode("utf-8", errors="replace"))


#: A run of two or more \\uXXXX escapes. Nobody writes that on purpose in an
#: email; when it appears it is text that has been through a bad decode, and it
#: is worth repairing rather than reading out loud as punctuation.
_ESCAPE_RUN = re.compile(r"(?:\\u[0-9a-fA-F]{4}){2,}")


def _unescape_survivors(text: str) -> str:
    """Repair text that already arrived escaped, from a path we do not control."""
    if "\\u" not in text:
        return text

    def repair(match: re.Match) -> str:
        try:
            return match.group(0).encode("ascii").decode("unicode_escape")
        except Exception:
            return match.group(0)          # leave it alone rather than lose it

    return _ESCAPE_RUN.sub(repair, text)


def _body_text(message: email.message.Message, limit: int) -> str:
    """
    The readable part of a message, preferring plain text over HTML.

    Mail arrives as a tree of parts in a dozen shapes, and senders routinely
    mislabel the charset. Walking it and falling back through the pieces is the
    only way to get a usable sentence out of all of them.
    """
    plain, rich = [], []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = str(part.get("Content-Disposition") or "")
        if "attachment" in disposition.lower():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        text = _part_text(part)
        if text is None:
            continue
        if ctype == "text/plain":
            plain.append(text)
        else:
            rich.append(text)

    text = "\n".join(plain).strip()
    if not text and rich:
        joined = "\n".join(rich)
        joined = re.sub(r"<(script|style)\b.*?</\1>", " ", joined, flags=re.S | re.I)
        joined = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", joined, flags=re.I)
        text = html.unescape(_TAG_RE.sub(" ", joined))
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # Quoted history is where a conversation came from, not what it says.
    # `\s*` after the newline because stripping HTML leaves indentation in front
    # of the quote marker, and an anchored pattern would then miss it entirely.
    cut = re.split(
        r"\n\s*(?:On .{0,120}wrote:|"
        r"-{2,}\s*Original Message\s*-{2,}|"
        r"From: .{0,80}\n\s*Sent:|"
        r"_{5,}|"
        # Arabic clients indent the attribution differently: "في ٢٩ سبتمبر، كتب
        # أحمد:" puts the writer's name between the verb and the colon, so an
        # anchored "كتب:" would never match a real reply.
        r"في .{0,80}?كتب.{0,40}?:|"
        r"كتب.{0,40}?:)",
        text, maxsplit=1, flags=re.I)[0]
    return cut.strip()[:limit]


def _message_row(uid: str, message: email.message.Message) -> dict:
    sender_name, sender_addr = email.utils.parseaddr(_header(message.get("From", "")))
    date = ""
    try:
        parsed = email.utils.parsedate_to_datetime(message.get("Date", ""))
        if parsed is not None:
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            date = parsed.strftime("%d %b %H:%M")
    except Exception:
        date = _header(message.get("Date", ""))[:16]
    return {
        "uid": uid,
        "from": sender_name or sender_addr or "unknown sender",
        "address": sender_addr,
        "subject": _header(message.get("Subject", "")) or "(no subject)",
        "date": date,
        "message": message,
    }


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

def _connect(config: dict, readonly: bool = True):
    """A logged-in IMAP connection, or a RuntimeError explaining why not."""
    credentials = _credentials(config)
    if credentials is None:
        raise RuntimeError("NOT_CONFIGURED")
    address, password = credentials
    try:
        client = imaplib.IMAP4_SSL(_IMAP_HOST, 993, timeout=_TIMEOUT)
    except Exception as exc:
        raise RuntimeError(f"I could not reach Gmail at all: {_redact(exc, config)}")
    try:
        client.login(address, password)
    except imaplib.IMAP4.error as exc:
        text = _redact(exc, config).lower()
        if "application-specific password" in text or "invalid credentials" in text \
                or "username and password not accepted" in text:
            raise RuntimeError(
                "Gmail refused the password. The usual reasons are: it is your "
                "normal password rather than an app password, 2-step "
                "verification is switched off, or the app password was deleted. "
                "Make a fresh one at myaccount.google.com/apppasswords.")
        raise RuntimeError(f"Gmail refused the login: {_redact(exc, config)}")
    except Exception as exc:
        raise RuntimeError(f"Gmail refused the login: {_redact(exc, config)}")
    try:
        client.select("INBOX", readonly=readonly)
    except Exception as exc:
        raise RuntimeError(f"I logged in but could not open the inbox: {_redact(exc, config)}")
    return client


def _search_criteria(parameters: dict) -> list[str]:
    """
    Turn what was asked into IMAP search terms.

    IMAP wants its own syntax — `UNSEEN`, `FROM "x"`, `SINCE 1-Jan-2026` — and
    building it here keeps the rest of the file free of string concatenation
    that a stray quote would break.
    """
    criteria: list[str] = []
    sender = str(parameters.get("from") or "").strip()
    subject = str(parameters.get("subject") or "").strip()
    query = str(parameters.get("query") or "").strip()
    if sender:
        criteria += ["FROM", f'"{sender}"']
    if subject:
        criteria += ["SUBJECT", f'"{subject}"']
    if query:
        criteria += ["TEXT", f'"{query}"']
    return criteria or ["ALL"]


def _fetch(client, criteria: list[str], count: int, config: dict) -> list[dict]:
    """The newest `count` messages matching `criteria`, newest first."""
    status, data = client.search(None, *criteria)
    if status != "OK":
        raise RuntimeError("the mailbox search did not go through")
    uids = (data[0] or b"").split()
    if not uids:
        return []
    newest = uids[-count:][::-1]
    rows: list[dict] = []
    for raw in newest:
        uid = raw.decode("ascii", errors="ignore")
        status, payload = client.fetch(raw, "(RFC822)")
        if status != "OK" or not payload:
            continue
        chunk = payload[0]
        if not isinstance(chunk, tuple) or len(chunk) < 2:
            continue
        try:
            message = email.message_from_bytes(chunk[1])
        except Exception:
            continue
        rows.append(_message_row(uid, message))
    return rows


def _unread_count(client) -> int:
    try:
        status, data = client.status("INBOX", "(UNSEEN)")
        if status == "OK" and data and data[0]:
            match = re.search(rb"UNSEEN\s+(\d+)", data[0])
            if match:
                return int(match.group(1))
    except Exception:
        pass
    status, data = client.search(None, _UNREAD_QUERY)
    if status == "OK" and data and data[0]:
        return len(data[0].split())
    return 0


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _list_text(rows: list[dict], config: dict) -> str:
    lines = []
    for number, row in enumerate(rows, 1):
        lines.append(f"{number}. {row['from']} — {row['subject']}  ({row['date']})")
        preview = _body_text(row["message"], config["preview_chars"])
        if preview:
            first = preview.splitlines()[0][:160]
            lines.append(f"   {first}")
    return "\n".join(lines)


def _spoken_list(rows: list[dict], what: str) -> str:
    if not rows:
        return f"Nothing {what}, sir."
    if len(rows) == 1:
        row = rows[0]
        return (f"One message {what}: from {row['from']}, subject "
                f"{row['subject']}, {row['date']}.")
    return (f"{len(rows)} messages {what}. The newest is from {rows[0]['from']} "
            f"about {rows[0]['subject']}. The list is on screen.")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _action_unread(config: dict) -> str:
    client = _connect(config)
    try:
        count = _unread_count(client)
        if count == 0:
            return "Your inbox has nothing unread, sir."
        rows = _fetch(client, [_UNREAD_QUERY], min(5, count), config)
        senders = ", ".join(f"{r['from']}" for r in rows[:3])
        tail = f" — {senders}" if senders else ""
        return (f"{count} unread message{'s' if count != 1 else ''}, sir{tail}.")
    finally:
        _logout(client)


def _action_inbox(config: dict, parameters: dict, player) -> str:
    count = max(1, min(20, int(parameters.get("count") or 5)))
    client = _connect(config)
    try:
        rows = _fetch(client, ["ALL"], count, config)
    finally:
        _logout(client)
    if not rows:
        return "Your inbox is empty, sir."
    text = _list_text(rows, config)
    if player:
        try:
            player.show_content("Inbox", text)
        except Exception:
            pass
    return _spoken_list(rows, "in the inbox") + "\n\n" + text


def _action_search(config: dict, parameters: dict, player) -> str:
    count = max(1, min(20, int(parameters.get("count") or 5)))
    criteria = _search_criteria(parameters)
    client = _connect(config)
    try:
        rows = _fetch(client, criteria, count, config)
    finally:
        _logout(client)

    described = []
    if parameters.get("from"):
        described.append(f"from {parameters['from']}")
    if parameters.get("subject"):
        described.append(f"about {parameters['subject']}")
    if parameters.get("query"):
        described.append(f"mentioning {parameters['query']}")
    what = " ".join(described) or "matching that"

    if not rows:
        return f"I found no email {what}, sir."
    text = _list_text(rows, config)
    if player:
        try:
            player.show_content(f"Mail {what}", text)
        except Exception:
            pass
    return (f"{len(rows)} message{'s' if len(rows) != 1 else ''} {what}, sir. "
            f"The newest is from {rows[0]['from']} about {rows[0]['subject']}."
            + "\n\n" + text)


def _action_read(config: dict, parameters: dict, player) -> str:
    index = max(1, int(parameters.get("index") or 1))
    client = _connect(config, readonly=not bool(config.get("mark_seen")))
    try:
        rows = _fetch(client, ["ALL"], index, config)
        if not rows:
            return "There is nothing in the inbox to read, sir."
        row = rows[0]
        body = _body_text(row["message"], config["preview_chars"] * 4)
        if config.get("mark_seen"):
            try:
                client.store(row["uid"].encode(), "+FLAGS", "\\Seen")
            except Exception:
                pass
    finally:
        _logout(client)

    text = (f"From: {row['from']} <{row['address']}>\n"
            f"Subject: {row['subject']}\nWhen: {row['date']}\n\n{body or '(no text)'}")
    if player:
        try:
            player.show_content("Mail", text)
        except Exception:
            pass
    spoken = f"From {row['from']}, {row['subject']}. "
    if body:
        spoken += body[:config["preview_chars"]].replace("\n", " ")
    else:
        spoken += "It has no readable text — only pictures or attachments."
    return spoken


def _send_now(config: dict, to: str, subject: str, body: str) -> str:
    """The irreversible half, kept separate so the gate wraps exactly this."""
    address, password = _credentials(config)
    message = EmailMessage()
    message["From"] = address
    message["To"] = to
    message["Subject"] = subject or "(no subject)"
    message["Date"] = email.utils.formatdate(localtime=True)
    message.set_content(body or "(no text)")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, timeout=_TIMEOUT, context=context) as server:
        server.login(address, password)
        server.send_message(message)
    return f"Sent to {to}, sir — subject '{message['Subject']}'."


def _action_send(config: dict, parameters: dict, player) -> str:
    to = str(parameters.get("to") or "").strip()
    if not to or "@" not in to:
        return "Who should I send it to, sir? I need a full email address."
    subject = str(parameters.get("subject") or "").strip()
    body = str(parameters.get("body") or "").strip()
    if not body:
        return "What should the message say, sir?"

    def _really_send() -> str:
        try:
            return _send_now(config, to, subject, body)
        except smtplib.SMTPAuthenticationError:
            return ("Gmail refused the login while sending — check the app "
                    "password in the plugin settings, sir.")
        except Exception as exc:
            return f"I could not send it: {_redact(exc, config)}"

    preview = f"To: {to}\nSubject: {subject or '(none)'}\n\n{body[:300]}"
    try:
        from core import confirm
        return confirm.request(key="gmail_send", title="Send this email?",
                               detail=preview, run=_really_send)
    except Exception:
        # No gate available means no permission to do something irreversible.
        return ("I have the message ready, sir, but sending needs your "
                "confirmation and the confirmation banner is not available "
                "right now. Run the assistant normally and ask again:\n" + preview)


def _logout(client) -> None:
    try:
        client.close()
    except Exception:
        pass
    try:
        client.logout()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    try:
        config = _config()
        if _credentials(config) is None:
            return _setup_help()

        action = str(parameters.get("action", "unread") or "unread").strip().lower()
        if action == "inbox":
            result = _action_inbox(config, parameters, player)
        elif action == "search":
            result = _action_search(config, parameters, player)
        elif action == "read":
            result = _action_read(config, parameters, player)
        elif action == "send":
            result = _action_send(config, parameters, player)
        else:
            result = _action_unread(config)

    except RuntimeError as exc:
        text = _redact(exc, _config())
        if text == "NOT_CONFIGURED":
            return _setup_help()
        return f"Sir, {text}"
    except Exception as exc:
        return f"Sir, gmail_control failed: {_redact(exc, _config())}"

    if player:
        try:
            player.write_log(f"JARVIS: {str(result).splitlines()[0][:120]}")
        except Exception:
            pass
    return result


if __name__ == "__main__":
    import sys
    # Without settings this prints the two-minute setup instructions, which is
    # exactly what somebody running the file by hand needs to see.
    print(run({"action": sys.argv[1] if len(sys.argv) > 1 else "unread"}))
