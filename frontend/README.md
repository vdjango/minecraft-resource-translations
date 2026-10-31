# Web frontend

Vue 3、TypeScript 和 Vite。`src/pages/` 包含统一资源首页、详情、开发文档、开发者中心和管理后台。`ResourceDownloads.vue` 提供首页下载区域，`ServiceNetwork.vue` 统一展示节点能力与加入步骤；`AdminAccess.vue` 管理账户表单，`Catalog.vue` 管理资源卡片。

```sh
npm ci
python ../scripts/render_platform_pages.py
npm run build
npm test
```

构建产物写入 `../src/mc_translate/web/`，页面模板自动更新资源版本号。部署时一起发布模板、`vue-*.js` 和样式。服务端输出列表与详情首屏，Vue 接管时保留已有译文、筛选状态和会话显示，避免空白后再次加载。

`pages.json` 定义页面入口和接口控制模块；请求、计时器和全局监听由页面生命周期统一回收。修改布局请编辑 `src/pages/*.vue`，用 `render_platform_pages.py` 更新首屏 HTML，再构建。导航由 `navigation.json` 统一维护，生成器只替换网站导航，不覆盖页面内的设置页签。后台设置在 `/admin/settings` 分页签展示，旧地址兼容跳转。不要直接修改生成的 JavaScript。文档代码示例使用 `v-pre`，服务端已清洗的资源正文通过 `v-html` 显示。
