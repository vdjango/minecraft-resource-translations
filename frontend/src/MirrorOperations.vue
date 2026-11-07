<script setup lang="ts">
import {computed} from 'vue';
import CloudStorageTasks from './CloudStorageTasks.vue';
import LocalCacheCleanup from './LocalCacheCleanup.vue';
import {useCloudStorageStore} from './stores/cloudStorage';
const cloud=useCloudStorageStore();
const job=computed(()=>cloud.delivery.job||{});
const flow=computed(()=>[
  {name:'扫描清单',active:job.value.state==='running'&&job.value.phase==='catalogue',text:'整理版本、平台、哈希与大小，不存游戏实体'},
  {name:'核验公开来源',active:job.value.state==='running'&&job.value.phase==='sources',text:'命中健康来源则直连，避免占用本站空间与流量'},
  {name:'筛选与回源',active:job.value.state==='running'&&['selection','download','loader_metadata'].includes(job.value.phase),text:'按平台和类别准备必要文件；单个失败继续处理其他文件'},
  {name:'上传与交付',active:!!cloud.snapshot.active_job,text:'云端上传、下载验证后释放暂存；本机类别保留本机'}
]);
defineProps<{page:string}>();
</script>
<template>
<div class="mirror-observation" v-show="['overview','resources','tasks','transfers','coverage','quality','temporary'].includes(page)">
  <div class="records-toolbar"><p id="mirror-state" class="muted" role="status">正在连接资源服务…</p><button id="mirror-refresh" class="secondary" type="button">刷新列表</button></div>
  <section v-show="page==='overview'">
    <div id="mirror-metrics" class="admin-metrics compact-metrics"></div>
    <div class="mirror-overview-grid">
      <article class="admin-card"><span class="section-caption">SYNCHRONIZATION</span><h2>同步与调度</h2><p id="mirror-overview-job"></p><p id="mirror-overview-schedule"></p><p id="mirror-overview-last" class="muted small"></p><RouterLink to="/admin/mirror/tasks">查看同步任务 →</RouterLink></article>
      <article class="admin-card"><span class="section-caption">CLIENT FILES</span><h2>客户端临时缓存</h2><p id="room-cache-overview"></p><p class="muted small">仅计实际存储对象；直下文件信息不占实体容量。多房间共用同一哈希。</p><RouterLink to="/admin/mirror/temporary">查看临时文件 →</RouterLink></article>
    </div>
    <article class="admin-card"><div class="section-heading"><h2>实际存储与同步范围</h2><RouterLink to="/admin/mirror/settings/cloud">调整存储空间 →</RouterLink></div><div id="mirror-storage-bars"></div><p id="mirror-quota-reason" class="mirror-explanation"></p><p id="mirror-catalog-summary" class="muted small"></p></article>
    <article class="admin-card"><div class="section-heading"><h2>云端副本</h2><RouterLink to="/admin/mirror/settings/cloud">管理云存储 →</RouterLink></div><p>{{cloud.snapshot.profiles.length}} 个空间 · {{cloud.copies}} 个已核验副本 · {{(cloud.bytes/1e9).toFixed(2)}} GB</p><p class="muted small">仅统计本站文件，服务商账单需单独核对。</p></article>
    <div class="mirror-overview-links"><RouterLink to="/admin/mirror/resources">缓存资源 <span>本机 / 云端文件与校验</span></RouterLink><RouterLink to="/admin/mirror/sources">来源覆盖 <span>映射与下载命中</span></RouterLink><RouterLink to="/admin/mirror/quality">来源质量 <span>速度、稳定性与采样记录</span></RouterLink></div>
  </section>
  <section v-show="page==='resources'" class="records-panel mirror-file-panel">
    <div class="mirror-resource-intro"><div><span class="section-caption">VERIFIED OBJECTS</span><h2>已核验的缓存文件</h2><p id="mirror-resource-summary" class="muted small">正在读取缓存统计…</p></div><RouterLink to="/admin/mirror/settings/cloud">存储空间 →</RouterLink></div>
    <form id="cache-files-filter" class="resource-filters"><label>搜索文件 / 哈希<input id="cache-files-query" type="search" maxlength="160" placeholder="文件名、SHA-1、SHA-256"></label><label>资源分类<select id="cache-files-category"><option value="">全部分类</option><option value="core">游戏核心</option><option value="loaders">加载器</option><option value="mods">模组</option><option value="java">Java</option><option value="libraries">依赖库</option><option value="assets">原版资源</option></select></label><button class="secondary" type="submit">筛选</button></form>
    <p id="cache-files-count" class="muted small"></p>
    <div class="table-scroll"><table class="resource-table"><thead><tr><th>文件 / 校验</th><th>分类</th><th>大小</th><th>存储位置</th><th>缓存时间</th></tr></thead><tbody id="cache-files"></tbody></table></div>
    <div class="record-pagination"><button id="cache-files-prev" class="secondary" type="button" disabled>上一页</button><span id="cache-files-page">第 1 页</span><button id="cache-files-next" class="secondary" type="button" disabled>下一页</button></div>
    <LocalCacheCleanup v-if="page==='resources'" />
  </section>
  <section v-show="page==='tasks'">
    <ol class="mirror-workflow" aria-label="资源处理流程"><li v-for="(step,index) in flow" :key="step.name" :class="{working:step.active}"><span>{{String(index+1).padStart(2,'0')}} · {{step.active?'进行中':'流程'}}</span><strong>{{step.name}}</strong><small>{{step.text}}</small></li></ol>
    <article class="admin-card"><div class="section-heading"><div><span class="section-caption">SYNCHRONIZATION</span><h2>当前任务</h2></div><RouterLink to="/admin/mirror/settings/storage">同步周期与分流 →</RouterLink></div><p id="mirror-job-state" role="status"></p><div id="mirror-job-progress"></div><p id="mirror-task-schedule" class="muted small"></p><p id="mirror-task-activity" class="mirror-explanation"></p><div class="mirror-admin-actions"><button id="mirror-inventory" type="button" class="secondary">扫描资源清单</button><button id="mirror-sync" type="button" class="secondary">立即增量同步</button><button id="mirror-prefetch" type="button">继续预下载</button><button id="mirror-retry" type="button" class="secondary">只重试失败文件</button><button id="mirror-cancel" type="button" class="secondary">停止任务</button></div><p class="muted small">扫描只登记文件信息，不下载实体。预下载先检查已核验来源，再按类别策略决定是否暂存于本机；云端类别随后上传、校验下载并释放可安全删除的本地副本。单个失败进入待补齐队列，不阻塞其他文件。</p><div id="mirror-sync-plan" class="mirror-sync-plan"></div><details><summary>清单原始记录</summary><pre id="mirror-report" class="mirror-admin-log">暂无清单。</pre></details></article>
    <article class="admin-card"><div class="section-heading"><div><span class="section-caption">DIAGNOSTICS</span><h2>失败文件与处理建议</h2></div><span id="mirror-failure-count" class="badge">暂无失败</span></div><div id="mirror-job-failures" class="mirror-failures"></div></article>
    <article class="admin-card"><div class="section-heading"><h2>正在从上游下载到本机暂存</h2><span id="mirror-transfer-capacity" class="badge"></span></div><p class="muted small">显示实际传输的文件、来源及最终去处；“本机暂存”不等于最终保留本地。后台预下载最多使用并发上限减 2 路，为玩家请求预留连接。云端上传进度见独立的“云端复制与迁移”页面。</p><p v-if="cloud.delivery.root" class="muted small">本机暂存目录：<code>{{cloud.delivery.root}}/tmp</code>；校验后的本机对象：<code>{{cloud.delivery.root}}/objects</code>。</p><div class="table-scroll"><table class="resource-table"><thead><tr><th>文件</th><th>平台</th><th>来源</th><th>最终去处</th><th>已接收 / 总量</th><th>平均速度</th><th>开始时间</th></tr></thead><tbody id="mirror-transfers"></tbody></table></div></article>
    <p class="muted small">回源后的云端上传、核验和释放暂存自动衔接，前往 <RouterLink to="/admin/mirror/transfers">云端复制与迁移 →</RouterLink></p>
    <article class="admin-card"><h2>来源映射与空间预留</h2><div id="mirror-background-activities" class="mirror-activity-grid"></div></article>
    <article class="admin-card"><h2>最近同步任务</h2><div class="table-scroll"><table><thead><tr><th>任务 / 触发</th><th>状态</th><th>处理结果</th><th>开始 / 结束</th></tr></thead><tbody id="mirror-job-history"></tbody></table></div><p class="muted small">最近 100 条持久保存，此处展示最近 12 条；服务重启前未结束的任务标记为中断。</p></article>
  </section>
  <CloudStorageTasks v-if="page==='transfers'" />
  <section v-show="page==='temporary'">
    <p id="room-cache-state" class="muted small" role="status"></p><div class="section-heading"><p class="muted">仅在正常来源不可用或持续过慢时保存实体文件。</p><RouterLink to="/admin/mirror/settings/room-cache">客户端缓存策略 →</RouterLink></div>
    <div id="room-cache-metrics" class="admin-metrics compact-metrics"></div>
    <article class="admin-card"><form id="room-files-filter" class="resource-filters"><label>搜索临时文件<input id="room-files-query" type="search" maxlength="160" placeholder="文件名或哈希"></label><button class="secondary" type="submit">筛选</button></form><p id="room-files-count" class="muted small"></p><div class="table-scroll"><table class="resource-table"><thead><tr><th>文件 / SHA-256</th><th>状态 / 原因</th><th>大小 / 实际占用</th><th>存储位置</th><th>保留到 / 引用</th></tr></thead><tbody id="room-files"></tbody></table></div><div class="record-pagination"><button id="room-files-prev" class="secondary" type="button" disabled>上一页</button><span id="room-files-page">第 1 页</span><button id="room-files-next" class="secondary" type="button" disabled>下一页</button></div></article>
    <article class="admin-card"><h3>公开来源观测</h3><div class="table-scroll"><table><thead><tr><th>来源</th><th>采样速度</th><th>连续慢速 / 样本</th><th>采样时间</th></tr></thead><tbody id="room-cache-sources"></tbody></table></div><p class="muted small">节点观测不代表所有玩家速度；Modrinth 与 MCIM 不参与自动候选。</p></article>
  </section>
</div>
</template>
<style scoped>
.mirror-workflow{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;list-style:none;padding:0;margin:0 0 20px}.mirror-workflow li{display:grid;align-content:start;gap:8px;border:1px solid var(--border,#393047);border-radius:12px;padding:17px;background:var(--panel,#201a2e)}.mirror-workflow li.working{border-color:var(--accent,#9f7ae8);box-shadow:inset 0 3px var(--accent,#9f7ae8)}.mirror-workflow span{font-size:11px;color:var(--accent-soft,#c6b0f2)}.mirror-workflow small{font-size:12px;line-height:1.7;color:var(--muted)}@media(max-width:1000px){.mirror-workflow{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:560px){.mirror-workflow{grid-template-columns:1fr}}
</style>
