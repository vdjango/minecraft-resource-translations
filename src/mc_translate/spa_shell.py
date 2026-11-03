"""One minimal browser bootstrap for the Vue application.

Page content and navigation live in Vue components; no per-route HTML files are
served or maintained. A browser still requires an HTML document to start JS.
"""

import json
from functools import lru_cache
from html import escape
from pathlib import Path


COMMON_STYLES = ("style.css", "mirror.css", "platform.css", "network.css", "vue-runtime.css",
                 "admin.css", "admin-access.css", "vue-page-admin-mirror.css", "setup.css")

@lru_cache(maxsize=2)
def prerendered(path, revision):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def asset(root: Path, name: str) -> str:
    path = root / name
    revision = format(path.stat().st_mtime_ns, "x") if path.is_file() else "0"
    return "/assets/" + name + "?v=" + revision


def render_shell(root: Path, entry: str, head: str, *, seed=None, noindex=False) -> str:
    styles = list(COMMON_STYLES)
    css = "".join(f'<link rel="stylesheet" href="{asset(root, file)}">' for file in styles if (root / file).is_file())
    libraries = ""
    if entry == "project":
        libraries = "".join(
            f'<script data-cfasync="false" src="{asset(root, file)}" defer></script>'
            for file in ("markdown-it.min.js", "purify.min.js")
        )
    state = json.dumps(seed or {}, ensure_ascii=False, separators=(",", ":"))
    state = state.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    robots = '<meta name="robots" content="noindex,nofollow">' if noindex else ""
    # Project text stays readable to crawlers and users without JavaScript.
    # The translated body has already passed through render_body sanitization.
    fallback = ""
    if entry == "project" and seed and seed.get("title"):
        fallback = ('<main class="project-page"><h1>'
                    + escape(str(seed["title"])) + '</h1><p>'
                    + escape(str(seed.get("summary", ""))) + '</p><article class="prose">'
                    + str(seed.get("body", "")) + '</article></main>')
    snapshot = root / "public-prerender.json"
    if not noindex and not fallback and snapshot.is_file():
        fallback = prerendered(str(snapshot), snapshot.stat().st_mtime_ns).get(entry+":"+(seed or {}).get("publicMode", "translation"), "")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        + head + robots
        + '<link rel="icon" href="/assets/logo.svg" type="image/svg+xml">'
        + css
        + f'<script data-cfasync="false" src="{asset(root, "theme.js")}" defer></script>'
        + libraries
        + f'<script data-cfasync="false" type="module" src="{asset(root, "vue-page-" + entry + ".js")}"></script>'
        + '</head><body><script type="application/json" data-page-seed>'
        + state
        + '</script>' + fallback + ('<div class="app-boot" role="status" aria-live="polite">'
        + '<span>正在准备页面…</span><div class="site-skeleton-layout" aria-hidden="true">'
        + '<div class="site-skeleton"></div><div class="site-skeleton"></div>'
        + '<div class="site-skeleton"></div><div class="site-skeleton"></div></div></div>'
        if not fallback else '') + '</body></html>'
    )
