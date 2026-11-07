"""Public crawlable HTML. Only indexed content is read; crawlers never queue jobs."""

import hashlib
import json
import math
import os
import re
import time
from urllib.parse import urlparse
from collections import OrderedDict
from html import escape
from pathlib import Path

import bleach
from aiohttp import web
from markdown_it import MarkdownIt

from .hashing import resource_ref
from .languages import DEFAULT_LANGUAGE, LANGUAGES, normalize_language
from .spa_shell import render_shell

LABELS = dict(mod="模组", resourcepack="资源包", shader="光影", modpack="整合包")
SEO = json.loads(Path(__file__).with_name("public-seo.json").read_text(encoding="utf-8"))
PUBLIC = {key: (value["title"], value["description"]) for key, value in SEO["pages"].items()}


def metadata(title, description, url, keywords=""):
    verification = '<meta name="baidu-site-verification" content="codeva-qOlOp7MRLB" />' if url == "https://mirror.bugg.icu/" else ""
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "SoftwareApplication", "@id": SEO["product"], "name": "Buggcraft", "alternateName": "BUGG启动器",
         "url": "https://bugg.icu/", "applicationCategory": "GameApplication", "operatingSystem": "Windows, macOS, Linux",
         "sameAs": ["https://github.com/vdjango/buggcraft"]},
        {"@type": "WebPage", "@id": url+"#page", "url": url, "name": title, "description": description,
         "about": {"@id": SEO["product"]}}]}
    structured = json.dumps(graph, ensure_ascii=False).replace("<", "\\u003c")
    return (verification + f'<title>{escape(title)}</title><meta name="description" content="{escape(description, quote=True)}">'
            f'<meta name="keywords" content="{escape(keywords, quote=True)}">'
            f'<link rel="canonical" href="{escape(url, quote=True)}">'
            f'<meta property="og:type" content="website"><meta property="og:title" content="{escape(title, quote=True)}">'
            f'<meta property="og:description" content="{escape(description, quote=True)}"><meta property="og:url" content="{escape(url, quote=True)}">'
            f'<meta name="twitter:card" content="summary"><meta name="twitter:title" content="{escape(title, quote=True)}">'
            f'<meta name="twitter:description" content="{escape(description, quote=True)}">'
            f'<script type="application/ld+json">{structured}</script>')


def render_body(text, format):
    if format == "plain":
        return "<p>" + escape(text).replace("\n", "<br>") + "</p>"
    html = MarkdownIt("commonmark", {"html": True}).enable("table").render(text) if format == "markdown" else text
    tags = set(bleach.sanitizer.ALLOWED_TAGS) | {"p", "div", "span", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "img", "table", "thead", "tbody", "tr", "th", "td", "details", "summary", "del", "s", "kbd"}
    def attr(tag, name, value):
        if tag == "img" and name in ("width", "height"):
            return len(value) <= 5 and value.isascii() and value.isdigit() and 0 < int(value) <= 10000
        if name in ("alt", "title", "colspan", "rowspan"):
            return True
        # HTTPS media only; links may be relative to the original source.
        return name == "href" or name == "src" and value.startswith("https://")
    clean = bleach.clean(html, tags=tags, attributes=attr, protocols={"https", "http", "mailto"}, strip=True)
    return clean.replace("<img ", '<img loading="lazy" decoding="async" referrerpolicy="no-referrer" ')


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
        self.mirror_base = os.environ.get("TRANSLATION_MIRROR_PUBLIC_URL", self.base).rstrip("/")
        if urlparse(self.mirror_base).scheme not in ("http", "https"):
            raise ValueError("Invalid mirror public URL")
        self.cache, self.cache_bytes = OrderedDict(), 0

    def mirror_home(self, request):
        return self.mirror_base != self.base and request.host.split(":")[0].lower() == urlparse(self.mirror_base).hostname

    def origin(self, request):
        return self.mirror_base if self.mirror_home(request) else self.base

    def response(self, request, text, content_type="text/html", status=200, cache=False):
        if content_type == "text/html" and status == 200:
            # Bootstrap from this live response, independent of the cached page body.
            # A mirror can route writes safely without probing the hub first.
            service = '译文节点在线' if self.api.config.federation_role == 'mirror' else '翻译服务在线' if self.api.config.api_key else '译文查询在线 · 生成暂不可用'
            text = text.replace('</head>', '<meta name="translation-hub" content="' + escape(self.base, quote=True) + '"></head>', 1)
            # The cached application shell carries the initial Vue state. Keep
            # its service indicator current when configuration changes.
            text = re.sub(r'("serviceState":)"(?:\\.|[^"\\])*"',
                          lambda match: match.group(1) + json.dumps(service, ensure_ascii=False),
                          text, count=1)
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

    async def cached(self, key, build, *, ttl=30, versioned=True):
        entry = self.cache.get(key)
        if entry and (not versioned or entry[0] == self.store.version) and entry[1] > time.monotonic():
            self.cache.move_to_end(key)
            return entry[2]
        version = self.store.version
        text = await build()
        size = len(text.encode("utf-8"))
        old = self.cache.pop(key, None)
        if old:
            self.cache_bytes -= old[3]
        if size <= 2 * 1024 * 1024 and version == self.store.version:
            self.cache[key] = (version, time.monotonic() + ttl, text, size)
            self.cache_bytes += size
        while len(self.cache) > 128 or self.cache_bytes > 8 * 1024 * 1024:
            self.cache_bytes -= self.cache.popitem(last=False)[1][3]
        return text

    async def page(self, request, file):
        if file not in PUBLIC:
            return web.Response(status=404)
        mirror_home = self.mirror_home(request)
        if mirror_home and file == "mirror":
            raise web.HTTPMovedPermanently(location="/")
        if mirror_home and file == "index":
            file = "mirror"
        page_base = self.base if file == "index" else self.mirror_base
        mode = "mirror" if mirror_home else "translation"
        async def build():
            title, description = PUBLIC[file]
            url = page_base + ("/" if file == "index" or file == "mirror" and self.mirror_base != self.base else "/" + file)
            seed = {"publicMode": mode, "publicOrigin": self.origin(request),
                    "mirrorBase": self.mirror_base, "translationBase": self.base}
            if file == "index" and self.api.config.federation_role != "mirror":
                data = json.loads((await self.api.catalogue(request)).body)
                seed["catalogueData"] = json.dumps(data, ensure_ascii=False)
            seed["serviceState"] = ('译文节点在线' if self.api.config.federation_role == 'mirror' else
                                    '翻译服务在线' if self.api.config.api_key else '译文查询在线 · 生成暂不可用')
            return render_shell(self.root, file, metadata(title, description, url, SEO["pages"][file]["keywords"]), seed=seed)
        # Search strings and cursor combinations never enter the HTML cache.
        if request.query_string:
            response = self.response(request, await build())
            response.headers["X-Robots-Tag"] = "noindex, follow"
            return response
        # The catalogue is a read-heavy public entry point. A short independent
        # cache keeps task progress from rebuilding the full SSR card list on
        # every hit, while the five-second bound keeps new translations visible.
        if file == "index":
            return self.response(request, await self.cached((file, mode), build, ttl=5, versioned=False), cache=True)
        return self.response(request, await self.cached((file, mode), build), cache=True)

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
            description = bleach.clean(values["summary"], tags=set(), strip=True)[:180]
            info = [("资源类型", LABELS.get(source["kind"], "资源")), ("作者", "、".join(a["name"] for a in meta.get("authors", []))), ("更新时间", meta.get("updated_at", "")), ("创建时间", meta.get("created_at", "")), ("加载器", " / ".join(meta.get("loaders", []))), ("Minecraft 版本", " / ".join(meta.get("game_versions", []))), ("许可证", str(meta.get("license") or "原站未提供"))]
            options = "".join(f'<option value="{code}"'+(' selected' if code == language else '')+f'>{escape(names[0])}</option>' for code, names in LANGUAGES.items())
            body_format = fields["body"].get("translation", {}).get("format", source["fields"]["body"]["format"])
            seed = {"title": title, "summary": values["summary"], "status": status,
                    "body": render_body(values["body"], body_format),
                    "original": render_body(source["fields"]["body"]["text"], source["fields"]["body"]["format"]),
                    "sourceUrl": source["source_url"],
                    "info": "".join(f'<dt>{escape(k)}</dt><dd>{escape(str(v))}</dd>' for k, v in info),
                    "tags": "".join('<span class="tag">'+escape(t)+'</span>' for t in meta.get("tags", [])),
                    "options": options, "detail": json.dumps(data, ensure_ascii=False),
                    "releases": releases_html(meta)}
            page_title = title + " - " + LABELS.get(source["kind"], "资源") + "中文介绍 - Buggcraft"
            seed.update(mirrorBase=self.mirror_base, translationBase=self.base,
                        seo=json.dumps({"title": page_title, "description": description, "url": url}, ensure_ascii=False))
            head = metadata(page_title, description, url, "Buggcraft,Minecraft," + LABELS.get(source["kind"], "资源") + "," + title)
            return render_shell(self.root, "project", head, seed=seed)
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
        return self.response(request, "User-agent: *\nAllow: /\nDisallow: /v1/\nDisallow: /api/\nDisallow: /mirror-api/\nDisallow: /admin\nDisallow: /account\nDisallow: /login\nDisallow: /assets/examples/\nSitemap: " + self.origin(request) + "/sitemap.xml\n", "text/plain", cache=True)

    def xml(self, request, items, index=False):
        container, item, namespace = ("sitemapindex", "sitemap", "http://www.sitemaps.org/schemas/sitemap/0.9") if index else ("urlset", "url", "http://www.sitemaps.org/schemas/sitemap/0.9")
        text = '<?xml version="1.0" encoding="UTF-8"?>' + f'<{container} xmlns="{namespace}">' + "".join(f'<{item}><loc>{escape(url)}</loc></{item}>' for url in items) + f'</{container}>'
        return self.response(request, text, "application/xml", cache=True)

    async def sitemap_index(self, request):
        if self.mirror_home(request):
            return self.xml(request, [self.mirror_base + path for path in ("/", "/docs", "/developers")])
        count = (await self.store.one("SELECT COUNT(*) AS n FROM catalogue c JOIN resources r ON r.key=c.key WHERE trim(c.title)<>'' AND trim(c.summary)<>''"))["n"]
        return self.xml(request, [self.base + "/sitemaps/pages.xml"] + [self.base + f"/sitemaps/projects-{n}.xml" for n in range(1, math.ceil(count / 1000) + 1)], index=True)

    async def sitemap_pages(self, request):
        if self.mirror_home(request):
            return self.xml(request, [self.mirror_base + path for path in ("/", "/docs", "/developers")])
        if self.mirror_base != self.base:
            return self.xml(request, [self.base + "/"])
        return self.xml(request, [self.base + path for path in ("/", "/mirror", "/docs", "/developers")])

    async def sitemap_projects(self, request):
        if self.mirror_home(request):
            raise web.HTTPNotFound()
        page = int(request.match_info["page"])
        count = (await self.store.one("SELECT COUNT(*) AS n FROM catalogue c JOIN resources r ON r.key=c.key WHERE trim(c.title)<>'' AND trim(c.summary)<>''"))["n"]
        if not 1 <= page <= math.ceil(count / 1000):
            raise web.HTTPNotFound()
        rows = await self.store.rows("SELECT r.ref FROM catalogue c JOIN resources r ON r.key=c.key WHERE trim(c.title)<>'' AND trim(c.summary)<>'' ORDER BY c.key LIMIT 1000 OFFSET ?", ((page - 1) * 1000,))
        refs = [json.loads(row["ref"]) for row in rows]
        return self.xml(request, [self.base + "/projects/" + r["provider"] + "/" + r["project_id"] for r in refs])
