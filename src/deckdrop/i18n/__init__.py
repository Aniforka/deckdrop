"""Interface languages: message catalogs and the language each request is answered in.

Catalogs are i18n/<lang>.json next to this file, one per language: a flat map from a key
("settings.pin.title") to the text. Adding a language means adding one file. Values are
plain text with {name} placeholders; a key ending in "_html" may carry markup. A value
may instead be a map of plural forms ({"one": ..., "few": ..., "many": ..., "other": ...})
picked by the placeholder n.

The language of a request, first match wins:
  1. the choice made in Settings on that device (cookie deckdrop_lang);
  2. the browser's languages (Accept-Language), i.e. the phone or PC the page is open on;
  3. the language of the Steam client on the Deck (SteamOS keeps its system locale in
     English, the language picked on the Deck is Steam's own, in ~/.steam/registry.vdf);
  4. English.
Log lines stay in English.
"""
import json
import re
import threading
import time
from pathlib import Path

from .. import bundle

COOKIE = "deckdrop_lang"
FALLBACK = "en"
# Steam client language names -> ISO 639-1, for the languages a catalog may exist for
STEAM_LANGS = {"english": "en", "russian": "ru", "ukrainian": "uk", "german": "de", "french": "fr",
               "spanish": "es", "latam": "es", "italian": "it", "polish": "pl", "portuguese": "pt",
               "brazilian": "pt", "turkish": "tr", "japanese": "ja", "koreana": "ko",
               "schinese": "zh", "tchinese": "zh", "czech": "cs", "dutch": "nl"}

_local = threading.local()
_catalogs = {}
_steam = {"at": 0.0, "lang": None}


def available():
    """Languages that have a catalog, e.g. ("en", "ru")."""
    if bundle.FILES is not None:
        names = [k for k in bundle.FILES if re.fullmatch(r"i18n/\w+\.json", k)]
    else:
        names = ["i18n/" + p.name for p in Path(__file__).parent.glob("*.json")]
    return tuple(sorted(n[5:-5] for n in names))


def catalog(lang):
    if lang not in _catalogs:
        _catalogs[lang] = json.loads(bundle.resource(f"i18n/{lang}.json"))
    return _catalogs[lang]


def plural_form(lang, n):
    """CLDR plural category of n for the languages DeckDrop has."""
    n = abs(int(n))
    if lang in ("ru", "uk"):
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    return "one" if n == 1 else "other"


class _Keep(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def t(key, lang=None, **kw):
    """The text for key in lang (default: the current request's language), placeholders filled."""
    lang = lang or current()
    text = catalog(lang).get(key) if lang in available() else None
    if text is None and lang != FALLBACK:
        text = catalog(FALLBACK).get(key)
    if text is None:
        return key
    if isinstance(text, dict):
        form = plural_form(lang, kw.get("n", 0))
        text = text.get(form) or text.get("other") or text.get("many") or next(iter(text.values()))
    return text.format_map(_Keep(kw)) if kw else text


tr = t   # the name modules import: `t` is a common loop variable in the code


def steam_language():
    """Language of the Steam client (what the user picked on the Deck), or None. Cached for a minute."""
    now = time.time()
    if now - _steam["at"] < 60:
        return _steam["lang"]
    lang = None
    try:
        text = (Path.home() / ".steam" / "registry.vdf").read_text("utf-8", "replace")
        m = re.search(r'"language"\s+"([^"]+)"', text, re.I)
        if m:
            code = STEAM_LANGS.get(m.group(1).strip().lower())
            lang = code if code in available() else None
    except OSError:
        pass
    _steam.update(at=now, lang=lang)
    return lang


def default():
    """Language when no browser is asking: the Deck's Steam language, else English."""
    return steam_language() or FALLBACK


def from_accept_language(header):
    """Best available language from an Accept-Language header, or None."""
    best, best_q = None, 0.0
    for i, part in enumerate((header or "").split(",")):
        bits = part.strip().split(";")
        tag = bits[0].strip().lower()
        if not tag or tag == "*":
            continue
        q = 1.0
        for b in bits[1:]:
            b = b.strip()
            if b.startswith("q="):
                try:
                    q = float(b[2:])
                except ValueError:
                    q = 0.0
        q -= i * 1e-6                      # equal weights: earlier wins
        code = tag.split("-")[0]
        if code in available() and q > best_q:
            best, best_q = code, q
    return best


def cookie_choice(cookie_header):
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE and v in available():
            return v
    return None


def for_request(headers):
    """(language, source) for a request; source is "choice", "browser", "steam" or "default"."""
    lang = cookie_choice(headers.get("Cookie"))
    if lang:
        return lang, "choice"
    lang = from_accept_language(headers.get("Accept-Language"))
    if lang:
        return lang, "browser"
    lang = steam_language()
    if lang:
        return lang, "steam"
    return FALLBACK, "default"


def set_current(lang):
    _local.lang = lang


def current():
    return getattr(_local, "lang", None) or default()


def carry(fn):
    """Wrap fn for another thread so its messages use the language of the thread that started it."""
    lang = current()

    def run(*args, **kwargs):
        set_current(lang)
        return fn(*args, **kwargs)
    return run
