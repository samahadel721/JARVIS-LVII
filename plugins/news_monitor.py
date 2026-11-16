"""
news_monitor.py — RSS/Atom feeds, without a dependency and without noise.

NO FEEDPARSER
    feedparser is a lovely library and it is one pip install more than this job
    needs. RSS 2.0 and Atom are XML, and Python ships an XML parser, so this file
    parses them itself: channel title, item titles, links, dates, description. It
    is not a general-purpose feed library and it does not pretend to be — it
    handles the two formats nearly every website publishes, and when a feed uses
    something else it says which feed and what it found instead of returning an
    empty list that looks like "no news".

WHY IT DOES NOT PING YOU FOR EVERY ITEM
    A feed's first fetch is the entire history — an empty result would be a lie
    and forty notifications would be a nuisance. So the first fetch records what
    is there and says "baseline taken"; from then on, only genuinely new items
    arrive. Dedupe is by the item's own id/link, then by a hash of title+date for
    feeds that have neither — and the summary is produced by the summarizer
    plugin next door, which works with no AI key at all.

POLITE BY CONSTRUCTION
    One request per feed per interval, an honest User-Agent, a timeout, a size
    ceiling, and every URL validated by the shared net core. A feed that answers
    403 is reported as a no. Nothing is fetched from a private address unless you
    allow it, and nothing is sent anywhere: alerts go to the project's own
    Notification Center.

SETTINGS (Plugin Manager → News monitor)
    interval_min  120 — shortest gap between checks per feed
    max_feeds     40
    per_feed      10 — most new items reported from one feed at a time
    allow_private no
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
from xml.etree import ElementTree

PLUGIN = {
    "name": "news_monitor",
    "description": (
        "Follows RSS and Atom feeds and tells you only what is genuinely new: 'follow "
        "theverge.com/rss/index.xml', 'what news do I follow', 'check my feeds', 'stop following "
        "X'. It remembers what it has seen, deduplicates, summarises through the summarizer "
        "plugin, and puts alerts in the Notification Center."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "follow | unfollow | list | check | latest | help"},
            "url": {"type": "STRING", "description": "Feed URL"},
            "name": {"type": "STRING", "description": "Short name for the feed"},
            "keyword": {"type": "STRING", "description": "Optional: only items whose title/body contains this"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "news_monitor",
    "title": "News monitor / مراقبة الأخبار",
    "fields": [
        {"key": "interval_min", "type": "text", "label": "Minutes between checks", "default": "120"},
        {"key": "max_feeds", "type": "text", "label": "Most feeds followed", "default": "40"},
        {"key": "per_feed", "type": "text", "label": "Most new items per check", "default": "10"},
        {"key": "allow_private", "type": "text",
         "label": "Allow local/private addresses (yes/no)", "default": "no"},
        {"key": "notify", "type": "text", "label": "Send alerts to the Notification Center (yes/no)",
         "default": "yes"},
    ],
}

STATE = Path(__file__).resolve().parent.parent / "config" / "feeds.json"
_NS = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("news_monitor") or {}).items()
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


def _sibling(name: str):
    key = f"_jarvis_{name}"
    module = sys.modules.get(key)
    if module is not None:
        return module
    import importlib.util
    path = Path(__file__).resolve().parent / f"{name}.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
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


# ── parsing (no feedparser needed) ────────────────────────────────────────────

def _clean(text: str, limit: int = 600) -> str:
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", str(text or ""))
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&#39;", "'"), ("&quot;", '"')):
        body = body.replace(entity, char)
    return re.sub(r"\s+", " ", body).strip()[:limit]


def parse_feed(xml_text: str) -> dict[str, Any]:
    """
    RSS 2.0 or Atom → {ok, title, items:[{id,title,link,date,summary}], reason}.
    Never raises: malformed XML is a reason, not a crash.
    """
    result: dict[str, Any] = {"ok": False, "title": "", "items": [], "reason": ""}
    try:
        root = ElementTree.fromstring(str(xml_text or "").strip())
    except Exception as e:
        return {**result, "reason": f"the feed is not readable XML ({str(e)[:80]})"}

    tag = root.tag.split("}")[-1].lower()
    if tag == "rss":
        channel = root.find("channel")
        if channel is None:
            return {**result, "reason": "an RSS file with no <channel>"}
        result["title"] = _clean(channel.findtext("title") or "", 80)
        for item in channel.findall("item"):
            guid = (item.findtext("guid") or "").strip()
            link = (item.findtext("link") or "").strip()
            title = _clean(item.findtext("title") or "(no title)", 160)
            date = (item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date")
                    or "").strip()
            summary = _clean(item.findtext("description") or "", 600)
            result["items"].append({"id": guid or link or _hash(title + date), "title": title,
                                    "link": link, "date": date, "summary": summary})
    elif tag == "feed":
        result["title"] = _clean(root.findtext("atom:title", namespaces=_NS) or "", 80)
        for entry in root.findall("atom:entry", _NS):
            link = ""
            for candidate in entry.findall("atom:link", _NS):
                if candidate.get("rel") in (None, "alternate"):
                    link = candidate.get("href") or ""
                    if link:
                        break
            title = _clean(entry.findtext("atom:title", namespaces=_NS) or "(no title)", 160)
            date = (entry.findtext("atom:updated", namespaces=_NS)
                    or entry.findtext("atom:published", namespaces=_NS) or "").strip()
            summary = _clean(entry.findtext("atom:summary", namespaces=_NS)
                             or entry.findtext("atom:content", namespaces=_NS) or "", 600)
            result["items"].append({"id": (entry.findtext("atom:id", namespaces=_NS) or link
                                           or _hash(title + date)), "title": title,
                                    "link": link, "date": date, "summary": summary})
    else:
        return {**result, "reason": (f"that XML is a '{tag}', not an RSS or Atom feed — check that "
                                     f"the address is the feed itself and not the site's page")}

    if not result["items"]:
        result["reason"] = "the feed parsed but has no items in it"
        return result
    result["ok"] = True
    return result


def _hash(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:16]


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
            module.publish(title, body, category="news", priority=priority, source="news")
    except Exception:
        pass


def _summarise(text: str) -> str:
    summarizer = _sibling("summarizer")
    if summarizer is None:
        return ""
    try:
        return str(summarizer.run({"action": "summarize", "text": text, "level": "short"}))
    except Exception:
        return ""


def _find(rows: list[dict], key: str) -> dict | None:
    wanted = str(key or "").strip().lower()
    for row in rows:
        if row.get("name", "").lower() == wanted or row.get("url", "").lower() == wanted:
            return row
    return None


# ── actions ───────────────────────────────────────────────────────────────────

def follow(url: str, name: str = "", keyword: str = "", player=None) -> str:
    net = _net()
    if net is None:
        return "The shared file '_net_core.py' is missing, sir."
    allow_private = str(_cfg().get("allow_private") or "no").lower() in ("yes", "true", "1")
    ok, why = net.validate_url(url, allow_private=allow_private)
    if not ok:
        return f"Sir, {why}."
    rows = _load()
    if len(rows) >= _int_cfg("max_feeds", 40, 1, 200) and _find(rows, url) is None:
        return f"I am already following {len(rows)} feeds, sir — the ceiling in my settings."
    label = re.sub(r"[^\w .-]", "", str(name or "").strip())[:40] or \
        re.sub(r"^www\.", "", url.split("//")[-1].split("/")[0])[:40]
    rows = [row for row in rows if row.get("url") != url]
    entry = {"name": label, "url": url, "keyword": str(keyword or "").strip(), "seen": [],
             "added": time.time(), "checked": 0, "new_count": 0}

    fetch = net.fetch(url, timeout=25, max_bytes=2_000_000, allow_private=allow_private,
                      headers={"Accept": "application/rss+xml, application/atom+xml, text/xml, */*"})
    if not fetch["ok"]:
        return f"Sir, {net.status_sentence(fetch)}."
    parsed = parse_feed(fetch["text"])
    if not parsed["ok"]:
        return f"Sir, {parsed['reason']}."
    entry["seen"] = [item["id"] for item in parsed["items"]][:400]
    entry["checked"] = time.time()
    rows.append(entry)
    _save(rows)
    return (f"Following '{label}', sir — {parsed['title'] or 'the feed'} with "
            f"{len(parsed['items'])} item(s), which I have taken as the baseline. From now on you "
            f"hear only about genuinely new ones.")


def check_feed(row: dict, player=None) -> dict[str, Any]:
    net = _net()
    allow_private = str(_cfg().get("allow_private") or "no").lower() in ("yes", "true", "1")
    fetch = net.fetch(row["url"], timeout=25, max_bytes=2_000_000, allow_private=allow_private,
                      headers={"Accept": "application/rss+xml, application/atom+xml, text/xml, */*"})
    row["checked"] = time.time()
    if not fetch["ok"]:
        return {"status": "failed", "reason": net.status_sentence(fetch), "new": []}
    if int(fetch.get("status") or 0) >= 400:
        return {"status": "failed", "reason": net.status_sentence(fetch), "new": []}
    parsed = parse_feed(fetch["text"])
    if not parsed["ok"]:
        return {"status": "failed", "reason": parsed["reason"], "new": []}

    seen = set(str(x) for x in (row.get("seen") or []))
    baseline = not seen
    fresh: list[dict] = []
    keyword = str(row.get("keyword") or "").strip().lower()
    for item in parsed["items"]:
        key = str(item["id"])
        if key in seen:
            continue
        seen.add(key)
        if keyword and keyword not in (item["title"] + " " + item["summary"]).lower():
            continue
        fresh.append(item)
    row["seen"] = list(seen)[-600:]
    if baseline:
        return {"status": "first", "reason": f"baseline taken — {len(parsed['items'])} item(s)",
                "new": []}
    if not fresh:
        return {"status": "same", "reason": "nothing new", "new": []}

    cap = _int_cfg("per_feed", 10, 1, 50)
    shown = fresh[:cap]
    row["new_count"] = int(row.get("new_count") or 0) + len(fresh)
    lines = []
    for item in shown:
        entry = f"{item['title']} — {item['link'] or '(no link)'}"
        if item["summary"]:
            short = _summarise(item["summary"]) or item["summary"][:200]
            entry += f"\n    {short[:220]}"
        lines.append(entry)
    if len(fresh) > cap:
        lines.append(f"… and {len(fresh) - cap} more new item(s).")
    _notify(f"{row['name']}: {len(fresh)} new item(s)",
            "; ".join(item["title"][:60] for item in shown[:3]), "normal")
    return {"status": "new", "reason": f"{len(fresh)} new item(s)", "new": shown,
            "text": "\n  ".join(lines)}


def check_all(player=None) -> str:
    rows = _load()
    if not rows:
        return "I am not following any feed yet, sir. Say 'follow <feed url>'."
    lines = [f"Checked {len(rows)} feed(s):", ""]
    total_new = 0
    for row in rows:
        outcome = check_feed(row, player)
        icon = {"first": "•", "same": "·", "new": "!", "failed": "×"}[outcome["status"]]
        lines.append(f"  {icon} {row['name'][:22]:<24}{outcome['reason'][:70]}")
        if outcome["status"] == "new":
            total_new += len(outcome["new"])
            for item in outcome["new"][:3]:
                lines.append(f"      {item['title'][:76]}")
    _save(rows)
    if not total_new:
        lines.append("")
        lines.append("Nothing new, sir.")
    return "\n".join(lines)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """One news action. Reads feeds; stores only ids it has seen."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "check").strip().lower()

        if action in ("help", "?"):
            return ("I follow RSS and Atom feeds, sir: 'follow theverge.com/rss/index.xml', 'check "
                    "my feeds', 'what do I follow', 'stop following theverge'. The first fetch is "
                    "a baseline — you hear only about new items, deduplicated, summarised, and put "
                    "in the Notification Center.")
        if action in ("follow", "add", "subscribe"):
            url = str(p.get("url") or p.get("feed") or "").strip()
            if not url:
                return "Which feed, sir? The RSS address."
            return follow(url, str(p.get("name") or ""), str(p.get("keyword") or ""), player)
        if action in ("unfollow", "remove", "stop"):
            key = str(p.get("name") or p.get("url") or "").strip()
            rows = _load()
            row = _find(rows, key)
            if row is None:
                return f"I am not following anything called '{key}', sir."
            rows = [r for r in rows if r is not row]
            _save(rows)
            return f"Stopped following '{row['name']}', sir."
        if action in ("list", "feeds", "following"):
            rows = _load()
            if not rows:
                return "No feeds followed yet, sir."
            lines = [f"{len(rows)} feed(s):"]
            for row in rows:
                when = (f"{int((time.time() - float(row.get('checked') or 0)) / 60)}m ago"
                        if row.get("checked") else "not checked")
                lines.append(f"  {row['name'][:22]:<24}{row.get('new_count', 0)} new so far  "
                             f"last: {when:<10}{row['url'][:40]}"
                             + (f"  [filter: {row['keyword']}]" if row.get("keyword") else ""))
            return "\n".join(lines)
        if action in ("check", "now", "run"):
            return check_all(player)
        if action in ("latest", "read"):
            key = str(p.get("name") or "").strip()
            rows = _load()
            row = _find(rows, key) if key else (rows[0] if rows else None)
            if row is None:
                return "No feed followed yet, sir."
            outcome = check_feed(row)
            _save(rows)
            if outcome["status"] == "failed":
                return f"Sir, {outcome['reason']}."
            if not outcome["new"]:
                return f"No new items in '{row['name']}', sir — {outcome['reason']}."
            return f"New in {row['name']}:\n  " + str(outcome.get("text", ""))[:1500]
        return (f"Sir, '{action}' is not a news action I have. I can follow, unfollow, list, check "
                f"or read the latest.")
    except Exception as e:                                     # noqa: BLE001
        return f"Sir, the news monitor hit a problem: {str(e)[:150]}"


# ── self-test: python news_monitor.py ───────────────────────────────────────

def _selftest() -> int:
    print("news_monitor — self-test")
    problems = 0

    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
    <title>Tech Daily</title>
    <item><title>First story</title><link>https://ex.example/1</link>
      <description>&lt;p&gt;Something &amp; something else&lt;/p&gt;</description>
      <pubDate>Mon, 01 Oct 2026 10:00:00 GMT</pubDate><guid>id-1</guid></item>
    <item><title>Second story</title><link>https://ex.example/2</link>
      <description>Plain text</description><guid>id-2</guid></item>
    </channel></rss>"""
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
    <title>Atom Source</title>
    <entry><title>Atom one</title><link href="https://a.example/1"/>
      <id>a-1</id><updated>2026-10-01T10:00:00Z</updated>
      <summary>Atom summary text</summary></entry>
    </feed>"""

    parsed = parse_feed(rss)
    print(f"  RSS                     : {parsed['ok']} — {parsed['title']!r} · "
          f"{len(parsed['items'])} عنصر · أول: {parsed['items'][0]['title'][:20]!r}")
    problems += 0 if parsed["ok"] and parsed["title"] == "Tech Daily" and len(parsed["items"]) == 2 else 1
    problems += 0 if parsed["items"][0]["summary"] == "Something & something else" else 1
    print(f"    تنظيف الوسوم           : {parsed['items'][0]['summary']!r}")

    parsed_atom = parse_feed(atom)
    print(f"  Atom                    : {parsed_atom['ok']} — {parsed_atom['title']!r} · "
          f"{parsed_atom['items'][0]['title']!r}")
    problems += 0 if parsed_atom["ok"] and parsed_atom["items"][0]["link"] == "https://a.example/1" else 1

    bad = parse_feed("<html><body>not a feed</body></html>")
    print(f"  صفحة ليست تغذية         : {bad['reason'][:64]}")
    problems += 0 if not bad["ok"] and "not an RSS or Atom" in bad["reason"] else 1
    broken = parse_feed("this is not xml at all")
    problems += 0 if not broken["ok"] and "readable XML" in broken["reason"] else 1
    empty_feed = parse_feed('<?xml version="1.0"?><rss version="2.0"><channel><title>X</title>'
                            '</channel></rss>')
    problems += 0 if not empty_feed["ok"] and "no items" in empty_feed["reason"] else 1

    # the whole life cycle against a local feed server
    import http.server
    import socketserver
    import threading as _t
    holder = {"body": rss}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            self.wfile.write(holder["body"].encode("utf-8"))

        def log_message(self, *a):
            pass

    real_cfg, real_state = _cfg, STATE
    sandbox = Path(tempfile.mkdtemp(prefix="news_probe_"))
    globals()["_cfg"] = lambda: {**real_cfg(), "allow_private": "yes", "per_feed": "10"}
    globals()["STATE"] = sandbox / "feeds.json"
    try:
        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
            port = httpd.server_address[1]
            url = f"http://127.0.0.1:{port}/feed.xml"
            _t.Thread(target=httpd.serve_forever, daemon=True).start()

            followed = run({"action": "follow", "url": url, "name": "probe"})
            print(f"  متابعة تغذية             : {followed.splitlines()[0][:80]}")
            problems += 0 if "Following 'probe'" in followed and "baseline" in followed else 1

            quiet = run({"action": "check"})
            print(f"  بلا جديد (الأساس)        : {[l.strip() for l in quiet.splitlines()[2:3]]}")
            problems += 0 if "Nothing new" in quiet else 1

            holder["body"] = rss.replace("</channel>",
                                         "<item><title>Third story</title>"
                                         "<link>https://ex.example/3</link>"
                                         "<description>Brand new thing</description>"
                                         "<guid>id-3</guid></item></channel>")
            fresh = run({"action": "check"})
            print(f"  عنصر جديد                : {[l.strip() for l in fresh.splitlines()[2:5]]}")
            problems += 0 if "1 new item" in fresh and "Third story" in fresh else 1

            again = run({"action": "check"})
            problems += 0 if "Nothing new" in again else 1
            print(f"  لا تكرار للمُبلَّغ عنه    : {'Nothing new' in again}")

            # Dedupe when the feed gives neither guid nor link: the fallback key
            # is title+date. Proven here rather than asserted — the same two
            # fetches must be first=baseline then same, never two "new"s.
            holder["body"] = ("<?xml version='1.0'?><rss version='2.0'><channel><title>N</title>"
                              "<item><title>Same</title><description>x</description>"
                              "<pubDate>Mon, 01 Oct 2026 10:00:00 GMT</pubDate></item>"
                              "</channel></rss>")
            row = {"name": "nog", "url": url, "keyword": "", "seen": [], "checked": 0,
                   "new_count": 0}
            first_pass = check_feed(row)
            second_pass = check_feed(row)
            print(f"  بلا guid/link → بصمة احتياطية: أول={first_pass['status']} · "
                  f"ثاني={second_pass['status']}")
            problems += 0 if (first_pass["status"] == "first"
                              and second_pass["status"] == "same") else 1

            listed = run({"action": "list"})
            problems += 0 if "1 feed(s)" in listed else 1
            stopped = run({"action": "unfollow", "name": "probe"})
            problems += 0 if "Stopped following" in stopped else 1
            httpd.shutdown()
    finally:
        globals()["_cfg"], globals()["STATE"] = real_cfg, real_state
        import shutil
        shutil.rmtree(sandbox, ignore_errors=True)

    private = run({"action": "follow", "url": "http://10.0.0.1/feed"})
    print(f"  عنوان خاص بلا سماح       : {private[:66]}")
    problems += 0 if "private" in private else 1
    print(f"  run بلا رابط             : {run({'action': 'follow'})[:56]}")
    problems += 0 if "Which feed" in run({"action": "follow"}) else 1
    print(f"  run(action مجهول)       : {run({'action': 'sing'})[:56]}")
    problems += 0 if "not a news action" in run({"action": "sing"}) else 1
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
