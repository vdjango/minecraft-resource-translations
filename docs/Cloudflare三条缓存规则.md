# Cloudflare 三条缓存规则（bugg.icu）

更新：2026-11-01。与现有三个 Cache Rules 槽位对应；顺序固定为「公共静态资源 → 动态与后台绕过 → 镜像资源及下载站后台静态例外」。仅修改缓存资格，Edge TTL 选「有源站 Cache-Control 时遵循，否则不缓存」，Browser TTL 选「遵循源站」。不配置强制 TTL、Cache Everything 或自定义缓存键。Cloudflare 的同一设置以最后匹配的规则为准，所以第三条必须在第二条之后。

## 1. 公共静态资源

动作：Eligible for cache。表达式：

~~~text
(
  http.host in {
    "bugg.icu" "download.bugg.icu" "node.bugg.icu"
    "translate.bugg.icu" "mirror.bugg.icu" "mirror2.bugg.icu"
  }
  and http.request.uri.path.extension in {
    "js" "css" "png" "jpg" "jpeg" "webp"
    "avif" "svg" "ico" "woff" "woff2"
  }
  and not starts_with(http.request.uri.path, "/api/")
  and not starts_with(http.request.uri.path, "/v1/")
)
~~~

只覆盖网页脚本、样式、图片和字体，不把 exe、jar、zip 及无扩展名资源按后缀一概缓存。后台的 /admin/assets/ 将被下一条覆盖，下载站的该路径由第三条重新允许；它的版本参数或指纹由源站决定长期缓存，其余文件仍需向源站重新验证。

## 2. 接口、登录及后台绕过

动作：Bypass cache。表达式：

~~~text
(
  http.host in {
    "bugg.icu" "download.bugg.icu" "node.bugg.icu"
    "translate.bugg.icu" "mirror.bugg.icu" "mirror2.bugg.icu"
  }
  and (
    starts_with(http.request.uri.path, "/api/")
    or starts_with(http.request.uri.path, "/v1/")
    or starts_with(http.request.uri.path, "/admin")
    or starts_with(http.request.uri.path, "/login")
    or starts_with(http.request.uri.path, "/account")
    or starts_with(http.request.uri.path, "/network")
    or starts_with(http.request.uri.path, "/developers")
    or starts_with(http.request.uri.path, "/setup")
    or starts_with(http.request.uri.path, "/room-cache/")
    or starts_with(http.request.uri.path, "/mirror-api/")
    or starts_with(http.request.uri.path, "/_internal/")
  )
)
~~~

启动器更新检查 /api/launcher/update、发行清单 /api/launcher/releases、包地址 /api/launcher/assets/... 及下载票据均保持实时。旧启动器直下包和新启动器签名跳转都不通过 Cloudflare 缓存固定响应；七牛下载域名由它自己的存储/CDN 策略处理。房间临时文件、管理、同步任务、状态和身份接口不缓存。

## 3. 镜像公开文件与下载站后台静态资源

动作：Eligible for cache，源站未明确给出 Cache-Control 时不缓存。表达式：

~~~text
(
  (
    http.host eq "download.bugg.icu"
    and starts_with(http.request.uri.path, "/admin/assets/")
  )
  or
  (
    http.host in {"mirror.bugg.icu" "mirror2.bugg.icu"}
    and (
      starts_with(http.request.uri.path, "/assets/")
      or starts_with(http.request.uri.path, "/mc/game/")
      or starts_with(http.request.uri.path, "/java/v1/")
      or starts_with(http.request.uri.path, "/v1/packages/")
      or starts_with(http.request.uri.path, "/v1/objects/")
      or starts_with(http.request.uri.path, "/v1/products/java-runtime/")
      or starts_with(http.request.uri.path, "/maven/")
      or starts_with(http.request.uri.path, "/fabric-meta/")
      or starts_with(http.request.uri.path, "/quilt-meta/")
      or starts_with(http.request.uri.path, "/fabric-maven/")
      or starts_with(http.request.uri.path, "/quilt-maven/")
      or starts_with(http.request.uri.path, "/neoforge-maven/")
      or starts_with(http.request.uri.path, "/mirrors/authlib-injector/")
      or starts_with(http.request.uri.path, "/mods/")
      or starts_with(http.request.uri.path, "/_mirror_node/")
    )
  )
)
~~~

第三条使 Mojang 无扩展名 assets 与 JSON 清单也能按源站 Cache-Control 判断是否缓存；现有镜像进程给实体文件 public、五分钟或哈希文件一年，给重定向与回退 no-store，故节点选路不会被固化。/v1/packages/ 等资源覆盖第二条的广义 /v1/ 绕过；翻译 /v1/ 仍绕过。普通模组默认重定向，响应 no-store，不会因为本规则被强制缓存。mirror2.bugg.icu 曾因设置文件属主错误返回 500，2026-11-01 已修复并复测；Cache Rule 不能修复回源错误，仍需独立健康检查。

## 验收

- mirror.bugg.icu/mc/game/version_manifest_v2.json 首次可为 MISS，重复请求应为 HIT；不同 Cloudflare PoP 可能需要分别取样。源站响应为 Cache-Control: public, max-age=300。
- /mirror-api/status、/room-cache/plan、/admin、翻译 /v1/ 和下载站 /api/launcher/update 均不得 HIT。
- /admin/assets/*.js 可以缓存；更新时依赖源站文件指纹或重新验证，不能对未版本化脚本强制一年 TTL。
- 大文件是否入边缘缓存受 Cloudflare 方案的单文件大小限制约束；主站配额、七牛分发和启动器更新仍按服务端各自策略控制。

依据：[规则顺序](https://developers.cloudflare.com/cache/how-to/cache-rules/order/)、[Edge TTL 模式](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/)、[默认缓存行为及文件限制](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/)、[源站 Cache-Control](https://developers.cloudflare.com/cache/concepts/cache-control/)。
