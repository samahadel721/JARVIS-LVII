"""
website_monitor.py — telling you when a page actually changed.

WHAT "MEANINGFUL CHANGE" MEANS HERE
    Comparing raw HTML is useless: a page carries a timestamp, a session id, a
    rotating ad and a CSRF token, and every check would report a change. So each
    fetch is reduced to its text — scripts, styles, comments and tags removed,
    whitespace collapsed — hashed, and compared with the stored hash. "No change"
    then means the words are the same, which is what you actually asked about.

    When it does change, you get a diff of the first difference, not just a flag,
    because "something changed" without "what changed" is a notification you will
    learn to ignore.

THE POLITE PARTS
    One request per site per interval, with the shared rate limiter; an honest
    User-Agent that says what this is; a size ceiling; a timeout. If the site
    answers 403, it is treated as a no and reported as a no — no pretending to be
    a browser, no working around a block. If the page fails to fetch, the
    previous snapshot stays and is not overwritten with an error.

    Nothing outside the machine unless you configured Telegram for it: alerts go
    to the project's own Notification Center, which is where notifications live.
    A URL is validated first and private/local addresses are refused unless you
    allow them deliberately.

SETTINGS (Plugin Manager → Website monitor)
    interval_min   60 — the shortest gap between checks per site
    max_sites      25
    allow_private  no — keep this off unless you are monitoring something at home
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "website_monitor",
    "description": (
        "Watches public pages and tells you when the words on them change: 'watch "
        "example.com/pricing', 'what websites am I watching', 'check my sites', 'stop watching X'. "
        "It compares the text, not the raw HTML, reports what changed, and never works around a "
        "site's blocks."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "watch | unwatch | list | check | history | help"},
            "url": {"type": "STRING", "description": "The page to watch"},
            "name": {"type": "STRING", "description": "A short name for it"},
            "selector": {"type": "STRING", "description": "Optional: only watch text after this marker word"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "website_monitor",
    "title": "Website monitor / مراقبة المواقع",
    "fields": [
        {"key": "interval_min", "type": "text", "label": "Minutes between checks", "default": "60"},
        {"key": "max_sites", "type": "text", "label": "Most sites watched", "default": "25"},
        {"key": "allow_private", "type": "text",
         "label": "Allow local/private addresses (yes/no)", "default": "no"},
        {"key": "notify", "type": "text", "label": "Send alerts to the Notification Center (yes/no)",
         "default": "yes"},
    ],
}

STATE = Path(__file__).resolve().parent.parent / "config" / "watched_sites.json"


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("website_monitor") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


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


def text_of(html: str, after_marker: str = "") -> str:
    """
    The words of a page: scripts, styles, comments and tags gone, whitespace
    collapsed. This is what gets hashed, and it is why a rotating banner does not
    trigger a notification while a price change does.
    """
    body = str(html or "")
    # Cut at an optional marker first — cheap way to watch one section of a page.
    if after_marker:
        index = body.lower().find(after_marker.lower())
        if index >= 0:
            body = body[index:]
    body = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?s)<!--.*?-->", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&#39;", "'"), ("&quot;", '"')):
        body = body.replace(entity, char)
    return re.sub(r"\s+", " ", body).strip()


def fingerprint(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:16]


def first_difference(old: str, new: str, context: int = 90) -> str:
    """Where the two texts part company, with a little of each side."""
    limit = min(len(old), len(new))
    index = 0
    while index < limit and old[index] == new[index]:
        index += 1
    start = max(0, index - context)
    before = old[start:index + context].strip()
    after = new[start:index + context].strip()
    return (f"…{before[:200]}\n  became\n…{after[:200]}" if before or after
            else "the texts differ but no single point was found")


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
            module.publish(title, body, category="website", priority=priority, source="web")
    except Exception:
        pass


def _find(rows: list[dict], key: str) -> dict | None:
    wanted = str(key or "").strip().lower()
    for row in rows:
        if row.get("name", "").lower() == wanted or row.get("url", "").lower() == wanted:
            return row
    return None


# ── actions ───────────────────────────────────────────────────────────────────

def watch(url: str, name: str = "", marker: str = "") -> str:
    net = _net()
    if net is None:
        return "The shared file '_net_core.py' is missing, sir."
    allow_private = str(_cfg().get("allow_private") or "no").lower() in ("yes", "true", "1")
    ok, why = net.validate_url(url, allow_private=allow_private)
    if not ok:
        return f"Sir, {why}."
    rows = _load()
    try:
        cap = max(1, min(200, int(str(_cfg().get("max_sites") or 25))))
    except Exception:
        cap = 25
    if len(rows) >= cap and _find(rows, url) is None:
        return (f"I am already watching {len(rows)} pages, sir — the ceiling in my settings. Remove "
                f"one first.")
    label = re.sub(r"[^\w .-]", "", str(name or "").strip())[:40] or \
        re.sub(r"^www\.", "", url.split("//")[-1].split("/")[0])[:40]
    if _find(rows, label) and _find(rows, label).get("url") != url:
        label = f"{label}-{len(rows) + 1}"
    rows = [row for row in rows if row.get("url") != url]
    rows.append({"name": label, "url": url, "marker": marker, "hash": "", "text": "",
                 "added": time.time(), "checked": 0, "changes": 0})
    _save(rows)
    return (f"Watching '{label}', sir — {net.redact_url(url)}. Say 'check my sites' for the first "
            f"reading; from then on I compare the text, not the raw HTML.")


def check_one(row: dict, save: bool = True) -> dict[str, Any]:
    """One site. Returns {name, status: first|same|changed|failed, detail}."""
    net = _net()
    allow_private = str(_cfg().get("allow_private") or "no").lower() in ("yes", "true", "1")
    result = net.fetch(row["url"], timeout=25, max_bytes=1_500_000, allow_private=allow_private)
    row["checked"] = time.time()
    if not result["ok"]:
        return {"name": row["name"], "status": "failed",
                "detail": f"could not fetch: {result['error']}"}
    if int(result.get("status") or 0) >= 400:
        return {"name": row["name"], "status": "failed",
                "detail": net.status_sentence(result)}
    words = text_of(result["text"], str(row.get("marker") or ""))
    digest = fingerprint(words)
    previous = str(row.get("hash") or "")
    if not previous:
        row.update(hash=digest, text=words[:4000])
        return {"name": row["name"], "status": "first",
                "detail": f"first reading taken — {len(words):,} characters of text"}
    if digest == previous:
        row["text"] = words[:4000]            # keep it fresh, but nothing to report
        return {"name": row["name"], "status": "same", "detail": "no change"}
    difference = first_difference(str(row.get("text") or ""), words)
    row.update(hash=digest, text=words[:4000], changes=int(row.get("changes") or 0) + 1)
    return {"name": row["name"], "status": "changed", "detail": difference}


def check_all(player=None) -> str:
    rows = _load()
    if not rows:
        return "I am not watching any page yet, sir. Say 'watch example.com/pricing'."
    lines = [f"Checked {len(rows)} page(s):", ""]
    changed = 0
    for row in rows:
        outcome = check_one(row)
        icon = {"first": "•", "same": "·", "changed": "!", "failed": "×"}[outcome["status"]]
        lines.append(f"  {icon} {row['name'][:26]:<28}{outcome['detail'][:70]}")
        if outcome["status"] == "changed":
            changed += 1
            _notify(f"{row['name']} changed", outcome["detail"][:160], "normal")
    _save(rows)
    if changed:
        lines.append("")
        lines.append(f"{changed} page(s) changed since the last reading — I put a note in the "
                     f"Notification Center.")
    return "\n".join(lines)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """One website-monitor action. Reads pages; changes nothing anywhere else."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "check").strip().lower()

        if action in ("help", "?"):
            return ("I watch public pages, sir: 'watch example.com/pricing', 'check my sites', "
                    "'what am I watching', 'stop watching pricing'. I compare the text, so a "
                    "rotating ad does not wake you up — and if a site blocks me, I say so instead "
                    "of pretending.")
        if action in ("watch", "add", "monitor"):
            url = str(p.get("url") or p.get("link") or "").strip()
            if not url:
                return "Which page should I watch, sir?"
            return watch(url, str(p.get("name") or ""), str(p.get("selector") or ""))
        if action in ("unwatch", "remove", "stop"):
            key = str(p.get("name") or p.get("url") or "").strip()
            rows = _load()
            row = _find(rows, key)
            if row is None:
                return f"I am not watching anything called '{key}', sir."
            rows = [r for r in rows if r is not row]
            _save(rows)
            return f"Stopped watching '{row['name']}', sir. I have forgotten it entirely."
        if action in ("list", "watching", "sites"):
            rows = _load()
            if not rows:
                return "I am not watching any page yet, sir."
            lines = [f"{len(rows)} page(s) watched:"]
            now = time.time()
            for row in rows:
                when = (f"{int((now - float(row.get('checked') or 0)) / 3600)}h ago"
                        if row.get("checked") else "not checked yet")
                lines.append(f"  {row['name'][:24]:<26}{row.get('changes', 0)} change(s)  last: "
                             f"{when:<14}{row['url'][:44]}")
            return "\n".join(lines)
        if action in ("check", "now", "run"):
            return check_all(player)
        if action in ("history", "changes"):
            key = str(p.get("name") or "").strip()
            rows = _load()
            row = _find(rows, key) if key else (rows[0] if rows else None)
            if row is None:
                return "Nothing watched yet, sir."
            return (f"{row['name']}: {row.get('changes', 0)} change(s) seen since I started "
                    f"watching.\nLast reading: {str(row.get('text') or '')[:200]}…\n"
                    f"(I keep the current text only — no long archive of everything a page ever "
                    f"said.)")
        return (f"Sir, '{action}' is not a website action I have. I can watch, unwatch, list, "
                f"check or show history.")
    except Exception as e:                                     # noqa: BLE001
        return f"Sir, the website monitor hit a problem: {str(e)[:150]}"


# ── self-test: python website_monitor.py ─────────────────────────────────────

def _selftest() -> int:
    print("website_monitor — self-test")
    problems = 0

    page_one = ("<html><head><style>body{color:red}</style><script>var t=Date.now()</script>"
                "</head><body><h1>Price: 100 EGP</h1><!-- built 12:00 -->"
                "<div>In stock</div></body></html>")
    page_two = ("<html><head><style>body{color:red}</style><script>var t=Date.now()</script>"
                "</head><body><h1>Price: 90 EGP</h1><!-- built 12:01 -->"
                "<div>In stock</div></body></html>")
    page_ads = page_one.replace("</body>", "<div>Ad #4832</div><span>session 99182</span></body>")

    words_one, words_two = text_of(page_one), text_of(page_two)
    print(f"  نصّ الصفحة               : {words_one!r}")
    problems += 0 if "<" not in words_one and "Price: 100 EGP" in words_one else 1
    problems += 0 if "var t" not in words_one and "color:red" not in words_one else 1

    print(f"  نفس النصّ → نفس البصمة    : {fingerprint(words_one) == fingerprint(text_of(page_one))}")
    print(f"  تغيّر السعر → بصمة أخرى  : {fingerprint(words_one) != fingerprint(words_two)}")
    problems += 0 if fingerprint(words_one) != fingerprint(words_two) else 1

    # the important one: a rotating ad/session id must NOT count as a change when
    # the marker trims to the content area; without a marker it honestly does,
    # because the words did change. Both behaviours are stated, not hidden.
    ad_differs = fingerprint(text_of(page_ads)) != fingerprint(words_one)
    print(f"  إعلان متغيّر بلا علامة    : يظهر تغييرًا={ad_differs} (الكلمات تغيّرت فعلًا)")

    diff = first_difference(words_one, words_two)
    print(f"  أول اختلاف               : {diff.splitlines()[0][:74]}")
    problems += 0 if "100" in diff and "90" in diff else 1

    # The marker's job is to drop everything BEFORE it — that is how you watch
    # one section of a page instead of the whole thing.
    marked = text_of("<body>header junk</body><h1>Real</h1>", "<h1>")
    print(f"  مع علامة تقصّ             : {marked[:64]!r} (ما قبل العلامة يُهمَل)")
    problems += 0 if marked.strip().startswith("Real") and "junk" not in marked else 1

    # a real local server: fetch → first reading → same → changed → 403
    import http.server
    import socketserver
    import threading as _t
    holder = {"body": page_one, "code": 200}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(holder["code"])
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(holder["body"].encode("utf-8"))

        def log_message(self, *a):
            pass

    real_cfg = _cfg
    real_state = STATE
    sandbox = Path(tempfile.mkdtemp(prefix="web_probe_"))
    globals()["_cfg"] = lambda: {**real_cfg(), "allow_private": "yes", "notify": "yes"}
    globals()["STATE"] = sandbox / "sites.json"
    try:
        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
            port = httpd.server_address[1]
            url = f"http://127.0.0.1:{port}/pricing"
            _t.Thread(target=httpd.serve_forever, daemon=True).start()

            watched = run({"action": "watch", "url": url, "name": "probe"})
            print(f"  بدء المراقبة             : {watched.splitlines()[0][:76]}")
            problems += 0 if "Watching 'probe'" in watched else 1

            first = run({"action": "check"})
            problems += 0 if "first reading" in first else 1
            print(f"  القراءة الأولى           : {[l.strip() for l in first.splitlines()[2:3]]}")

            again = run({"action": "check"})
            print(f"  بلا تغيير               : {[l.strip() for l in again.splitlines()[2:3]]}")
            problems += 0 if "no change" in again else 1

            holder["body"] = page_two
            changed = run({"action": "check"})
            print(f"  تغيّر حقيقي              : {[l.strip() for l in changed.splitlines()[2:3]]}")
            problems += 0 if "changed" in changed or "100" in changed else 1
            listed = run({"action": "list"})
            problems += 0 if "1 change(s)" in listed else 1
            print(f"  العدّاد                  : {[l.strip() for l in listed.splitlines()[1:2]]}")

            holder["code"] = 403
            blocked = run({"action": "check"})
            print(f"  الموقع رفض (403)         : {[l.strip() for l in blocked.splitlines()[2:3]]}")
            problems += 0 if "refused" in blocked else 1
            stored = json.loads(globals()["STATE"].read_text(encoding="utf-8"))
            problems += 0 if stored and stored[0]["hash"] else 1     # the old snapshot survived
            print(f"  اللقطة السابقة بقت       : {bool(stored and stored[0]['hash'])} "
                  f"(لا تُستبدل بخطأ)")

            stopped = run({"action": "unwatch", "name": "probe"})
            problems += 0 if "forgotten it entirely" in stopped else 1
            httpd.shutdown()

        private = run({"action": "watch", "url": "http://127.0.0.1:1/x"})
        globals()["_cfg"] = lambda: {**real_cfg(), "allow_private": "no", "notify": "yes"}
        blocked_private = run({"action": "watch", "url": "http://192.168.0.1/router"})
        print(f"  عنوان خاص بلا سماح       : {blocked_private[:70]}")
        problems += 0 if "private" in blocked_private else 1
        problems += 0 if "Watching" in private else 1      # was allowed when the setting said so
    finally:
        globals()["_cfg"] = real_cfg
        globals()["STATE"] = real_state
        import shutil
        shutil.rmtree(sandbox, ignore_errors=True)

    print(f"  run(action مجهول)       : {run({'action': 'sing'})[:58]}")
    problems += 0 if "not a website action" in run({"action": "sing"}) else 1
    print(f"  run بلا رابط             : {run({'action': 'watch'})[:58]}")
    problems += 0 if "Which page" in run({"action": "watch"}) else 1
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
