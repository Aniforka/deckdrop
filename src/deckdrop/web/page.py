"""The single page UI: index.html with app.css and app.js inlined, served as one response."""

from ..bundle import resource

PAGE = (resource("web/index.html")
        .replace("/*@app.css*/", resource("web/app.css"))
        .replace("/*@app.js*/", resource("web/app.js"))
        .rstrip("\n"))
