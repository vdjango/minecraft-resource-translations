"""Public crawlable HTML. Only indexed content is read; crawlers never queue jobs."""

import hashlib
import json
import math
import re
import time
from urllib.parse import urlparse
from collections import OrderedDict
from html import escape

import bleach
from aiohttp import web
from markdown_it import MarkdownIt

from .hashing import resource_ref
from .languages import DEFAULT_LANGUAGE, LANGUAGES, normalize_language

LABELS = dict(mod="模组", resourcepack="资源包", shader="光影", modpack="整合包")
PUBLIC = {"index.html": ("Minecraft 模组、光影与资源包翻译 · Buggcraft", "共享 Minecraft 模组、资源包、光影与整合包的多语言介绍，保留原版排版，支持启动器自动查询与社区翻译节点。"),
          "docs.html": ("开放接口与启动器接入文档 · Buggcraft Translate", "资源翻译 API 文档：批量查询、自动翻译、原文版本更新、社区节点分发，以及 Java、C、C++、C#、Python 接入示例。"),
          "network.html": ("社区翻译节点 · Buggcraft Translate", "部署并加入 Buggcraft 社区翻译网络，就近读取共享译文，支持节点测速、缓存及主服务回退。"),
          "developers.html": ("申请启动器接入 · Buggcraft Translate", "为 Minecraft 启动器申请资源翻译 API 密钥，自动读取共享译文并跟踪原文更新。")}


def metadata(title, description, url):
    return (f'<title>{escape(title)}</title><meta name="description" content="{escape(description, quote=True)}">'
            f'<link rel="canonical" href="{escape(url, quote=True)}">'
            f'<meta property="og:type" content="website"><meta property="og:title" content="{escape(title, quote=True)}">'
            f'<meta property="og:description" content="{escape(description, quote=True)}"><meta property="og:url" content="{escape(url, quote=True)}">'
            '<meta name="twitter:card" content="summary">')


def render_body(text, format):
    if format == "plain":
        return "<p>" + escape(text).replace("\n", "<br>") + "</p>"
    html = MarkdownIt("commonmark", {"html": True}).enable("table").render(text) if format == "markdown" else text
    tags = set(bleach.sanitizer.ALLOWED_TAGS) | {"p", "div", "span", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "img", "table", "thead", "tbody", "tr", "th", "td", "details", "summary", "del", "s", "kbd"}
    def attr(tag, name, value):
        if name in ("alt", "title", "colspan", "rowspan"):
            return True
        # HTTPS media only; links may be relative to the original source.
        return name == "href" or name == "src" and value.startswith("https://")
    clean = bleach.clean(html, tags=tags, attributes=attr, protocols={"https", "http", "mailto"}, strip=True)
    return clean.replace("<img ", '<img loading="lazy" referrerpolicy="no-referrer" ')


def source_link(url, label):
    if urlparse(url or "").scheme != "https":
        return escape(label)
    return '<a href="'+escape(url, quote=True)+'" target="_blank" rel="noopener noreferrer">'+escape(label)+'</a>'


def releases_html(metadata):
    rows = []
    for release in metadata.get("releases", []):
        dependencies = "、".join(source_link(d.get("url"), d.get("name") or d.get("project_id") or "依赖") for d in release.get("dependencies", [])) or "此版本未声明额外依赖"
        rows.append('<tr><td>'+source_link(release.get("url"), release["name"])+'</td><td>'+escape(" / ".join(release.get("game_versions", [])))+'</td><td>'+escape(" / ".join(release.get("loaders", [])))+'</td><td>'+dependencies+'</td></tr>')
    return '<details><summary>版本与依赖</summary><div class="table-scroll"><table><thead><tr><th>版本</th><th>Minecraft</th><th>加载器</th><th>依赖</th></tr></thead><tbody>'+"".join(rows)+'</tbody></table></div></details>' if rows else ""


class PublicPages:
    def __init__(self, api, root):
        self.api, self.root, self.store = api, root, api.store
        config = api.config
        self.base = (api.app["federation"].hub if config.federation_role == "mirror" else config.public_url).rstrip("/")
        self.cache, self.cache_bytes = OrderedDict(), 0

    def response(self, request, text, content_type="text/html", status=200, cache=False):
        body = text.encode("utf-8")
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        # The deployed CDN enforces a long browser TTL for cacheable responses.
        # Keep HTML/status freshness in the bounded origin cache, not browser storage.
        headers = {"ETag": etag, "Cache-Control": "no-cache, no-store, must-revalidate", "Cloudflare-CDN-Cache-Control": "no-store"}
        if cache and request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        response = web.Response(body=body, content_type=content_type, charset="utf-8", status=status, headers=headers)
        response.enable_compression()
        return response

    async def cached(self, key, build):
        entry = self.cache.get(key)
        if entry and entry[0] == self.store.version and entry[1] > time.monotonic():
            self.cache.move_to_end(key)
            return entry[2]
        version = self.store.version
        text = await build()
        size = len(text.encode("utf-8"))
        old = self.cache.pop(key, None)
        if old:
            self.cache_bytes -= old[3]
        if size <= 2 * 1024 * 1024 and version == self.store.version:
            self.cache[key] = (version, time.monotonic() + 30, text, size)
            self.cache_bytes += size
        while len(self.cache) > 128 or self.cache_bytes > 8 * 1024 * 1024:
            self.cache_bytes -= self.cache.popitem(last=False)[1][3]
        return text

    async def page(self, request, file):
        if file not in PUBLIC:
            response = web.FileResponse(self.root / file)
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
            return response
        async def build():
            html = (self.root / file).read_text(encoding="utf-8")
            title, description = PUBLIC[file]
            url = self.base + ("/" if file == "index.html" else "/" + file[:-5])
            html = re.sub(r"<title>.*?</title>", metadata(title, description, url), html, count=1, flags=re.S)
            if file == "index.html" and self.api.config.federation_role != "mirror":
                # Seed Vue using an inert HTML attribute (no executable inline JS).
                data = json.loads((await self.api.catalogue(request)).body)
                cards = []
                for p in data["projects"]:
                    ref = p["resource"]
                    href = "/projects/" + ref["provider"] + "/" + ref["project_id"]
                    authors = "、".join(a["name"] for a in p["metadata"].get("authors", []))
                    cards.append(f'<article class="card resource-card"><div class="project-icon">{escape(p["title"][:1])}</div><div class="resource-main"><a class="resource-title" href="{escape(href)}">{escape(p["title"])}</a><div class="resource-author">{escape(authors)}</div><p class="resource-summary">{escape(p.get("localized_summary") or p["summary"])}</p><span class="tag">{LABELS.get(p["kind"], "资源")}</span></div></article>')
                html = html.replace('<section id="projects" class="resource-list"></section>', '<section id="projects" class="resource-list">' + "".join(cards) + '</section><div id="catalog-seed" hidden data-catalog="' + escape(json.dumps(data, ensure_ascii=False), quote=True) + '"></div>')
            return html
        # Search strings and cursor combinations never enter the HTML cache.
        if request.query_string:
            response = self.response(request, await build())
            response.headers["X-Robots-Tag"] = "noindex, follow"
            return response
        return self.response(request, await self.cached(file, build), cache=True)

    async def project(self, request):
        from .api import ApiError
        self.api.rate(request, "read")
        if self.api.config.federation_role == "mirror":
            return web.Response(status=302, headers={"Location": self.base + request.path_qs})
        resource = resource_ref(request.match_info["provider"], request.match_info["project_id"])
        language = normalize_language(request.query.get("target_language", DEFAULT_LANGUAGE))
        async def build():
            data = await self.api.detail_data(resource, language)
            source, fields = data["source"], {v["field"]: v for v in data["fields"]}
            title, meta = source["fields"]["title"]["text"], source["metadata"]
            values = {name: fields[name].get("translation", {}).get("text", source["fields"][name]["text"]) for name in fields}
            ready = all(v["state"] == "ready" and v["is_current"] for v in fields.values())
            status = LANGUAGES[language][0] + " · 简介与正文已就绪" if ready else "部分译文尚未就绪，显示可用译文及原文"
            if ready and all(v["translation"].get("translation_required") is False for v in fields.values()):
                status = "无需翻译 · 内容已就绪，保留原文"
            path = "/projects/" + resource["provider"] + "/" + resource["project_id"]
            url = self.base + path + ("?target_language=" + language if language != DEFAULT_LANGUAGE else "")
            html = (self.root / "project.html").read_text(encoding="utf-8")
            description = bleach.clean(values["summary"], tags=set(), strip=True)[:180]
            info = [("资源类型", LABELS.get(source["kind"], "资源")), ("作者", "、".join(a["name"] for a in meta.get("authors", []))), ("更新时间", meta.get("updated_at", "")), ("创建时间", meta.get("created_at", "")), ("加载器", " / ".join(meta.get("loaders", []))), ("Minecraft 版本", " / ".join(meta.get("game_versions", []))), ("许可证", str(meta.get("license") or "原站未提供"))]
            options = "".join(f'<option value="{code}"'+(' selected' if code == language else '')+f'>{escape(names[0])}</option>' for code, names in LANGUAGES.items())
            body_format = fields["body"].get("translation", {}).get("format", source["fields"]["body"]["format"])
            substitutions = {"HEAD": metadata(title + " · " + LABELS.get(source["kind"], "资源") + "介绍与翻译 · Buggcraft", description, url), "TITLE": escape(title), "SUMMARY": escape(values["summary"]), "STATUS": escape(status), "BODY": render_body(values["body"], body_format), "ORIGINAL": render_body(source["fields"]["body"]["text"], source["fields"]["body"]["format"]), "SOURCE_URL": escape(source["source_url"], quote=True), "INFO": "".join(f'<dt>{escape(k)}</dt><dd>{escape(str(v))}</dd>' for k, v in info), "TAGS": "".join('<span class="tag">'+escape(t)+'</span>' for t in meta.get("tags", [])), "OPTIONS": options, "SEED": escape(json.dumps(data, ensure_ascii=False), quote=True)}
            substitutions["RELEASES"] = releases_html(meta)
            return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: substitutions[m[1]], html)
        try:
            text = await self.cached((request.path, language), build)
        except ApiError as exc:
            if exc.status != 404:
                raise
            response = self.response(request, '<!doctype html><html lang="zh-CN"><title>项目未收录</title><main><h1>项目未收录</h1><a href="/">返回译文库</a></main></html>', status=404)
            response.headers["X-Robots-Tag"] = "noindex"
            return response
        return self.response(request, text, cache=True)

    async def robots(self, request):
        return self.response(request, "User-agent: *\nAllow: /\nDisallow: /v1/\nDisallow: /admin\nDisallow: /assets/examples/\nSitemap: " + self.base + "/sitemap.xml\n", "text/plain", cache=True)

    def xml(self, request, items, index=False):
        container, item, namespace = ("sitemapindex", "sitemap", "http://www.sitemaps.org/schemas/sitemap/0.9") if index else ("urlset", "url", "http://www.sitemaps.org/schemas/sitemap/0.9")
        text = '<?xml version="1.0" encoding="UTF-8"?>' + f'<{container} xmlns="{namespace}">' + "".join(f'<{item}><loc>{escape(url)}</loc></{item}>' for url in items) + f'</{container}>'
        return self.response(request, text, "application/xml", cache=True)

    async def sitemap_index(self, request):
        count = (await self.store.one("SELECT COUNT(*) AS n FROM catalogue"))["n"]
        return self.xml(request, [self.base + "/sitemaps/pages.xml"] + [self.base + f"/sitemaps/projects-{n}.xml" for n in range(1, math.ceil(count / 1000) + 1)], index=True)

    async def sitemap_pages(self, request):
        return self.xml(request, [self.base + path for path in ("/", "/docs", "/network", "/developers")])

    async def sitemap_projects(self, request):
        page = int(request.match_info["page"])
        count = (await self.store.one("SELECT COUNT(*) AS n FROM catalogue"))["n"]
        if not 1 <= page <= math.ceil(count / 1000):
            raise web.HTTPNotFound()
        rows = await self.store.rows("SELECT r.ref FROM catalogue c JOIN resources r ON r.key=c.key ORDER BY c.key LIMIT 1000 OFFSET ?", ((page - 1) * 1000,))
        refs = [json.loads(row["ref"]) for row in rows]
        return self.xml(request, [self.base + "/projects/" + r["provider"] + "/" + r["project_id"] for r in refs])
