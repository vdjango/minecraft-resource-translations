<script setup lang="ts">
defineProps<{page:string}>();
</script>
<template>
<div class="mirror-observation" v-show="['overview','resources','tasks','coverage','quality','temporary'].includes(page)">
  <div class="records-toolbar"><p id="mirror-state" class="muted" role="status">正在连接资源服务…</p><button id="mirror-refresh" class="secondary" type="button">刷新列表</button></div>
  <section v-show="page==='overview'">
    <div id="mirror-metrics" class="admin-metrics compact-metrics"></div>
    <div class="mirror-overview-grid">
      <article class="admin-card"><span class="section-caption">SYNCHRONIZATION</span><h2>同步与调度</h2><p id="mirror-overview-job"></p><p id="mirror-overview-schedule"></p><p id="mirror-overview-last" class="muted small"></p><a href="/admin/mirror/tasks">查看同步任务 →</a></article>
      <article class="admin-card"><span class="section-caption">CLIENT FILES</span><h2>客户端临时缓存</h2><p id="room-cache-overview"></p><p class="muted small">仅计实际存储对象；直下文件信息不占实体容量。多房间共用同一哈希。</p><a href="/admin/mirror/temporary">查看临时文件 →</a></article>
    </div>
    <article class="admin-card"><div class="section-heading"><h2>空间使用</h2><a href="/admin/mirror/settings/storage">调整空间与调度 →</a></div><div id="mirror-storage-bars"></div><p class="muted small">普通镜像与客户端临时文件分别受各自配额限制，同时服从磁盘余量保护。</p></article>
    <div class="mirror-overview-links"><a href="/admin/mirror/resources">缓存资源 <span>本地实际文件与校验</span></a><a href="/admin/mirror/sources">来源覆盖 <span>映射与下载命中</span></a><a href="/admin/mirror/quality">来源质量 <span>速度、稳定性与采样记录</span></a></div>
  </section>
  <section v-show="page==='resources'" class="records-panel">
    <form id="cache-files-filter" class="resource-filters"><label>搜索文件 / 哈希<input id="cache-files-query" type="search" maxlength="160" placeholder="文件名、SHA-1、SHA-256"></label><label>资源分类<select id="cache-files-category"><option value="">全部分类</option><option value="core">游戏核心</option><option value="loaders">加载器</option><option value="mods">模组</option><option value="java">Java</option><option value="libraries">依赖库</option><option value="assets">原版资源</option></select></label><button class="secondary" type="submit">筛选</button></form>
    <p id="cache-files-count" class="muted small"></p>
    <div class="table-scroll"><table class="resource-table"><thead><tr><th>文件 / 校验</th><th>分类</th><th>大小</th><th>存储位置</th><th>缓存时间</th></tr></thead><tbody id="cache-files"></tbody></table></div>
    <div class="record-pagination"><button id="cache-files-prev" class="secondary" type="button" disabled>上一页</button><span id="cache-files-page">第 1 页</span><button id="cache-files-next" class="secondary" type="button" disabled>下一页</button></div>
  </section>
  <section v-show="page==='tasks'">
    <article class="admin-card"><div class="section-heading"><h2>当前同步</h2><a href="/admin/mirror/settings/storage">同步周期与存储设置 →</a></div><p id="mirror-job-state" role="status"></p><div id="mirror-job-progress"></div><p id="mirror-task-schedule" class="muted small"></p><div class="mirror-admin-actions"><button id="mirror-inventory" type="button" class="secondary">扫描清单与容量</button><button id="mirror-sync" type="button" class="secondary">立即增量同步</button><button id="mirror-prefetch" type="button">开始 / 继续预下载</button><button id="mirror-cancel" type="button" class="secondary">停止后台任务</button></div><p class="muted small">扫描只读取元数据；预下载遵守各类缓存策略和空间保护。停止任务不影响已缓存文件下载。</p><div id="mirror-job-failures"></div><details><summary>清单与原始任务记录</summary><pre id="mirror-report" class="mirror-admin-log">暂无清单。</pre></details></article>
    <article class="admin-card"><h2>正在回源</h2><div class="table-scroll"><table class="resource-table"><thead><tr><th>文件</th><th>已接收 / 总量</th><th>平均速度</th><th>开始时间</th></tr></thead><tbody id="mirror-transfers"></tbody></table></div></article>
    <article class="admin-card"><h2>其他后台活动</h2><div id="mirror-background-activities"></div></article>
    <article class="admin-card"><h2>最近同步任务</h2><div class="table-scroll"><table><thead><tr><th>任务 / 触发</th><th>状态</th><th>处理结果</th><th>开始 / 结束</th></tr></thead><tbody id="mirror-job-history"></tbody></table></div><p class="muted small">最近 100 条持久保存，此处展示最近 12 条；服务重启前未结束的任务标记为中断。</p></article>
  </section>
  <section v-show="page==='temporary'">
    <p id="room-cache-state" class="muted small" role="status"></p><div class="section-heading"><p class="muted">仅在正常来源不可用或持续过慢时保存实体文件。</p><a href="/admin/mirror/settings/room-cache">客户端缓存策略 →</a></div>
    <div id="room-cache-metrics" class="admin-metrics compact-metrics"></div>
    <article class="admin-card"><form id="room-files-filter" class="resource-filters"><label>搜索临时文件<input id="room-files-query" type="search" maxlength="160" placeholder="文件名或哈希"></label><button class="secondary" type="submit">筛选</button></form><p id="room-files-count" class="muted small"></p><div class="table-scroll"><table class="resource-table"><thead><tr><th>文件 / SHA-256</th><th>状态 / 原因</th><th>大小 / 实际占用</th><th>存储位置</th><th>保留到 / 引用</th></tr></thead><tbody id="room-files"></tbody></table></div><div class="record-pagination"><button id="room-files-prev" class="secondary" type="button" disabled>上一页</button><span id="room-files-page">第 1 页</span><button id="room-files-next" class="secondary" type="button" disabled>下一页</button></div></article>
    <article class="admin-card"><h3>公开来源观测</h3><div class="table-scroll"><table><thead><tr><th>来源</th><th>采样速度</th><th>连续慢速 / 样本</th><th>采样时间</th></tr></thead><tbody id="room-cache-sources"></tbody></table></div><p class="muted small">节点观测不代表所有玩家速度；Modrinth 与 MCIM 不参与自动候选。</p></article>
  </section>
</div>
</template>
