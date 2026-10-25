from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / "src/mc_translate/web/docs.html"
text = path.read_text(encoding="utf-8")
section = """<section id="authentication"><div class="section-number">ACCESS / AUTHENTICATION</div><h2>接入需要申请密钥吗？</h2><p>公开查询（resolve、source、catalog、artifact）无需申请；个人体验可继续使用一小时匿名会话。正式启动器可以在 <a href="developers">开发者申请页</a> 提交名称、主页、联系信息及用途，获得待审批密钥。密钥只展示一次，请立即保存。</p><ol><li><code>POST /v1/clients/applications</code> 提交 JSON：name、contact、website（HTTPS）、purpose。返回 application_id、access_token、state=pending。</li><li>携带 <code>Authorization: Bearer bct_…</code> 调用 <code>GET /v1/clients/application</code> 查看审批状态。</li><li>管理员批准后，原密钥生效；ensure、jobs、events、feedback 和 refresh 可复用此凭据，无需每小时重新创建匿名会话。</li></ol><p>待审批、驳回和撤销的密钥不能创建翻译任务，返回 403 client_key_pending / client_key_rejected / client_key_revoked。未知密钥返回 401 invalid_client_key。默认独立额度为每小时 1000 字段、每分钟 600 请求，管理员可调整；IP 防滥用限额与全局模型预算仍然适用。用量按申请字段数统计，重复幂等请求不重复计数。密钥仅以 SHA-256 存储，撤销后下次请求立即失效。</p><p>桌面启动器内置的共同密钥可被提取，不能把它当成用户身份凭据。需要保密的应用密钥应保存在开发者自己的后端，或让用户配置个人凭据。申请方密钥与平台上游 OpenAI 模型密钥完全独立。</p><h3>管理台</h3><p><a href="admin">平台管理</a> 使用独立管理员令牌。可修改 OpenAI 兼容 Base URL（包含 /v1）、API Key、模型、推理档位和预算。保存后供后续模型调用使用；已在执行的调用不会取消。密钥留空保留原值，读取设置只返回 api_key_configured，不回显密钥。运行配置原子保存到数据库同目录 runtime-config.json（Linux 权限 0600），重启后优先于环境配置；服务需要对该目录有写权限。备份应同时包含数据库与此私密文件。</p><pre><code>GET /v1/admin/settings
POST /v1/admin/settings
GET /v1/admin/clients?after=&lt;next_cursor&gt;
POST /v1/admin/clients
Authorization: Bearer &lt;管理员令牌&gt;

// 配置示例：省略 api_key 保留当前密钥
{"base_url":"https://api.example.org/v1","model":"gpt-6.1-sol","reasoning_effort":"medium"}
// 批准并设限额
{"id":"client_…","state":"active","fields_per_hour":1000,"requests_per_minute":600}</code></pre><h3>列表状态与正文状态分别判断</h3><p>catalog 的 <code>field_states.summary</code> 与 <code>field_states.body</code> 分别表示当前版本的就绪情况。<code>translation_state=ready</code> 表示两者都就绪；只有简介就绪时为 partial。localized_summary 存在不代表正文已经生成。详情页按 resolve 的具体字段显示状态，正文缺失时明确提示“正文正在准备，暂时显示原文”，不能把简介就绪显示成整篇就绪。</p></section>"""
if '<section id="authentication">' not in text:
    text = text.replace(
        '<section id="quickstart">', section + '\n<section id="quickstart">'
    )
    text = text.replace(
        '<a href="#quickstart">',
        '<a href="#authentication">接入密钥与管理</a><a href="#quickstart">',
        1,
    )
text = text.replace("API V1.1", "API V1.2")
path.write_text(text, encoding="utf-8")
