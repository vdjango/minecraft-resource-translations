<script setup lang="ts">
const path=location.pathname;
const resources=[['/admin/mirror','镜像概览'],['/admin/mirror/resources','缓存资源'],['/admin/mirror/tasks','同步任务'],['/admin/mirror/temporary','客户端临时缓存'],['/admin/mirror/sources','来源覆盖'],['/admin/mirror/quality','来源质量']];
const records=[['/admin','平台概览'],['/admin/traffic','访问统计'],['/admin/tasks','翻译任务'],['/admin/usage','用量与费用'],['/admin/feedback','译文反馈']];
const settings=[['/admin/mirror/settings','镜像设置'],['/admin/nodes','服务节点'],['/admin/settings','翻译设置'],['/admin/mail','邮件通知'],['/admin/clients','接入申请']];
const current=(url:string)=>path===url||(url==='/admin/mirror/settings'&&path.startsWith(url+'/'));
</script>
<template>
  <aside class="admin-sidebar" aria-label="管理工作区">
    <span class="eyebrow">资源与任务</span>
    <a v-for="[url,label] in resources" :key="url" :href="url" :class="{active:current(url)}" :aria-current="current(url)?'page':undefined">{{label}}</a>
    <details class="admin-nav-records" :open="records.some(([url])=>current(url))"><summary>平台与翻译记录</summary><a v-for="[url,label] in records" :key="url" :href="url" :class="{active:current(url)}" :aria-current="current(url)?'page':undefined">{{label}}</a></details>
    <span class="eyebrow admin-nav-group">设置配置</span>
    <a v-for="[url,label] in settings" :key="url" :href="url" :class="{active:current(url)}" :aria-current="current(url)?'page':undefined">{{label}}</a>
    <div class="sidebar-bottom">Buggcraft</div>
  </aside>
</template>
