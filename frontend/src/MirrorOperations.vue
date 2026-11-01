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
    <article class="admin-card"><div class="section-heading"><h2>空间使用</h2><a href="/admin/mirror/settings/storage">调整空间与调度 →</a></div><div id="mirror-storage-bars"></div><p id="mirror-quota-reason" class="mirror-explanation"></p><p id="mirror-catalog-summary" class="muted small"></p></article>
    <article class="admin-card"><div class="section-heading"><h2>云端副本</h2><a href="/admin/mirror/settings/cloud">管理云存储 →</a></div><p id="cloud-overview">尚未加载云端状态。</p><p class="muted small">这里仅统计本站已核验的文件，不代表七牛云账单用量。</p></article>
    <div class="mirror-overview-links"><a href="/admin/mirror/resources">缓存资源 <span>本地实际文件与校验</span></a><a href="/admin/mirror/sources">来源覆盖 <span>映射与下载命中</span></a><a href="/admin/mirror/quality">来源质量 <span>速度、稳定性与采样记录</span></a></div>
  </section>
  <section v-show="page==='resources'" class="records-panel mirror-file-panel">
    <div class="mirror-resource-intro"><div><span class="section-caption">LOCAL OBJECTS</span><h2>实际缓存的文件</h2><p id="mirror-resource-summary" class="muted small">正在读取本地缓存统计…</p></div><a href="/admin/mirror/settings/storage">空间设置 →</a></div>
    <form id="cache-files-filter" class="resource-filters"><label>搜索文件 / 哈希<input id="cache-files-query" type="search" maxlength="160" placeholder="文件名、SHA-1、SHA-256"></label><label>资源分类<select id="cache-files-category"><option value="">全部分类</option><option value="core">游戏核心</option><option value="loaders">加载器</option><option value="mods">模组</option><option value="java">Java</option><option value="libraries">依赖库</option><option value="assets">原版资源</option></select></label><button class="secondary" type="submit">筛选</button></form>
    <p id="cache-files-count" class="muted small"></p>
    <div class="table-scroll"><table class="resource-table"><thead><tr><th>文件 / 校验</th><th>分类</th><th>大小</th><th>存储位置</th><th>缓存时间</th></tr></thead><tbody id="cache-files"></tbody></table></div>
    <div class="record-pagination"><button id="cache-files-prev" class="secondary" type="button" disabled>上一页</button><span id="cache-files-page">第 1 页</span><button id="cache-files-next" class="secondary" type="button" disabled>下一页</button></div>
  </section>
  <section v-show="page==='tasks'">
    <article class="admin-card"><div class="section-heading"><div><span class="section-caption">SYNCHRONIZATION</span><h2>当前任务</h2></div><a href="/admin/mirror/settings/storage">同步周期与存储设置 →</a></div><p id="mirror-job-state" role="status"></p><div id="mirror-job-progress"></div><p id="mirror-task-schedule" class="muted small"></p><p id="mirror-task-activity" class="mirror-explanation"></p><div class="mirror-admin-actions"><button id="mirror-inventory" type="button" class="secondary">扫描资源清单</button><button id="mirror-sync" type="button" class="secondary">立即增量同步</button><button id="mirror-prefetch" type="button">继续预下载</button><button id="mirror-retry" type="button" class="secondary">只重试失败文件</button><button id="mirror-cancel" type="button" class="secondary">停止任务</button></div><p class="muted small">增量同步每 30 分钟检查变化，上一轮未结束时顺延；失败文件单独重试，不清除已完成文件。</p><details><summary>清单原始记录</summary><pre id="mirror-report" class="mirror-admin-log">暂无清单。</pre></details></article>
    <article class="admin-card"><div class="section-heading"><div><span class="section-caption">DIAGNOSTICS</span><h2>失败文件与处理建议</h2></div><span id="mirror-failure-count" class="badge">暂无失败</span></div><div id="mirror-job-failures" class="mirror-failures"></div></article>
    <article class="admin-card"><div class="section-heading"><h2>正在从上游下载</h2><span id="mirror-transfer-capacity" class="badge"></span></div><p class="muted small">这里仅显示此刻正在传输的文件。后台预下载最多使用并发上限减 2 路，给玩家实时下载留出空间；文件很小或上游尚未响应时，活跃数可能低于上限。</p><div class="table-scroll"><table class="resource-table"><thead><tr><th>文件</th><th>已接收 / 总量</th><th>平均速度</th><th>开始时间</th></tr></thead><tbody id="mirror-transfers"></tbody></table></div></article>
    <article class="admin-card"><div class="section-heading"><div><span class="section-caption">CLOUD COPIES</span><h2>云端复制与迁移</h2></div><a href="/admin/mirror/settings/cloud">空间与类别配置 →</a></div><p id="cloud-task-state" class="muted small"></p><div class="mirror-admin-actions"><label>资源类别<select id="cloud-migrate-route"></select></label><label>最多入队文件数<input id="cloud-migrate-limit" type="number" min="1" max="200" value="20"></label><button id="cloud-migrate" type="button" class="secondary">分批迁移已缓存文件</button></div><p class="muted small">迁移仅在手动提交后入队；暂停上传时保留队列。每个对象通过 SHA-256 和云端哈希核对后才会分发或释放本地。</p><div class="table-scroll"><table><thead><tr><th>文件 / 类别</th><th>目标空间</th><th>大小</th><th>状态</th><th>时间 / 操作</th></tr></thead><tbody id="cloud-jobs"></tbody></table></div></article>
    <article class="admin-card"><h2>来源映射与空间预留</h2><div id="mirror-background-activities" class="mirror-activity-grid"></div></article>
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
