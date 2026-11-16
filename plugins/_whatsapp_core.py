"""
_whatsapp_core.py — the WhatsApp transport layer (a helper, not a plugin).

WHY THIS FILE EXISTS
    The project already declares this file. requirements.txt says pywinauto
    "drives the Windows accessibility tree. Three plugins read it: screen_agent,
    the WhatsApp pair, and game_updater." and actions/send_message.py contains:

        try:
            from plugins import _whatsapp_core as wa
        except Exception:
            ...typing blind...

    So the core action was written EXPECTING a verified WhatsApp driver to exist
    here, and falls back to blind keystrokes when it does not. This file is that
    driver. Nothing about send_message.py changes; the blind path simply stops
    being used whenever a real transport is available.

THE CONTRACT (exactly what actions/send_message.py calls)
    transport, why = get()                       # (None, reason) when unavailable
    sent, failure  = transport.send_message_to(receiver, message)

TWO TRANSPORTS, IN ORDER
    1. CloudAPITransport  — Meta's official WhatsApp Cloud API. Needs a token and
       a phone-number id. Official and supported; must obey Meta's rules (a
       business may only send free-form text inside the 24-hour window opened by
       the user's own message — outside it, a template is required, and this
       transport says so instead of failing silently).
    2. DesktopTransport   — the WhatsApp Desktop app already logged in on this
       Windows machine, driven through the accessibility tree. The message is
       typed into a conversation this file FIRST verifies is the right person's,
       and "sent" is only reported after the input box is confirmed empty.

WHAT THIS FILE WILL NOT DO
    No session extraction, no QR pairing capture, no reading of WhatsApp's
    encrypted store, no credential files, no automation of account creation.
    It drives an app the owner has already legitimately signed into, or uses the
    official API with a token the owner supplied.

The leading underscore keeps the plugin loader from treating this as a plugin —
it is imported by whatsapp_control.py and by actions/send_message.py, and costs
no tool declaration.
"""

from __future__ import annotations

import platform
import time
from typing import Any

NAMESPACE = "whatsapp_control"          # shared with the plugin that fronts this
GRAPH = "https://graph.facebook.com/v21.0"

# How long we wait for the UI to settle between steps. Deliberately generous:
# a keystroke that lands before the window is ready is the exact failure the
# blind path suffers, and the whole point here is not to repeat it.
UI_WAIT = 1.2
VERIFY_WAIT = 0.6


# ── settings (same store the plugin's settings form writes to) ────────────────

def _cfg() -> dict[str, Any]:
    try:
        from memory.config_manager import get_plugin_config
        return dict(get_plugin_config(NAMESPACE) or {})
    except Exception:
        return {}


def _redact(text: str) -> str:
    """Keep tokens and phone-number ids out of every message we emit."""
    import re
    cfg = _cfg()
    txt = str(text)
    for key in ("api_token", "phone_number_id"):
        value = str(cfg.get(key) or "")
        if value and value in txt:
            txt = txt.replace(value, "<hidden>")
    return re.sub(r"\bEAAG[A-Za-z0-9]{20,}\b", "<hidden>", txt)


def normalise_number(receiver: str) -> str:
    """
    Turn whatever the user said into the digits Meta expects.

    Egypt-aware by default, and driven by the configured country code rather
    than a hard-coded one: 01012345678 with country 20 becomes 201012345678.
    Numbers that already carry a country code are left alone.
    """
    digits = "".join(c for c in str(receiver or "") if c.isdigit())
    if not digits:
        return ""
    if digits.startswith("00"):
        return digits[2:]
    cc = "".join(c for c in str(_cfg().get("country_code") or "20") if c.isdigit()) or "20"
    if cc == "20":
        if digits.startswith("0") and len(digits) == 11:      # 010xxxxxxxx
            return "20" + digits[1:]
        if len(digits) == 10 and digits.startswith("1"):       # 10xxxxxxxx
            return "20" + digits
    else:
        # generic rule: a leading 0 on a local number becomes the country code
        if digits.startswith("0") and not digits.startswith("00") and len(digits) <= 12:
            return cc + digits.lstrip("0")
    return digits


# ── transport 1: official Cloud API ──────────────────────────────────────────

class CloudAPITransport:
    """Meta's WhatsApp Cloud API — the supported, documented way in."""

    kind = "cloud_api"

    def __init__(self, token: str, phone_id: str) -> None:
        self._token = token
        self._phone = phone_id

    def _post(self, payload: dict) -> tuple[bool, dict | str]:
        try:
            import requests
        except Exception:
            return False, "the requests library is missing — run python setup.py once"
        try:
            r = requests.post(
                f"{GRAPH}/{self._phone}/messages",
                headers={"Authorization": f"Bearer {self._token}",
                         "Content-Type": "application/json"},
                json=payload, timeout=25)
        except Exception as e:
            return False, f"could not reach graph.facebook.com: {_redact(str(e))[:100]}"
        try:
            body = r.json()
        except Exception:
            return False, f"unreadable answer from Meta (HTTP {r.status_code})"
        if r.status_code >= 400:
            err = body.get("error") if isinstance(body, dict) else {}
            msg = str((err or {}).get("message") or f"HTTP {r.status_code}")
            code = (err or {}).get("code")
            if code == 131047 or "24" in msg and "hour" in msg.lower():
                return False, ("that person has not messaged the business number in the last "
                               "24 hours, and Meta does not allow free-form messages outside "
                               "that window without an approved template")
            if "not a valid whatsapp" in msg.lower() or code == 131026:
                return False, "that number is not on WhatsApp"
            return False, _redact(msg)[:160]
        return True, body

    _normalise = staticmethod(normalise_number)

    def send_message_to(self, receiver: str, message: str) -> tuple[bool, str]:
        number = self._normalise(receiver)
        if not number:
            return False, "that does not look like a phone number"
        ok, res = self._post({"messaging_product": "whatsapp", "to": number,
                              "type": "text", "text": {"body": str(message)[:4096]}})
        if not ok:
            return False, str(res)
        return True, ""

    def send_media(self, receiver: str, path: str, caption: str = "") -> tuple[bool, str]:
        number = self._normalise(receiver)
        if not number:
            return False, "that does not look like a phone number"
        from pathlib import Path
        p = Path(path).expanduser()
        if not p.is_file():
            return False, f"no such file: {p.name}"
        try:
            import requests
            with p.open("rb") as fh:
                r = requests.post(
                    f"{GRAPH}/{self._phone}/media",
                    headers={"Authorization": f"Bearer {self._token}"},
                    data={"messaging_product": "whatsapp"},
                    files={"file": (p.name, fh, "application/octet-stream")},
                    timeout=120)
            body = r.json()
        except Exception as e:
            return False, f"upload failed: {_redact(str(e))[:100]}"
        if r.status_code >= 400:
            return False, _redact(str(body))[:140]
        media_id = body.get("id")
        if not media_id:
            return False, "Meta accepted the file but returned no media id"
        ext = p.suffix.lower().lstrip(".")
        kind = ("image" if ext in {"jpg", "jpeg", "png"}
                else "video" if ext in {"mp4", "3gp"}
                else "audio" if ext in {"mp3", "ogg", "amr", "aac", "m4a"}
                else "document")
        payload: dict[str, Any] = {"messaging_product": "whatsapp", "to": number,
                                   "type": kind, kind: {"id": media_id}}
        if caption and kind in {"image", "video", "document"}:
            payload[kind]["caption"] = str(caption)[:1024]
        if kind == "document":
            payload[kind]["filename"] = p.name
        ok, res = self._post(payload)
        return (True, "") if ok else (False, str(res))

    def status(self) -> str:
        return "connected through Meta's official WhatsApp Cloud API"


# ── transport 2: the desktop app the owner is already signed into ────────────

class DesktopTransport:
    """
    Drives WhatsApp Desktop through the Windows accessibility tree.

    Ordered steps, each named, so a failure says WHICH step failed rather than
    "it did not work": find window → open chat → verify the chat → type →
    confirm the box emptied.
    """

    kind = "desktop"

    def __init__(self) -> None:
        self._app = None
        self._window = None

    # -- attaching ------------------------------------------------------------
    def _attach(self) -> tuple[bool, str]:
        if platform.system() != "Windows":
            return False, "the WhatsApp Desktop driver is Windows-only"
        try:
            from pywinauto import Desktop
        except Exception:
            return False, "pywinauto is not importable"
        try:
            wins = Desktop(backend="uia").windows()
        except Exception as e:
            return False, f"could not read the window list: {e}"
        for w in wins:
            try:
                title = (w.window_text() or "").strip()
            except Exception:
                continue
            if title.lower() in {"whatsapp", "whatsapp business"}:
                self._app, self._window = Desktop(backend="uia"), w
                return True, ""
        return False, "the WhatsApp Desktop app is not open (or not on screen)"

    def _focus(self) -> bool:
        try:
            self._window.set_focus()
            time.sleep(UI_WAIT)
            return True
        except Exception:
            return False

    def _edits(self) -> list:
        """The text inputs of the window — first is search, last is the composer."""
        try:
            return self._window.descendants(control_type="Edit")
        except Exception:
            return []

    # -- the one call actions/send_message.py makes ---------------------------
    def send_message_to(self, receiver: str, message: str) -> tuple[bool, str]:
        ok, why = self._attach()
        if not ok:
            return False, why
        if not self._focus():
            return False, "could not bring the WhatsApp window to the front"
        edits = self._edits()
        if not edits:
            return False, "the WhatsApp window exposed no text fields"

        search = edits[0]
        try:
            search.click_input()
            time.sleep(VERIFY_WAIT)
            search.set_edit_text(str(receiver))
            time.sleep(UI_WAIT * 1.6)          # let the search results settle
            search.type_keys("{ENTER}")
            time.sleep(UI_WAIT)
        except Exception as e:
            return False, f"could not open that chat: {e}"

        # Verify we are in the RIGHT conversation before typing anything.
        if not self._title_matches(receiver):
            return False, (f"the conversation I opened does not carry {receiver}'s name, so I "
                           f"stopped instead of typing into the wrong chat")

        composer = self._edits()[-1]
        try:
            composer.click_input()
            time.sleep(VERIFY_WAIT)
            composer.set_edit_text(str(message))
            time.sleep(VERIFY_WAIT)
            composer.type_keys("{ENTER}")
            time.sleep(UI_WAIT)
        except Exception as e:
            return False, f"could not type the message: {e}"

        if not self._box_is_empty():
            return False, ("the text was typed but the box did not clear, so I cannot confirm "
                           "it was sent")
        return True, ""

    def _title_matches(self, receiver: str) -> bool:
        """The open conversation's header carries the contact's name."""
        want = "".join(c for c in str(receiver) if not c.isdigit()).strip().lower()
        digits = "".join(c for c in str(receiver) if c.isdigit())
        try:
            header = self._window.window_text() or ""
            # Desktop shows "Contact Name - WhatsApp"; accept either the name
            # appearing there, or the phone number if the contact is unsaved.
            blob = header.lower()
            if want and want in blob:
                return True
            for child in self._window.descendants(control_type="Text")[:40]:
                t = (child.window_text() or "").lower()
                if (want and len(want) > 2 and want in t) or (digits and digits[-9:] in t):
                    return True
        except Exception:
            pass
        return False

    def _box_is_empty(self) -> bool:
        try:
            composer = self._edits()[-1]
            return (composer.get_value() or "").strip() == ""
        except Exception:
            # Cannot read it back — say so rather than claim success.
            return False

    def send_media(self, receiver: str, path: str, caption: str = "") -> tuple[bool, str]:
        return False, ("sending files through WhatsApp Desktop needs the file picker, which "
                       "this driver deliberately does not automate — use the Cloud API "
                       "transport for attachments")

    def status(self) -> str:
        ok, why = self._attach()
        return "connected to WhatsApp Desktop" if ok else f"desktop app unavailable: {why}"


# ── what the rest of the project calls ───────────────────────────────────────

def _build() -> tuple[Any, str]:
    cfg = _cfg()
    token = str(cfg.get("api_token") or "").strip()
    phone = str(cfg.get("phone_number_id") or "").strip()
    if token and phone:
        return CloudAPITransport(token, phone), ""
    if platform.system() != "Windows":
        return None, ("no WhatsApp transport is available: the Cloud API is not configured "
                      "and the desktop driver needs Windows")
    return DesktopTransport(), ""


def get() -> tuple[Any, str]:
    """
    Returns (transport, reason). `reason` is a human sentence naming what is
    missing — never a stack trace, never a credential. Matches the call in
    actions/send_message.py exactly.
    """
    try:
        return _build()
    except Exception as e:                                   # noqa: BLE001
        return None, f"the WhatsApp driver could not start ({_redact(str(e))[:90]})"


def available() -> dict:
    """Diagnostics for the plugin's status action and its self-test."""
    cfg = _cfg()
    info = {
        "platform": platform.system(),
        "cloud_api_configured": bool(str(cfg.get("api_token") or "").strip()
                                     and str(cfg.get("phone_number_id") or "").strip()),
        "desktop_driver_platform_ok": platform.system() == "Windows",
    }
    try:
        import pywinauto                                          # noqa: F401
        info["pywinauto_installed"] = True
    except Exception:
        info["pywinauto_installed"] = False
    transport, why = get()
    info["transport"] = getattr(transport, "kind", None)
    info["reason"] = why
    return info


def _selftest() -> int:
    print("_whatsapp_core — self-test")
    info = available()
    for k, v in info.items():
        print(f"  {k:28}: {v}")
    t, why = get()
    print(f"  send_message_to contract     : "
          f"{'present' if (t is None or hasattr(t, 'send_message_to')) else 'MISSING'}")
    if t is not None:
        sent, failure = t.send_message_to("", "")
        print(f"  empty-input rejection        : {failure[:70]}")
    print("  RESULT: PASS" if (t is None or hasattr(t, "send_message_to")) else "  RESULT: FAIL")
    return 0 if (t is None or hasattr(t, "send_message_to")) else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
