# 部署自己的服务，或加入共享译文网络

## 先选择一种运行方式

| 方式 | 适合谁 | 需要模型 Key | 谁生成译文 |
| --- | --- | --- | --- |
| 独立主服务 `hub` | 想使用自己的模型、预算与数据库 | 是，生成时需要 | 自己的服务 |
| 社区镜像 `mirror` | 有服务器，希望帮助公共服务分担读取流量 | 不需要 | 所加入的主服务 |

当前联邦实现是**共享读取与缓存网络**，已经包含节点自动申请、后台审批、所有权验证、心跳、在线节点发现、客户端测速、缓存分流和失败回退。它不会在多个节点重复调用模型，也不把主服务数据库或密钥复制到节点。

## 加入公共网络

### 1. 准备环境

准备 Docker Compose、一台可访问外网的服务器、自己的域名和有效 HTTPS 证书。将域名指向服务器。示例中的 `translations.example.org` 必须换成你的域名。

```sh
git clone https://github.com/vdjango/minecraft-resource-translations.git
cd minecraft-resource-translations
cp .env.example .env
```

### 2. 修改 `.env`

```dotenv
TRANSLATION_FEDERATION_ROLE=mirror
TRANSLATION_FEDERATION_HUB=https://translate.bugg.icu
TRANSLATION_PUBLIC_URL=https://translations.example.org
TRANSLATION_FEDERATION_NAME=我的社区镜像
TRANSLATION_FEDERATION_REGION=cn-east
TRANSLATION_FEDERATION_CONTACT=you@example.org
TRANSLATION_FEDERATION_CACHE_MB=512
TRANSLATION_API_KEY=
TRANSLATION_ADMIN_TOKEN=替换成自己生成的随机管理令牌
```

`region` 是部署者声明的区域提示，例如 `cn-east`、`eu-west`；实际选路以客户端测速为准。联系方式仅供主服务管理员处理申请，不出现在公开节点列表。管理令牌属于你自己的节点，不要填写主服务令牌。

生成自己的管理令牌：

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. 启动并配置反向代理

```sh
docker compose up -d --build
docker compose logs -f translation
```

容器默认只在宿主机 `127.0.0.1:18448` 提供服务。将自己的 HTTPS 域名反向代理到该地址，参考 [Nginx 示例](../deploy/nginx.conf)。域名根路径、`/v1/` 和 `/assets/` 均需正常代理，不要将 `/v1/` 配成静态页面。

### 4. 等待审批

节点启动后自动向主服务登记，并每 30 秒发送心跳。管理员在主服务 `/admin/nodes` 查看申请并点击“验证并启用”。主服务向申请地址发送随机挑战，验证部署者确实掌握该节点的凭据；验证失败不会启用。

批准后，节点在下一次心跳收到启用状态，通常不超过 30 秒。通过 `https://你的域名/v1/federation/health` 查看 `ready`；通过主服务 `/network` 查看分发列表和当前浏览器测速。

首次登记返回的节点令牌自动保存在数据目录的 `federation-node.json`。保留数据卷；不要提交此文件、复制到其他域名或公开下载。更换域名或主服务需重新建立节点身份，先撤销旧节点，并备份原数据。

### 5. 停止或退出

`docker compose stop` 会停止节点。超过 90 秒未收到心跳，或超过 120 秒未通过探测，主服务就不再公布该节点。管理员也可以立即撤销节点，将其移出发现列表；节点下一次心跳收到撤销状态后停止服务。

公开发现结果最多缓存 15 秒，已有客户端还可能持有旧选择；这段时间若请求失败，会自动回主服务。

## 数据如何流动

```text
                       ┌─ 已缓存 ───────→ 社区镜像直接响应
启动器 / 网页 → 测速选路 ┤
                       └─ 未缓存 ───────→ 主服务查询 → 镜像缓存

翻译申请、任务票据、反馈、应用密钥 ───────→ 主服务
源站核验、翻译队列、模型调用、预算 ───────→ 主服务
```

- 镜像只代理白名单中的公共资源读取接口，不是任意 URL 代理；回源只允许配置的主服务，不递归请求其他镜像。
- `resolve` 按语言、资源、原文哈希等语义缓存，相关请求编号不影响复用；相同冷请求合并为一次回源。
- 动态数据严格缓存 2 秒，不将过期状态伪装成最新。节点与主服务无法通信时返回错误，客户端回退或显示原文。
- 正文按 SHA-256 校验原始字节，再写入本地磁盘；磁盘缓存默认 512 MiB，达到上限淘汰旧文件。动态内存缓存上限为 32 MiB / 2048 项，等待回源最多 256 个不同键。
- 应用密钥、匿名会话令牌、Cookie 和幂等键不发送给镜像。生成和私有票据访问始终使用主服务。
- 镜像仍属于需要信任的运营方。TLS 和正文哈希校验并不等于客户端已经验证所有字段的发布者签名；当前版本不宣称提供零信任、签名译文或社区模型计算节点。

## 启动器如何就近选择

1. 从固定的可信主服务读取 `GET /v1/federation/nodes`，不能接受任意第三方传入的节点列表。
2. 对返回的少量候选节点并行请求 `/v1/federation/health`，确认 `hub_url`、节点 ID、协议与 `ready`。
3. 使用真实往返时间选择较快节点。区域字符串只用于候选顺序，主服务到节点的延迟不能替代玩家到节点的延迟。
4. 将公共 `resolve`、catalogue、manifest、source、history、artifact 读取发往选中的节点；私有访问和写入仍发主服务。
5. 节点请求超时或失败，立即回主服务，并冷却失败节点 30 秒；每 5 分钟或网络切换后重新测速。

网页已经内置这套选路。已发布启动器不会仅靠服务端升级自动获得所有客户端能力，需要客户端采用发现协议；原来的主服务地址继续兼容。

Python 可使用 [network_client.py](../src/mc_translate/web/examples/network_client.py)：

```python
from network_client import TranslationNetwork

network = TranslationNetwork("https://translate.bugg.icu")
network.select()  # 初始化时测速；后续可放到后台定期执行
result = network.call("translations:resolve", payload)
# 写入接口和 Authorization 始终自动使用主服务。
```

其他语言按照上述流程实现；原有 Python / Java / C / C++ / C# 的单服务接入示例继续有效。节点列表的 `expires_in` 是建议刷新时间，不是授权有效期。

## 统计与性能口径

- `/admin/traffic` 统计本服务实际收到的请求、字段命中、启动器实例估算、接入应用、错误和延迟；`/admin/nodes` 单独显示社区节点上报的当日请求及缓存命中。
- 节点回源与用户请求属于不同口径，不将二者相加当成独立用户。节点数字是节点自报的汇总，不用于计费或安全授权；跨节点实例数量不做不准确的精确去重承诺。
- 缓存查询的目标是服务端 p95 10–20ms，必须结合批量大小、并发、硬件和实测。第一次节点回源、公网 TLS / 网络往返、短词全库搜索及模型生成不属于这个目标。
- 更接近用户的节点主要减少网络往返和主服务读取压力；它不会让首次模型生成变成毫秒级，也不会自动分担主服务的模型费用。

## 测试网络

正式节点必须使用公共 HTTPS 地址。隔离的本地实验可显式设置 `TRANSLATION_FEDERATION_ALLOW_PRIVATE=true`，允许私有地址与 HTTP；该选项只用于开发测试，不要在公共主服务启用。

联测使用临时主服务和镜像，不需要真实模型 Key。后端覆盖申请、审批挑战、重复冷请求、哈希损坏、撤销、离线和权限隔离。浏览器与 Linux 测试记录见 [test-report.md](test-report.md)。
