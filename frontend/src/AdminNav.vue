<script setup lang="ts">
const path=location.pathname;
const resources=[['/admin/mirror','镜像统计'],['/admin/mirror/resources','缓存文件'],['/admin/mirror/tasks','同步与失败'],['/admin/mirror/temporary','房间临时文件'],['/admin/mirror/sources','来源覆盖'],['/admin/mirror/quality','来源评分']];
const records=[['/admin/traffic','访问统计'],['/admin/tasks','翻译任务'],['/admin/usage','用量与费用'],['/admin/feedback','译文反馈']];
const settings=[['/admin/mirror/settings','镜像策略'],['/admin/nodes','服务节点'],['/admin/settings','翻译配置'],['/admin/mail','邮件通知'],['/admin/clients','接入申请']];
const current=(url:string)=>path===url||(url==='/admin/mirror/settings'&&path.startsWith(url+'/'));
</script>
<template>
  <aside class="admin-sidebar" aria-label="管理工作区">
    <span class="eyebrow">工作台</span>
    <a href="/admin" :class="{active:current('/admin')}" :aria-current="current('/admin')?'page':undefined">平台总览</a>
    <span class="eyebrow admin-nav-group">镜像源</span>
    <a v-for="[url,label] in resources" :key="url" :href="url" :class="{active:current(url)}" :aria-current="current(url)?'page':undefined">{{label}}</a>
    <span class="eyebrow admin-nav-group">翻译与平台</span>
    <a v-for="[url,label] in records" :key="url" :href="url" :class="{active:current(url)}" :aria-current="current(url)?'page':undefined">{{label}}</a>
    <span class="eyebrow admin-nav-group">设置</span>
    <a v-for="[url,label] in settings" :key="url" :href="url" :class="{active:current(url)}" :aria-current="current(url)?'page':undefined">{{label}}</a>
    <div class="sidebar-bottom">Buggcraft</div>
  </aside>
</template>
