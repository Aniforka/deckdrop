"""The single page UI: index.html with app.css, app.js and the language's texts inlined.

index.html carries {{key}} placeholders that are filled on the server, so the page
arrives already translated; app.js gets the whole catalog as L and looks texts up
with t(). The page is built once per language.
"""
import html
import json
import re

from .. import i18n
from ..bundle import resource

_pages = {}


def render(lang):
    if lang not in _pages:
        def fill(m):
            key = m.group(1)
            if key == "lang":
                return lang
            text = i18n.t(key, lang)
            return text if key.endswith("_html") else html.escape(text)

        texts = dict(i18n.catalog(i18n.FALLBACK), **i18n.catalog(lang))
        names = {code: i18n.t("lang.name", code) for code in i18n.available()}
        inline = "const LANG=%s,LANGS=%s,L=%s;" % (json.dumps(lang), json.dumps(names, ensure_ascii=False),
                                                 json.dumps(texts, ensure_ascii=False))
        page = re.sub(r"\{\{([\w.]+)\}\}", fill, resource("web/index.html"))
        _pages[lang] = (page
                        .replace("/*@app.css*/", resource("web/app.css"))
                        .replace("/*@i18n*/", inline.replace("</", "<\\/"))
                        .replace("/*@app.js*/", resource("web/app.js"))
                        .rstrip("\n"))
    return _pages[lang]
