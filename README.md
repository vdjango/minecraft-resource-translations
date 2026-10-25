<p align="center">
  <img src="src/mc_translate/web/logo.svg" width="96" height="96" alt="Buggcraft Translate">
</p>

<h1 align="center">Buggcraft Minecraft Resource Translations</h1>

<p align="center">面向 Minecraft 启动器与资源目录的版本化、多语言翻译服务。</p>

<p align="center">
  <a href="https://github.com/vdjango/minecraft-resource-translations/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/vdjango/minecraft-resource-translations/ci.yml?label=CI" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg" alt="AGPL-3.0 license"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg?logo=python&logoColor=white" alt="Python 3.11 or newer"></a>
  <a href="src/mc_translate/contracts/openapi.json"><img src="https://img.shields.io/badge/API-OpenAPI%203.1-6BA539.svg" alt="OpenAPI 3.1"></a>
</p>

[![CI](https://img.shields.io/github/actions/workflow/status/vdjango/minecraft-resource-translations/ci.yml?label=CI)](https://github.com/vdjango/minecraft-resource-translations/actions) [![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/) [![OpenAPI](https://img.shields.io/badge/API-OpenAPI%203.1-6BA539.svg)](src/mc_translate/contracts/openapi.json)

一个面向 Minecraft 启动器和资源网站的共享翻译平台。它把 Modrinth、CurseForge 的模组、资源包、光影和整合包介绍按项目 ID、原文哈希、目标语言缓存起来：命中时快速返回译文，未命中时先显示原文并在后台翻译。

[在线译文库](https://translate.bugg.icu/) · [在线开放文档](https://translate.bugg.icu/docs) · [社区节点页面](https://translate.bugg.icu/network) · [GitHub](https://github.com/vdjango/minecraft-resource-translations)

本项目按 AGPL-3.0-or-later 发布，欢迎提交 Issue、改进接口和加入社区节点。源代码、API 合约和示例都在同一个仓库中；模型服务只在生成译文时使用，读取已有译文不需要模型 Key。

## 你可以怎么使用

| 目标 | 推荐方式 | 是否需要模型 Key |
| --- | --- | --- |
| 只想接入现成的共同翻译服务 | 使用公共 API，或运行启动器示例 | 不需要 |
| 想拥有自己的翻译服务和额度 | 部署 `hub` 主服务 | 生成新译文时需要 |
| 有服务器，想分担公共服务读取流量 | 部署 `mirror` 社区镜像并申请加入 | 不需要 |

社区镜像只负责就近读取共享译文和缓存，不重复调用模型。用户端从主服务取得可信节点列表，并行测速后选择较近节点；节点离线、超时或被撤销时自动回到主服务。翻译生成、任务、预算、密钥申请和管理始终由主服务负责。

## 功能

- 资源库：按作者、时间、标签、游戏版本、加载器和依赖展示项目。
- 自动翻译：列表和详情先显示原文，译文完成后自动替换；保留 Markdown / HTML 排版并清洗危险标签。
- 多语言：默认简体中文，语言按缓存、任务和页面隔离。
- 更新识别：项目身份稳定，标题、作者、创建时间和字段哈希分开保存；原文变化后只补译变化字段，并提示“正在准备译文”。
- 无需翻译：项目原名、空内容、纯代码和已经是目标语言的内容保留原文，不调用模型。
- 失败恢复：读取失败结果不阻塞下次访问；客户端再次 `ensure` 会按冷却、队列和预算重试。
- 统计后台：今日请求、启动器实例估算、接入应用、字段命中率、模型调用、接口错误、p95 延迟和节点请求分开统计。
- 联邦节点：申请、审批、心跳、在线发现、就近测速、只读缓存、正文 SHA-256 校验和故障回退。

## 最快开始：自己运行主服务

环境要求：Python 3.11+、SQLite 3.34+（FTS5 和 trigram）。

```sh
# Linux / macOS
python -m venv .venv
. .venv/bin/activate
cp .env.example .env

# Windows PowerShell 使用：
# .venv\Scripts\Activate.ps1
# Copy-Item .env.example .env

python -m pip install -e .
python -m mc_translate
```

编辑 `.env`：

```dotenv
TRANSLATION_PUBLIC_URL=http://127.0.0.1:18448
TRANSLATION_DATABASE=data/translations.sqlite3
TRANSLATION_BASE_URL=https://你的模型服务/v1
TRANSLATION_API_KEY=你的模型服务Key
TRANSLATION_MODEL=gpt-6.1-sol
TRANSLATION_ADMIN_TOKEN=随机生成的后台令牌
```

打开：

- 资源库：<http://127.0.0.1:18448/>
- 开放文档：<http://127.0.0.1:18448/docs>
- 后台：<http://127.0.0.1:18448/admin>
- 健康检查：<http://127.0.0.1:18448/health>

没有配置模型 Key 也可以读取已有译文，但无法生成新的模型译文。生产环境请使用 HTTPS 反向代理。

### Linux 服务安装（Ubuntu / Debian）

CI 会构建可安装的 `.deb`。下载对应版本的包后，直接用 apt 安装，安装脚本会创建专用系统用户、虚拟环境、systemd 服务和随机后台令牌：

```sh
wget https://github.com/vdjango/minecraft-resource-translations/releases/latest/download/buggcraft-translation_0.1.2_all.deb
sudo apt install ./buggcraft-translation_0.1.2_all.deb
sudo systemctl status buggcraft-translation
sudoedit /etc/buggcraft-translation.env
```

本地构建包需要 Debian 系列系统的 `dpkg-deb`：`bash scripts/build_deb.sh`。服务默认只监听本机；公开部署时把 `TRANSLATION_HOST` 改为 `0.0.0.0`，再用 HTTPS 反向代理暴露 `/`。

### Windows 服务安装

CI 会同时提供 `mc-translate-windows.zip`。解压到固定目录，以管理员 PowerShell 执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install-windows-service.ps1
Get-Service BuggcraftTranslation
```

脚本生成本地 `.env`、注册 `BuggcraftTranslation` 服务并设置开机启动。模型 Key、SMTP 密码和管理员令牌只写入该目录的配置文件，不会打包进 exe。需要手动运行时直接执行 `mc-translate.exe`。

## 用 Docker 部署

```sh
cp .env.example .env
# 编辑 .env 后执行
docker compose up -d --build
docker compose logs -f translation
```

[Dockerfile](Dockerfile)、[Compose 配置](compose.yaml) 和 [Nginx 示例](deploy/nginx.conf) 已包含在仓库中。数据库、运行配置和节点凭据必须放在持久数据卷中。

## 加入公共共享翻译网络

完整部署教程见 [联邦节点指南](docs/federation.md)。流程只有四步：

1. 准备 Docker、域名和 HTTPS，把仓库部署到自己的服务器。
2. 在 `.env` 设置 `TRANSLATION_FEDERATION_ROLE=mirror`、公共地址、主服务地址、节点名称和区域。
3. `docker compose up -d --build`，节点会自动向主服务申请并保存自己的节点令牌。
4. 主服务管理员在 `/admin/nodes` 验证并启用；节点收到心跳确认后自动加入公开列表。

镜像节点配置示例：

```dotenv
TRANSLATION_FEDERATION_ROLE=mirror
TRANSLATION_FEDERATION_HUB=https://translate.bugg.icu
TRANSLATION_PUBLIC_URL=https://你的节点域名
TRANSLATION_FEDERATION_NAME=我的社区镜像
TRANSLATION_FEDERATION_REGION=cn-east
TRANSLATION_FEDERATION_CONTACT=你的联系邮箱
TRANSLATION_FEDERATION_CACHE_MB=512
TRANSLATION_API_KEY=
```

节点不需要主服务的 API Key，也不能接收主服务的管理令牌。首次登记生成的 `federation-node.json` 只保存在自己的数据目录，不能提交到 Git 或公开下载。节点连续 90 秒没有心跳会自动退出发现列表；管理员也可以立即撤销。

实验室局域网可以临时设置 `TRANSLATION_FEDERATION_ALLOW_PRIVATE=true`，正式公网节点必须使用 HTTPS 和公共地址。

## API 接入

完整协议见 [OpenAPI 3.1](src/mc_translate/contracts/openapi.json) 和 [在线文档](https://translate.bugg.icu/docs)。最常用的流程：

```text
获取原站内容 → 计算 mc-text-v1 原文哈希 → POST translations:resolve
                                  ↓ 命中
                              立即显示译文
                                  ↓ 未命中
                         显示原文 → POST translations:ensure
                                  ↓
                          轮询任务 / 订阅事件
```

主要接口：

| 接口 | 用途 | 认证 |
| --- | --- | --- |
| `GET /v1/capabilities` | 语言、限制和服务能力 | 无 |
| `POST /v1/translations:resolve` | 批量读取译文，最多 50 个项目 | 公共读取 |
| `POST /v1/translations:ensure` | 创建或复用翻译任务，最多 20 个项目 | 会话或应用 Key |
| `GET /v1/jobs/{job_id}` | 查询当前调用者的任务 | 任务所有者 |
| `GET /v1/artifacts/{sha256}` | 读取不可变正文 | 公共读取 |
| `GET /v1/federation/nodes` | 获取已批准在线节点 | 无 |
| `GET /v1/federation/health` | 节点健康与协议检查 | 无 |

跨语言示例位于 [src/mc_translate/web/examples](src/mc_translate/web/examples)，包含 Python、Java、C、C++、C#；可选的 [network_client.py](src/mc_translate/web/examples/network_client.py) 会自动测速并在读取失败时回退主服务。应用 Key 与模型 Key 完全独立。

## 性能和统计

服务把“服务端读取延迟”和“用户实际体验”分开：

- 译文查询使用批量 SQL、SQLite WAL、游标分页、语义字段缓存和相同冷请求合并。
- 聚合统计每 5 秒批量写入，不在每个请求中写数据库，不会拖慢读取。
- 目标是本机或同区域缓存查询 p95 10–20ms；公网 TLS、玩家到节点的网络往返、首次回源和模型生成不属于这个目标。
- 当前合成测试：数据库冷路径 500 次、并发 5、每批 20 字段，p50 16.89ms、p95 18.81ms、p99 20.89ms、零错误；实际硬件和网络请运行自己的压测。

登录 `/admin/traffic` 查看今日请求、启动器实例（上报与估算分开）、接入应用、译文字段命中率、模型调用、错误、接口 p95 和趋势图。`/admin/nodes` 查看节点申请、在线状态和节点自己上报的请求量；两种统计不会混为用户数量。

运行压测：

```sh
python scripts/benchmark.py --requests 1000 --concurrency 20 --cold
python scripts/benchmark.py --requests 1000 --concurrency 20 --unique-ids
python scripts/benchmark_catalogue.py --projects 100000 --requests 100
```

`--cold` 测数据库读取，`--unique-ids` 测不同请求编号但可复用语义缓存。首次模型生成会受上游服务、预算和队列影响。

## 管理后台

使用 `TRANSLATION_ADMIN_TOKEN` 登录。浏览器建立一小时 HttpOnly 会话，切换页面会先恢复会话，不会反复显示登录表单。

| 页面 | 功能 |
| --- | --- |
| `/admin` | 运行概览、预算和今日访问摘要 |
| `/admin/traffic` | 服务访问、命中率、延迟和模型调用统计 |
| `/admin/nodes` | 社区节点审批、验证、撤销和在线状态 |
| `/admin/model` | 模型列表、推理档位、预算和单价 |
| `/admin/tasks` | 成功/失败筛选、错误详情和失败重试 |
| `/admin/usage` | Token、费用及调用状态 |
| `/admin/clients` | 启动器接入申请和应用 Key |
| `/admin/mail` | SMTP 通知 |

## 开发、测试与贡献

```sh
python -m unittest discover -s tests -v
python scripts/browser_regression.py
python scripts/browser_multilingual.py
python scripts/browser_management.py
python scripts/browser_admin_retries.py
```

联邦和统计的后端回归包含在测试集，测试不调用付费模型。完整结果见 [docs/test-report.md](docs/test-report.md)。提交 API 改动时请同步 OpenAPI、在线文档、跨语言示例和测试。

项目结构：`src/mc_translate` 是服务端、队列和 Web；`src/mc_translate/web/examples` 是接入示例；`tests` 是回归测试；`scripts` 是浏览器测试与压测；`docs` 是部署和扩容文档。

## 生产注意事项

- 一个 SQLite 数据库只运行一个主服务进程；需要多进程时先迁移 PostgreSQL、共享队列和对象存储。
- 不要把模型 Key、SMTP 密码、管理员令牌、应用 Key、节点令牌或 `.env` 提交到 Git。
- 主服务和节点都要保留数据卷并定期备份；节点缓存可以删除后重新回源。
- 节点只代理白名单公共读取接口，写入、任务和模型生成始终回主服务。
- 统计不保存原始 IP、User-Agent 或设备明文；实例数分为上报和估算，不能作为精确用户数或计费依据。
- 译文是机器生成内容，应保留原项目作者、来源链接和原文对照。

扩容和后续对象存储、PostgreSQL、社区节点边界见 [带宽与联邦节点规划](docs/scaling-and-federation.md)。

## 许可

本项目采用 [GNU AGPL-3.0](LICENSE)，与 Buggcraft 项目保持一致。第三方组件许可见 [THIRD-PARTY.txt](src/mc_translate/web/THIRD-PARTY.txt)，资源原文和图片归各自权利人所有。
