# Web frontend

页面使用 Vue 3、Vue Router、Pinia、TypeScript 和 Vite。路由定义在 `src/platformRoutes.ts`，页面组件在 `src/pages/*.vue`；管理导航配置在 `src/adminNavigation.ts`。页面状态使用 Vue 响应式数据或 Pinia store，不新增逐页 HTML 模板。

~~~sh
npm ci
npm run build
~~~

构建产物输出至 `../src/mc_translate/web/`。Python 服务端通过 `spa_shell.py` 生成浏览器所需的最小启动文档，并为项目详情提供元数据和初始化数据。开发页面只修改 Vue 组件，构建时不生成或更新 `.html` 文件。

浏览器需要 HTML 文档作为 Vue 启动载体；这里禁止维护手写页面模板，页面结构和交互只放在 Vue 组件中。构建会拒绝前端源码或服务静态目录中新出现的 `.html` 文件。
