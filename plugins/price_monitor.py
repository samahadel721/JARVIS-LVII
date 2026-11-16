"""
price_monitor.py — watching a price, and showing you where it was read from.

THE HONESTY RULE, FIRST
    A price scraped from a page is a guess until a person sees where it came
    from. So every reading carries its evidence: the exact snippet the number was
    pulled out of, and the number of other candidates the page also contained. If
    a page has three prices and no marker to say which one matters, the plugin
    says so instead of quietly reporting the first one. A number is never
    invented, and "I could not find a price" is a real, common answer.

WHAT IT WILL NOT DO
    No CAPTCHA solving, no login, no cookie dance, no pretending to be Chrome so
    a site stops complaining, no proxy rotation. If a page answers 403 or 429,
    that is reported as a refusal, once, and the previous reading stays. Price
    checking is a courtesy to a public page, not an attack on it: one request per
    interval, an honest User-Agent, a timeout.

TWO WAYS TO WATCH
    An API (JSON path like 'data.0.price') is exact and preferred. A public page
    uses a marker: the word or symbol to look near ('Price', 'EGP', '$'), then the
    first number after it. Both are configured by you, explicitly, per item.

SETTINGS (Plugin Manager → Price monitor)
    interval_min  360 — six hours between checks of one item
    max_items     30
    allow_private no
    history       20 — how many past readings each item keeps
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "price_monitor",
    "description": (
        "Watches prices you point it at: 'watch the price of X at <url>', 'set a target of 900 "
        "for X', 'check my prices'. It can read a JSON API exactly or a public page by a marker "
        "word, always tells you the snippet it read the number from, and never works around a "
        "site's blocks."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "watch | unwatch | list | check | history | target | help"},
            "url": {"type": "STRING", "description": "The product page or API address"},
            "name": {"type": "STRING", "description": "What this item is"},
            "marker": {"type": "STRING", "description": "For pages: the word/symbol to read the price near"},
            "path": {"type": "STRING", "description": "For JSON APIs: e.g. data.0.price"},
            "below": {"type": "STRING", "description": "Alert when the price drops below this"},
            "above": {"type": "STRING", "description": "Alert when the price goes above this"},
            "currency": {"type": "STRING", "description": "Optional label, e.g. EGP"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "price_monitor",
    "title": "Price monitor / مراقبة الأسعار",
    "fields": [
        {"key": "interval_min", "type": "text", "label": "Minutes between checks per item",
         "default": "360"},
        {"key": "max_items", "type": "text", "label": "Most items watched", "default": "30"},
        {"key": "history", "type": "text", "label": "Readings kept per item", "default": "20"},
        {"key": "allow_private", "type": "text",
         "label": "Allow local/private addresses (yes/no)", "default": "no"},
        {"key": "notify", "type": "text", "label": "Send alerts to the Notification Center (yes/no)",
         "default": "yes"},
    ],
}

STATE = Path(__file__).resolve().parent.parent / "config" / "price_watch.json"

_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?![\w.])")
_CURRENCIES = {"$": "USD", "€": "EUR", "£": "GBP", "﷼": "SAR", "ج.م": "EGP", "EGP": "EGP",
               "USD": "USD", "EUR": "EUR", "GBP": "GBP", "SAR": "SAR", "AED": "AED",
               "ر.س": "SAR", "د.إ": "AED"}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("price_monitor") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _int_cfg(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(_cfg().get(key) or default))))
    except Exception:
        return default


def _net():
    name = "_jarvis_net_core"
    module = sys.modules.get(name)
    if module is not None:
        return module
    import importlib.util
    path = Path(__file__).resolve().parent / "_net_core.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(rows: list[dict[str, Any]]) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(STATE.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE)
    except Exception:
        pass


def to_price(text: str) -> float | None:
    """'1,299.50' → 1299.5 · '٩٩' (Arabic-Indic digits) → 99 · 'abc' → None."""
    raw = str(text or "").strip()
    if not raw:
        return None
    arabic = "٠١٢٣٤٥٦٧٨٩"
    for index, digit in enumerate(arabic):
        raw = raw.replace(digit, str(index))
    match = _NUMBER.search(raw.replace("\u066b", "."))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "").replace(" ", ""))
    except Exception:
        return None


def extract_price(text: str, marker: str = "") -> dict[str, Any]:
    """
    {found, value, snippet, candidates}. The snippet is the whole point: it is
    what a person checks when a monitoring rule behaves oddly.
    """
    body = str(text or "")
    plain = re.sub(r"(?s)<[^>]+>", " ", body)
    plain = re.sub(r"\s+", " ", plain)

    if marker:
        lowered, marker_low = plain.lower(), str(marker).lower()
        index = lowered.find(marker_low)
        if index < 0:
            return {"found": False, "value": None, "snippet": "",
                    "candidates": 0, "why": f"the marker '{marker}' is not on the page any more"}
        window = plain[index:index + 160]
        match = _NUMBER.search(window[len(marker):])
        if not match:
            return {"found": False, "value": None, "snippet": window.strip(),
                    "candidates": 0, "why": f"the marker '{marker}' was found but no number after it"}
        value = to_price(match.group(1))
        return {"found": value is not None, "value": value,
                "snippet": window.strip()[:160], "candidates": 1}

    # No marker: look for a currency symbol with a number, and say how many other
    # numbers were on the page so the user can judge how lucky the pick was.
    symbols = "|".join(re.escape(s) for s in ("$", "€", "£", "EGP", "USD", "AED", "SAR", "ج.م"))
    matches = list(re.finditer(rf"(?:{symbols})\s*(\d[\d,.\s]*\d|\d)", plain))
    if not matches:
        matches = list(re.finditer(r"(\d[\d,.\s]*\d|\d)\s*(?:" + symbols + r")", plain))
    if not matches:
        return {"found": False, "value": None, "snippet": "", "candidates": 0,
                "why": "no price with a currency symbol was found — give me a marker word to "
                       "look near"}
    first = matches[0]
    value = to_price(first.group(1))
    start = max(0, first.start() - 50)
    return {"found": value is not None, "value": value,
            "snippet": plain[start:first.end() + 40].strip()[:160],
            "candidates": len(matches)}


def _notify(title: str, body: str, priority: str = "normal") -> None:
    if str(_cfg().get("notify") or "yes").lower() not in ("yes", "true", "1"):
        return
    try:
        import importlib.util
        name = "_jarvis_notification_center"
        module = sys.modules.get(name)
        if module is None:
            path = Path(__file__).resolve().parent / "notification_center.py"
            if not path.exists():
                return
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        if hasattr(module, "publish"):
            module.publish(title, body, category="price", priority=priority, source="price")
    except Exception:
        pass


def _find(rows: list[dict], key: str) -> dict | None:
    wanted = str(key or "").strip().lower()
    for row in rows:
        if row.get("name", "").lower() == wanted or row.get("url", "").lower() == wanted:
            return row
    return None


def _fetch_price(row: dict) -> dict[str, Any]:
    net = _net()
    allow_private = str(_cfg().get("allow_private") or "no").lower() in ("yes", "true", "1")
    fetch = net.fetch(row["url"], timeout=25, max_bytes=1_500_000, allow_private=allow_private)
    if not fetch["ok"]:
        return {"ok": False, "why": net.status_sentence(fetch)}
    if int(fetch.get("status") or 0) >= 400:
        return {"ok": False, "why": net.status_sentence(fetch)}

    if str(row.get("path") or "").strip():
        try:
            payload = json.loads(fetch["text"])
        except Exception:
            return {"ok": False, "why": "the address did not return JSON, and this item is "
                                        "configured with a JSON path"}
        value = net.dig(payload, str(row["path"]))
        number = to_price(value) if not isinstance(value, (int, float)) else float(value)
        if number is None:
            return {"ok": False, "why": f"'{row['path']}' is not in the response, or its value "
                                        f"is not a number"}
        return {"ok": True, "value": number,
                "snippet": f"{row['path']} = {value}", "candidates": 1}

    found = extract_price(fetch["text"], str(row.get("marker") or ""))
    if not found["found"]:
        return {"ok": False, "why": found.get("why", "no price found")}
    return {"ok": True, "value": found["value"], "snippet": found["snippet"],
            "candidates": found["candidates"]}


def judge(row: dict, value: float) -> tuple[bool, str]:
    """(alert?, sentence). Targets are what the user set — nothing is implied."""
    below = row.get("below")
    above = row.get("above")
    previous = row.get("last")
    label = f"{value:,.2f}" + (f" {row['currency']}" if row.get("currency") else "")
    if below is not None and value < float(below):
        return True, f"{label} — below your target of {float(below):,.2f}"
    if above is not None and value > float(above):
        return True, f"{label} — above your ceiling of {float(above):,.2f}"
    if previous is not None and float(previous) != value:
        direction = "down" if value < float(previous) else "up"
        change = abs(value - float(previous))
        percent = (change / float(previous) * 100) if float(previous) else 0
        return True, (f"{label} — {direction} from {float(previous):,.2f} "
                      f"({percent:.1f}%)")
    return False, label


def check_one(row: dict, player=None) -> dict[str, Any]:
    outcome = _fetch_price(row)
    row["checked"] = time.time()
    if not outcome["ok"]:
        row["last_error"] = outcome["why"]
        return {"status": "failed", "why": outcome["why"]}
    row.pop("last_error", None)
    value = float(outcome["value"])
    previous = row.get("last")
    history = list(row.get("history") or [])
    history.append({"t": int(time.time()), "v": value})
    row["history"] = history[-_int_cfg("history", 20, 1, 200):]
    row["last"] = value
    alert, sentence = judge({**row, "last": previous}, value)
    row["snippet"] = outcome["snippet"]
    row["candidates"] = outcome["candidates"]
    if alert:
        _notify(f"{row['name']}: {value:,.2f}", sentence + f"\nRead from: {outcome['snippet'][:120]}",
                "normal")
    return {"status": "alert" if alert else "same", "why": sentence, "value": value,
            "snippet": outcome["snippet"], "candidates": outcome["candidates"]}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """One price action. Reads public pages; keeps its own small history."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "check").strip().lower()

        if action in ("help", "?"):
            return ("I watch prices, sir: 'watch the price of <thing> at <url>', optionally with a "
                    "marker word ('marker Price') for pages or a JSON path for APIs, and targets "
                    "('below 900'). Every reading shows the snippet I took the number from — I "
                    "never invent a price, and I never work around a site's blocks.")

        if action in ("watch", "add"):
            net = _net()
            if net is None:
                return "The shared file '_net_core.py' is missing, sir."
            url = str(p.get("url") or p.get("link") or "").strip()
            if not url:
                return "Which page or API, sir?"
            allow_private = str(_cfg().get("allow_private") or "no").lower() in ("yes", "true", "1")
            ok, why = net.validate_url(url, allow_private=allow_private)
            if not ok:
                return f"Sir, {why}."
            rows = _load()
            if len(rows) >= _int_cfg("max_items", 30, 1, 200) and _find(rows, url) is None:
                return f"I am already watching {len(rows)} prices, sir — the ceiling in my settings."
            name = re.sub(r"[^\w .-]", "", str(p.get("name") or "").strip())[:44] or \
                net.redact_url(url)[:44]
            below = to_price(p.get("below")) if p.get("below") not in (None, "") else None
            above = to_price(p.get("above")) if p.get("above") not in (None, "") else None
            rows = [row for row in rows if row.get("url") != url]
            row = {"name": name, "url": url, "marker": str(p.get("marker") or "").strip(),
                   "path": str(p.get("path") or "").strip(), "currency":
                   str(p.get("currency") or "").strip(), "below": below, "above": above,
                   "last": None, "history": [], "added": time.time(), "checked": 0, "alerts": 0}
            outcome = _fetch_price(row)
            if not outcome["ok"]:
                row["last_error"] = outcome["why"]
                rows.append(row)
                _save(rows)
                return (f"Stored '{name}', sir — but the first reading failed: {outcome['why']}. "
                        f"I will try again on the next check.")
            taken = judge({**row, "last": None}, float(outcome["value"]))
            row["last"] = float(outcome["value"])
            row["snippet"] = outcome["snippet"]
            row["history"] = [{"t": int(time.time()), "v": float(outcome["value"])}]
            rows.append(row)
            _save(rows)
            extra = (f"\n  {outcome['candidates']} price(s) on the page — I took the first; use a "
                     f"marker word to be exact." if outcome.get("candidates", 1) > 1 else "")
            return (f"Watching '{name}', sir — now {taken[1]}{extra}\n"
                    f"  read from: {outcome['snippet'][:120]}")

        if action in ("unwatch", "remove", "stop"):
            key = str(p.get("name") or p.get("url") or "").strip()
            rows = _load()
            row = _find(rows, key)
            if row is None:
                return f"I am not watching anything called '{key}', sir."
            rows = [r for r in rows if r is not row]
            _save(rows)
            return f"Stopped watching '{row['name']}', sir — and its history is gone with it."

        if action in ("list", "prices", "watching"):
            rows = _load()
            if not rows:
                return "I am not watching any prices yet, sir."
            lines = [f"{len(rows)} price(s) watched:"]
            for row in rows:
                latest = (f"{float(row['last']):,.2f}" if row.get("last") is not None
                          else "no reading yet")
                targets = []
                if row.get("below") is not None:
                    targets.append(f"below {float(row['below']):,.2f}")
                if row.get("above") is not None:
                    targets.append(f"above {float(row['above']):,.2f}")
                lines.append(f"  {row['name'][:22]:<24}{latest:>12}  "
                             f"{('| ' + ' / '.join(targets)) if targets else '':<26}"
                             f"{row.get('last_error', '')[:30]}")
            return "\n".join(lines)

        if action in ("check", "now", "run"):
            rows = _load()
            if not rows:
                return "I am not watching any prices yet, sir."
            lines = [f"Checked {len(rows)} price(s):", ""]
            alerts = 0
            for row in rows:
                outcome = check_one(row, player)
                if outcome["status"] == "alert":
                    alerts += 1
                    row["alerts"] = int(row.get("alerts") or 0) + 1
                    icon = "!"
                elif outcome["status"] == "failed":
                    icon = "×"
                else:
                    icon = "·"
                lines.append(f"  {icon} {row['name'][:20]:<22}{outcome['why'][:64]}")
                if outcome["status"] == "alert":
                    lines.append(f"      from: {outcome['snippet'][:90]}")
            _save(rows)
            if not alerts:
                lines.append("")
                lines.append("No target met and nothing moved since the last reading, sir.")
            return "\n".join(lines)

        if action in ("history", "past"):
            key = str(p.get("name") or "").strip()
            rows = _load()
            row = _find(rows, key) if key else (rows[0] if rows else None)
            if row is None:
                return "Nothing watched yet, sir."
            history = row.get("history") or []
            if not history:
                return f"No readings for '{row['name']}' yet, sir."
            lines = [f"{row['name']} — {len(history)} reading(s):"]
            for mark in history[-10:]:
                when = time.strftime("%d %b %H:%M", time.localtime(int(mark["t"])))
                lines.append(f"  {when}   {float(mark['v']):>12,.2f}")
            values = [float(m["v"]) for m in history]
            lines.append(f"  lowest {min(values):,.2f} · highest {max(values):,.2f}")
            return "\n".join(lines)

        if action in ("target", "alert", "set_target"):
            key = str(p.get("name") or p.get("url") or "").strip()
            rows = _load()
            row = _find(rows, key)
            if row is None:
                return f"I am not watching anything called '{key}', sir."
            if p.get("below") not in (None, ""):
                row["below"] = to_price(p.get("below"))
            if p.get("above") not in (None, ""):
                row["above"] = to_price(p.get("above"))
            _save(rows)
            below_text = "—" if row.get("below") is None else f"{float(row['below']):,.2f}"
            above_text = "—" if row.get("above") is None else f"{float(row['above']):,.2f}"
            return (f"Targets for '{row['name']}': below {below_text}, above {above_text}. "
                    f"I will tell you the next time a check crosses one.")

        return (f"Sir, '{action}' is not a price action I have. I can watch, unwatch, list, check, "
                f"history or set a target.")
    except Exception as e:                                     # noqa: BLE001
        return f"Sir, the price monitor hit a problem: {str(e)[:150]}"


# ── self-test: python price_monitor.py ──────────────────────────────────────

def _selftest() -> int:
    print("price_monitor — self-test")
    problems = 0

    print(f"  قراءة الأرقام           : '1,299.50'→{to_price('1,299.50')} · '٩٩'→{to_price('٩٩')} · "
          f"'12.00 EGP'→{to_price('12.00 EGP')} · 'abc'→{to_price('abc')}")
    problems += 0 if (to_price("1,299.50") == 1299.5 and to_price("٩٩") == 99.0
                      and to_price("12.00 EGP") == 12.0 and to_price("abc") is None) else 1

    page = "<html><body><h2>Cool Headphones</h2><div class='price'>Price: 1,299.50 EGP</div>" \
           "<span>Shipping 50 EGP</span><div>Warranty 12 months</div></body></html>"
    marked = extract_price(page, "Price")
    print(f"  بعلامة                  : {marked['value']} — من: {marked['snippet'][:62]!r}")
    problems += 0 if marked["found"] and marked["value"] == 1299.5 else 1

    unmarked = extract_price(page)
    print(f"  بلا علامة               : {unmarked['value']} · مرشّحات={unmarked['candidates']}")
    problems += 0 if unmarked["found"] and unmarked["candidates"] >= 2 else 1

    missing = extract_price("<html><body>Out of stock</body></html>", "Price")
    print(f"  صفحة بلا سعر             : {missing['why'][:64]}")
    problems += 0 if not missing["found"] and "not on the page" in missing["why"] else 1

    # rules: below / above / movement — no target means no alarm
    row = {"name": "x", "below": 1000.0, "above": None, "currency": "EGP"}
    alert, why = judge({**row, "last": None}, 950.0)
    problems += 0 if alert and "below your target" in why else 1
    print(f"  الهدف: أقل من           : {why}")
    alert2, why2 = judge({**row, "last": None}, 1200.0)
    problems += 0 if not alert2 else 1
    alert3, why3 = judge({"name": "x", "below": None, "above": None, "last": 1000.0}, 900.0)
    print(f"  بلا هدف لكن تغيّر السعر : {why3}")
    problems += 0 if alert3 and "down from" in why3 else 1
    alert4, _ = judge({"name": "x", "below": None, "above": None, "last": 1000.0}, 1000.0)
    problems += 0 if not alert4 else 1

    # a real server: page watch → same → drop below target (alert) → 403 (kept, not overwritten)
    import http.server
    import socketserver
    import threading as _t
    holder = {"body": page, "code": 200, "json": '{"data": {"price": 555.0}}'}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(holder["code"])
            if self.path.startswith("/api"):
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(holder["json"].encode())
                return
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(holder["body"].encode("utf-8"))

        def log_message(self, *a):
            pass

    real_cfg, real_state = _cfg, STATE
    sandbox = Path(tempfile.mkdtemp(prefix="price_probe_"))
    globals()["_cfg"] = lambda: {**real_cfg(), "allow_private": "yes", "history": "20"}
    globals()["STATE"] = sandbox / "prices.json"
    try:
        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
            port = httpd.server_address[1]
            base = f"http://127.0.0.1:{port}"
            _t.Thread(target=httpd.serve_forever, daemon=True).start()

            added = run({"action": "watch", "name": "headphones", "url": base + "/product",
                         "marker": "Price", "below": "1000", "currency": "EGP"})
            print(f"  متابعة سعر               : {added.splitlines()[0][:76]}")
            problems += 0 if "Watching 'headphones'" in added and "1,299.50" in added else 1
            problems += 0 if "read from:" in added else 1

            same = run({"action": "check"})
            print(f"  بلا تغيّر               : {[l.strip() for l in same.splitlines()[2:3]]}")
            problems += 0 if "nothing moved" in same else 1

            holder["body"] = page.replace("1,299.50", "899.00")
            drop = run({"action": "check"})
            print(f"  نزل تحت الهدف            : {[l.strip() for l in drop.splitlines()[2:4]]}")
            problems += 0 if "below your target" in drop else 1

            holder["code"] = 403
            blocked = run({"action": "check"})
            print(f"  الموقع رفض (403)         : {[l.strip() for l in blocked.splitlines()[2:3]]}")
            problems += 0 if "refused" in blocked else 1
            stored = json.loads(globals()["STATE"].read_text(encoding="utf-8"))
            problems += 0 if stored[0]["last"] == 899.0 else 1
            print(f"  آخر قراءة بقت            : {stored[0]['last']} (لا يُستبدل بخطأ)")

            holder["code"] = 200
            api = run({"action": "watch", "name": "api-item", "url": base + "/api",
                       "path": "data.price"})
            print(f"  متابعة عبر JSON path     : {api.splitlines()[0][:70]}")
            problems += 0 if "555.00" in api and "data.price" in api else 1

            history = run({"action": "history", "name": "headphones"})
            print(f"  السجل                   : {history.splitlines()[-1].strip()[:66]}")
            problems += 0 if "lowest" in history else 1

            target = run({"action": "target", "name": "headphones", "above": "2000"})
            print(f"  ضبط هدف                 : {target.splitlines()[0][:70]}")
            problems += 0 if "above" in target else 1

            listing = run({"action": "list"})
            problems += 0 if "2 price(s) watched" in listing else 1
            stopped = run({"action": "unwatch", "name": "api-item"})
            problems += 0 if "Stopped watching" in stopped else 1
            httpd.shutdown()
    finally:
        globals()["_cfg"], globals()["STATE"] = real_cfg, real_state
        import shutil
        shutil.rmtree(sandbox, ignore_errors=True)

    private = run({"action": "watch", "url": "http://127.0.0.1/x", "name": "y"})
    print(f"  عنوان خاص بلا سماح       : {private[:64]}")
    problems += 0 if "private" in private else 1
    print(f"  run بلا رابط             : {run({'action': 'watch'})[:56]}")
    problems += 0 if "Which page" in run({"action": "watch"}) else 1
    print(f"  run(action مجهول)       : {run({'action': 'sing'})[:56]}")
    problems += 0 if "not a price action" in run({"action": "sing"}) else 1
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
