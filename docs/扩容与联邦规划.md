# 带宽、缓存与联邦节点规划

这份文档描述平台在资源规模和接入方增加后如何扩展。当前生产实现是单进程 SQLite：一个数据库只能运行一个服务进程，异步 HTTP 读取、任务去重和多个翻译 worker 提供并发。不要把多个 Web worker 直接指向同一个 SQLite 队列。

## 当前请求链路

1. 启动器从 Modrinth / CurseForge 获取项目元数据和完整原始简介。
2. 启动器计算 `provider + project_id`、字段原文哈希，然后批量调用 `POST /v1/translations:resolve`。
3. resolve 只读取平台的资源快照、译文表和不可变 artifact，不请求原站，不下载模组 JAR、资源包 ZIP、视频或图片。
4. 命中当前哈希就返回译文；未命中先返回原文，并通过 `POST /v1/translations:ensure` 创建去重任务。
5. 后台 worker 按资源锁拉取一次权威元数据，核验哈希，再调用模型。完成后写入 artifact；同一原文哈希被其他启动器复用。

因此，未命中确实会产生一次平台到源站的元数据请求和一次模型调用，但不会把安装文件流量经过翻译平台。启动器仍负责图片和下载。资源快照有五分钟检查间隔，单项目并发请求合并；任务、模型预算和客户端额度也有上限。未命中高峰的主要压力是源站元数据、模型 token 和数据库写入，不是模组文件带宽。

## 第一阶段：低成本扩容

- 对公开的 `GET /v1/catalog/*`、项目 manifest、source 和 artifact 设置 `ETag`、`Cache-Control: public, s-maxage`，边缘节点只缓存内容寻址的 artifact；POST resolve/ensure 保持鉴权和短请求，不把用户令牌放入缓存键。
- artifact 使用 SHA-256 路径，响应体不可变；CDN 命中后不会重复访问应用和对象存储。缓存键必须包含版本路径和必要的语言/profile/context，不使用 Cookie 作为公开资源键。
- 将原站响应、resolve 结果、模型任务分别统计：`source_fetch_total`、`resolve_hit_ratio`、`artifact_hit_ratio`、`queue_wait_seconds`、`model_tokens`、`egress_bytes`。带宽异常时先区分源站回源、artifact 回源和启动器下载。
- 对批量 resolve 使用压缩 JSON、最多 50 个项目；客户端磁盘缓存按原文哈希复用，滚动列表只请求可见窗口。服务端拒绝超大 JSON 和超长 artifact。

Cloudflare 不会因为域名经过代理就自动缓存动态 JSON；应由响应头和缓存规则明确指定公共、不可变资源。带 `Authorization` 的请求也不应与公共匿名响应共用缓存键。见 [Cloudflare 默认缓存行为](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/) 与 [Cache-Control](https://developers.cloudflare.com/cache/concepts/cache-control/)。

## 第二阶段：共享队列和对象存储

当单机 CPU、SQLite 写入或队列等待成为瓶颈时，迁移边界如下：

| 部件 | 单机实现 | 横向扩容实现 |
| --- | --- | --- |
| 元数据与译文索引 | SQLite WAL | PostgreSQL，按 provider/project 分区或索引 |
| 任务领取 | SQLite 事务租约 | Redis Streams / RabbitMQ / 云队列，幂等键和 lease 必须保留 |
| artifact | SQLite BLOB | S3 兼容对象存储 + CDN，写入后校验哈希 |
| Web API | 一个 aiohttp 进程 | 无状态 API 多副本，负载均衡健康检查 `/health` |
| 源站拉取 | 每进程本地锁 | 共享任务去重；同一资源同一时间只允许一个 refresh |

先迁移 artifact 和只读 API，再迁移任务队列，最后迁移写入数据库。负载均衡不能解决多个独立 SQLite 写入者的问题。模型调用必须保留全局预算、并发上限、退避和熔断；扩机器不能绕过预算。

## 第三阶段：社区联邦节点

社区节点只做可验证的译文读取和 artifact 镜像，不共享平台模型密钥、管理员令牌、开发者申请资料、联系方式、原始私有任务或预算数据。

- 中央目录发布节点公钥、协议版本、能力、区域、最后同步时间和健康状态；公告使用 Ed25519 签名并设置 30 秒到期时间。
- 节点同步资源哈希、语言、profile、artifact 哈希和签名 manifest，不直接复制 SQLite。artifact 必须验证 `resource`、`field`、`source_hash`、目标语言、profile 和签名发布者；只有 SHA-256 不能证明发布者可信。
- 客户端优先使用本地或区域节点；节点 miss 最多询问两个已知 peer，禁止递归转发和无限重定向。失败时回中央节点或显示原文，并携带“正在准备译文”状态。
- 生成权保持在授权的 hub。独立社区生成器必须使用独立签名命名空间和管理员信任策略，不能把未经信任的翻译伪装成官方版本。
- 节点离线时返回已验证的旧 artifact 和 `is_current=false`，不能静默当作最新版。恢复后用 manifest digest 增量同步。

联邦上线前需要压测：节点选择 p95、命中率、跨节点 miss、签名验证耗时、同步延迟、artifact 出站流量和队列重复率。先做 CDN + 对象存储，再做只读镜像，最后才开放社区生成，避免把一致性和安全问题同时引入。

## 运行阈值

- `resolve` p95 超过 200 ms：检查数据库索引、JSON 压缩和边缘缓存命中。
- `artifact_hit_ratio` 低于 90%：检查 Cache-Control、ETag 和缓存键，不增加模型 worker。
- `queue_wait_seconds` 持续超过 120 秒：按 purpose 分层，详情优先；检查模型限额和源站限流。
- `source_fetch_total` 突增：启用单资源锁、退避和短时负缓存，禁止客户端每次刷新都强制 refresh。
- 出站带宽持续增长：确认平台没有代理安装文件和图片，artifact 是否启用 CDN，客户端是否按哈希缓存。
