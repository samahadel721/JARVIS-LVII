"""
plugins/currency_rates.py — live exchange rates and currency conversion.

Drop this single file into `plugins/`. Standard library only, no API key, no
signup: it reads a free public rate feed and keeps a local cache so a failed
network never turns into a failed answer.

WHY IT CACHES THE WHOLE TABLE
    One request per hour answers any conversion for the next hour. Asking the
    network for every single "100 dollars in Egyptian pounds" would be slower,
    would hammer a free service, and would break the moment the Wi-Fi does.
    With the table on disk, the honest fallback is available: reply with the
    rate *and* say how old it is.

IS IT ACCURATE ENOUGH FOR MONEY?
    These are mid-market reference rates, not the rate a bank or exchange office
    will give you. The reply says so when the amount is large enough for the
    spread to matter, so nobody budgets a trip on a number that was never an
    offer.

SETTINGS (Plugin Manager → Currency Rates)
    Preferred base currency, refresh window, and whether to warn on stale rates.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

PLUGIN = {
    "name": "currency_rates",
    "description": (
        "Live exchange rates and currency conversion, with a local cache so it "
        "still answers when the internet is down. Use for: 'how much is 100 "
        "dollars in Egyptian pounds', 'what is the euro rate today', 'convert "
        "2500 pounds to dollars', 'which currencies can you convert'. Handles "
        "currency names as well as codes. Do NOT use web_search for exchange "
        "rates — this tool is exact and cached."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "'convert' (default) to change an amount, 'rate' for a single "
                    "pair without an amount, or 'list' for the currencies available."
                ),
            },
            "amount": {"type": "NUMBER", "description": "How much to convert, e.g. 100."},
            "from": {
                "type": "STRING",
                "description": "Source currency — code or name ('USD', 'dollar', 'دولار', 'جنيه').",
            },
            "to": {
                "type": "STRING",
                "description": "Target currency — code or name ('EGP', 'egyptian pound').",
            },
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "currency_rates",
    "title": "Currency Rates / أسعار العملات",
    "fields": [
        {"key": "base_currency", "type": "text", "label": "Base currency / العملة الأساسية",
         "default": "EGP", "placeholder": "EGP"},
        {"key": "refresh_minutes", "type": "text", "label": "Refresh (minutes) / مدة التحديث",
         "default": "60", "placeholder": "60"},
        {"key": "warn_stale", "type": "toggle", "label": "Warn me when rates are old / تنبيه لو قديمة",
         "default": True},
    ],
}

#: Free feeds, tried in order. Neither needs a key or an account.
#:  - open.er-api.com : 160+ currencies including EGP
#:  - frankfurter.dev: European Central Bank reference rates (majors only)
_FEEDS = (
    "https://open.er-api.com/v6/latest/{base}",
    "https://api.frankfurter.dev/v1/latest?base={base}",
)

#: Currency words the model may hand over instead of a code — English and Arabic.
_NAMES = {
    "dollar": "USD", "dollars": "USD", "usd": "USD", "دولار": "USD", "دولار امريكي": "USD",
    "euro": "EUR", "euros": "EUR", "eur": "EUR", "يورو": "EUR",
    "pound": "GBP", "sterling": "GBP", "gbp": "GBP", "جنيه استرليني": "GBP",
    "egyptian pound": "EGP", "egp": "EGP", "جنيه": "EGP", "جنيه مصري": "EGP",
    "riyal": "SAR", "sar": "SAR", "ريال": "SAR", "ريال سعودي": "SAR",
    "dirham": "AED", "aed": "AED", "درهم": "AED", "درهم اماراتي": "AED",
    "dinar": "KWD", "kwd": "KWD", "دينار": "KWD",
    "qatari riyal": "QAR", "qar": "QAR", "ريال قطري": "QAR",
    "yen": "JPY", "jpy": "JPY", "ين": "JPY",
    "yuan": "CNY", "rmb": "CNY", "cny": "CNY", "يوان": "CNY",
    "lira": "TRY", "try": "TRY", "ليرة": "TRY",
    "jordanian dinar": "JOD", "jod": "JOD",
    "moroccan dirham": "MAD", "mad": "MAD",
    "tunisian dinar": "TND", "tnd": "TND",
    "algerian dinar": "DZD", "dzd": "DZD",
    "canadian dollar": "CAD", "cad": "CAD",
    "swiss franc": "CHF", "chf": "CHF", "فرنك": "CHF",
    "indian rupee": "INR", "inr": "INR", "روبية": "INR",
    "ruble": "RUB", "rub": "RUB", "روبل": "RUB",
}

#: A few display names so the spoken answer sounds like a sentence, not a ticker.
_LABELS = {
    "USD": "US dollars", "EUR": "euros", "GBP": "British pounds",
    "EGP": "Egyptian pounds", "SAR": "Saudi riyals", "AED": "UAE dirhams",
    "KWD": "Kuwaiti dinars", "QAR": "Qatari riyals", "JOD": "Jordanian dinars",
    "TRY": "Turkish lira", "JPY": "Japanese yen", "CNY": "Chinese yuan",
    "CHF": "Swiss francs", "CAD": "Canadian dollars", "AUD": "Australian dollars",
    "INR": "Indian rupees", "MAD": "Moroccan dirhams", "TND": "Tunisian dinars",
    "DZD": "Algerian dinars", "RUB": "Russian rubles",
}

_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "EGP": "E£", "SAR": "﷼", "AED": "د.إ",
            "JPY": "¥", "CNY": "¥", "KWD": "د.ك", "TRY": "₺"}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    """
    Where the last good rate table lives.

    `config/` is created by the app on first run; if it does not exist yet
    (a plugin called before setup finished) this still works, because the
    directory is created here rather than assumed.
    """
    try:
        from memory.config_manager import BASE_DIR
        base = Path(BASE_DIR)
    except Exception:
        base = Path(__file__).resolve().parent.parent
    folder = base / "config" / ".plugin_cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "currency_rates.json"


def _load_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(base: str, rates: dict, when: float, source: str) -> None:
    try:
        _cache_path().write_text(json.dumps(
            {"base": base, "rates": rates, "when": when, "source": source},
            ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass          # a cache that cannot be written is not an error worth speaking about


def _cache_ages_in_minutes(entry: dict) -> float:
    return (time.time() - float(entry.get("when", 0))) / 60.0


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "JARVIS-plugin/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_table(base: str, timeout: float = 8.0) -> tuple[dict, str]:
    """(rates keyed by base, source name). Raises only if every feed is down."""
    last_error: Exception | None = None
    for template in _FEEDS:
        host = template.split("/")[2]
        try:
            payload = _get_json(template.format(base=base), timeout)
            rates = payload.get("rates") or {}
            # Frankfurter answers with the ECB set, which does not contain every
            # currency — an empty answer is treated as a failure of that feed.
            if isinstance(rates, dict) and rates:
                rates.setdefault(base, 1.0)
                return {k.upper(): float(v) for k, v in rates.items()}, host
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("no feed replied")


def _table(base: str, refresh_minutes: float) -> tuple[dict, str, float, bool]:
    """
    (rates, source, age_in_minutes, is_stale).

    Fresh enough → cache. Otherwise try the feeds; on failure keep serving the
    old table and mark it stale so the reply can admit the age. A plugin that
    answers "the dollar is 48.6" from this morning is more useful than one that
    says it cannot reach the internet.
    """
    cached = _load_cache()
    if cached.get("base") == base and cached.get("rates"):
        age = _cache_ages_in_minutes(cached)
        if age <= max(1.0, refresh_minutes):
            return cached["rates"], str(cached.get("source", "cache")), age, False
    else:
        age = 999.0 if cached else 0.0

    try:
        rates, source = _fetch_table(base)
        _save_cache(base, rates, time.time(), source)
        return rates, source, 0.0, False
    except Exception:
        if cached.get("rates"):
            return cached["rates"], str(cached.get("source", "cache")), \
                _cache_ages_in_minutes(cached), True
        raise


# ---------------------------------------------------------------------------
# Currency codes and formatting
# ---------------------------------------------------------------------------

def _code(text: str, fallback: str) -> str:
    """
    Whatever the model passed → a three-letter code.

    Accepts 'USD', 'usd', 'dollar', 'جنيه', or a full name. Unknown three-letter
    codes are passed through untouched, which is what makes currencies nobody
    listed here still work.
    """
    raw = str(text or "").strip()
    if not raw:
        return fallback
    low = raw.lower().replace("_", " ")
    if low in _NAMES:
        return _NAMES[low]
    compact = low.replace(" ", "").replace("pounds", "pound").replace("riyals", "riyal")
    if compact in _NAMES:
        return _NAMES[compact]
    if len(compact) == 3 and compact.isalpha():
        return compact.upper()
    for word, code in _NAMES.items():          # "egyptian pound" style names
        if word in low:
            return code
    return raw.upper()[:3]


def _label(code: str) -> str:
    return _LABELS.get(code, code)


def _money(value: float, code: str) -> str:
    """
    Numbers people can read: two decimals for normal amounts, more only when
    the extra digits carry information (small rates between two strong
    currencies), and grouping so "48,612.50" is not read as a phone number.
    """
    if value is None:
        return "—"
    decimals = 2
    if 0 < abs(value) < 0.1:
        decimals = 6
    elif 0 < abs(value) < 1:
        decimals = 4
    text = f"{value:,.{decimals}f}"
    symbol = _SYMBOLS.get(code)
    return f"{text} {symbol}" if symbol else f"{text} {code}"


def _config() -> dict:
    values = {"base_currency": "EGP", "refresh_minutes": "60", "warn_stale": True}
    try:
        from memory.config_manager import get_plugin_config
        for key, value in (get_plugin_config("currency_rates") or {}).items():
            if value not in (None, ""):
                values[key] = value
    except Exception:
        pass
    try:
        values["refresh_minutes"] = float(values["refresh_minutes"])
    except Exception:
        values["refresh_minutes"] = 60.0
    return values


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _stale_note(age_minutes: float, stale: bool, config: dict) -> str:
    if not stale:
        return ""
    hours = age_minutes / 60.0
    age = f"{hours:.1f} hours" if hours >= 1 else f"{age_minutes:.0f} minutes"
    if not config.get("warn_stale", True):
        return ""
    return f" These rates are {age} old — the network could not be reached just now."


def _convert(rates: dict, source: str, age: float, stale: bool, config: dict,
             amount: float, src: str, dst: str) -> str:
    if src not in rates or dst not in rates:
        have = ", ".join(sorted(rates)[:24])
        return (f"I do not have a rate for {src} or {dst}. Currencies available "
                f"include: {have}…")
    # rates[] is "units of X per 1 base", so the base cancels out of the ratio.
    unit = rates[dst] / rates[src]
    result = amount * unit
    line = (f"{_money(amount, src)} = {_money(result, dst)} "
            f"(1 {src} = {_money(unit, dst)}, rate from {source})")
    if abs(amount) >= 1000:
        line += (" — that is a mid-market reference rate, not what a bank or "
                 "exchange office will offer.")
    return line + _stale_note(age, stale, config)


def _rate(rates: dict, source: str, age: float, stale: bool, config: dict,
          src: str, dst: str) -> str:
    return _convert(rates, source, age, stale, config, 1.0, src, dst)


def _list(rates: dict, source: str, base: str) -> str:
    codes = sorted(rates)
    named = [f"{c} ({_label(c)})" for c in codes if c in _LABELS]
    return (f"{len(codes)} currencies available against {base}, source {source}. "
            f"Common ones: {', '.join(named[:18])}.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    try:
        config = _config()
        base = _code(config.get("base_currency", "EGP"), "EGP")

        action = str(parameters.get("action", "convert") or "convert").strip().lower()
        src = _code(parameters.get("from"), base)
        dst = _code(parameters.get("to"), base)

        try:
            amount = float(parameters.get("amount", 1) or 1)
        except (TypeError, ValueError):
            amount = 1.0

        if action == "list":
            rates, source, _, stale = _table(base, config["refresh_minutes"])
            result = _list(rates, source, base)
        else:
            rates, source, age, stale = _table(base, config["refresh_minutes"])
            if action == "rate":
                result = _rate(rates, source, age, stale, config, src, dst)
            else:
                result = _convert(rates, source, age, stale, config, amount, src, dst)

    except Exception as exc:
        return (f"Sir, I could not reach an exchange-rate feed and I have no "
                f"cached rates yet: {exc}")

    if player:
        try:
            player.write_log(f"JARVIS: {result}")
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Tiny self-test — `python plugins/currency_rates.py` runs it without the app
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(run({"action": "convert", "amount": 100, "from": "USD", "to": "EGP"}))
    print(run({"action": "rate", "from": "EUR", "to": "EGP"}))
    print(run({"action": "list"}))
