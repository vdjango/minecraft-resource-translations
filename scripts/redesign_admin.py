"""Build independent management pages from the existing shared visual shell."""
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1] / "src/mc_translate/web"
path = root / "admin.html"
html = path.read_text(encoding="utf-8").replace('src="assets/', 'src="/assets/').replace('href="assets/', 'href="/assets/')
html = html.replace('href="./"', 'href="/"').replace('href="docs"', 'href="/docs"')
pages = [
    ("dashboard", "/admin", "运行概览"),
    ("model-section", "/admin/model", "模型与额度"),
    ("tasks-section", "/admin/tasks", "翻译任务"),
    ("usage-section", "/admin/usage", "用量与费用"),
    ("mail-section", "/admin/mail", "邮件通知"),
    ("client-section", "/admin/clients", "接入申请"),
    ("feedback-section", "/admin/feedback", "译文反馈"),
]
html = re.sub(r'<aside class="admin-sidebar">.*?</aside>', '<aside class="admin-sidebar"><span class="eyebrow">工作区</span>' + ''.join(f'<a href="{url}">{label}</a>' for _, url, label in pages) + '<div class="sidebar-bottom">Buggcraft Translate</div></aside>', html, flags=re.S)
html = html.replace('<h1>平台控制台</h1>', '<h1 id="page-title">平台控制台</h1>')
html = html.replace("令牌仅保存在当前页面内存。", "令牌仅用于建立浏览器管理会话，一小时后过期。")
html = re.sub(r'<p class="muted small">用量按 UTC.*?</details>', '<p id="budget-notice" class="muted small"></p>', html)
html = html.replace('<option>none</option><option>minimal</option><option>low</option><option>medium</option><option>high</option><option>xhigh</option>', ''.join(f'<option value="{value}">{label}</option>' for value,label in [("none","关闭推理"),("minimal","极简"),("low","低"),("medium","中"),("high","高"),("xhigh","极高")]))
prices = '<h3 class="form-divider">当前模型费用 · 美元 / 百万 Token</h3><p class="muted small">填写实际服务商价格；留空表示费用未知。历史调用保留当时计价结果。</p><div class="form-grid three"><label>输入<input id="price-input" type="number" min="0" step="0.000001"></label><label>缓存输入<input id="price-cached" type="number" min="0" step="0.000001"></label><label>输出<input id="price-output" type="number" min="0" step="0.000001"></label></div>'
html = html.replace('<div class="form-footer">', prices + '<div class="form-footer">', 1)
records = '<section id="tasks-section" class="admin-section"><div class="section-heading"><h2>翻译任务</h2><button id="refresh-tasks" class="secondary">刷新</button></div><label class="filter-label">任务状态<select id="task-filter"><option value="">全部</option><option value="ready">成功</option><option value="failed">失败</option><option value="deferred">延后重试</option><option value="queued">排队中</option><option value="running">翻译中</option></select></label><p id="task-budget" class="muted small"></p><div id="tasks" class="admin-list"></div><button id="more-tasks" class="secondary hidden">加载更多任务</button></section><section id="usage-section" class="admin-section"><div class="section-heading"><h2>用量与费用</h2><button id="refresh-usage" class="secondary">刷新</button></div><div id="usage-summary" class="admin-metrics"></div><label class="filter-label">统计周期<select id="usage-period"><option value="day">今天</option><option value="month">本月</option></select></label><p class="muted small">按北京时间统计；缓存输入不重复计费，推理 Token 包含在输出中。费用为按配置价格的估算，最终以服务商账单为准。</p><div id="usage-calls" class="admin-list"></div><button id="more-usage" class="secondary hidden">加载更多调用</button></section>'
html = html.replace('<section id="mail-section"', records + '<section id="mail-section"')
# Each URL serves only its own section; no hidden stack of other settings.
sections = re.findall(r'<section id="(?:dashboard|model-section|tasks-section|usage-section|mail-section|client-section|feedback-section)".*?</section>', html, re.S)
for section_id, url, label in pages:
    page = html
    for section in sections:
        if not section.startswith(f'<section id="{section_id}"'):
            page = page.replace(section, '')
    page = page.replace('<h1 id="page-title">平台控制台</h1>', f'<h1 id="page-title">{label}</h1>')
    page = page.replace(f'<a href="{url}">{label}</a>', f'<a class="active" aria-current="page" href="{url}">{label}</a>')
    if section_id != "mail-section":
        page = page.replace('<script src="/assets/admin-mail.js" defer></script>', '')
    (root / ('admin.html' if url == '/admin' else 'admin-' + url.rsplit('/',1)[1] + '.html')).write_text(page, encoding="utf-8")
