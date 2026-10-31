<script setup lang="ts">
import SiteNav from '../SiteNav.vue';
import AdminAccess from '../AdminAccess.vue';
defineProps<{seed:Record<string,string>;views:any}>();
</script>

<template>
<SiteNav />
<main class="admin-shell">
<aside class="admin-sidebar"><span class="eyebrow">工作区</span><a href="/admin">运行概览</a><a href="/admin/traffic">访问统计</a><a href="/admin/nodes">服务节点</a><a href="/admin/settings">翻译设置</a><a class="active" aria-current="page" href="/admin/tasks">翻译任务</a><a href="/admin/usage">用量与费用</a><a href="/admin/mail">邮件通知</a><a href="/admin/clients">接入申请</a><a href="/admin/feedback">译文反馈</a><div class="sidebar-bottom">Buggcraft</div></aside>
<div class="admin-content">
<header class="admin-header"><div><div class="eyebrow">PLATFORM MANAGEMENT</div><h1 id="page-title">翻译任务</h1><p class="muted">查看任务进度、处理结果与自动重试计划。</p></div><button id="logout" type="button" class="secondary hidden">退出管理</button></header>
<section id="admin-access" :data-mode="seed.authMode" class="admin-access-host"><AdminAccess v-if="views.access.options" :options="views.access.options" :visible="views.access.visible" /></section><p id="message" class="admin-message" role="status" aria-live="polite"></p>
<div id="panel" class="hidden">
<section id="tasks-section" class="admin-section records-page">
  <div class="records-toolbar"><div><h2>任务记录</h2><p>按更新时间排序，查看详情可展开错误与重试信息。</p></div><div class="records-controls"><label for="task-filter">任务状态</label><select id="task-filter"><option value="">全部状态</option><option value="ready">成功</option><option value="failed">失败</option><option value="deferred">延后重试</option><option value="queued">排队中</option><option value="running">翻译中</option><option value="verifying">核验中</option><option value="partial">部分完成</option><option value="blocked">已阻止</option></select><button id="refresh-tasks" class="secondary">刷新</button></div></div>
  <div class="task-status-tabs" role="group" aria-label="快捷筛选任务"><button type="button" data-task-state="" aria-pressed="true">全部</button><button type="button" data-task-state="ready" aria-pressed="false">成功</button><button type="button" data-task-state="failed" aria-pressed="false">失败</button></div>
  <p id="task-budget" class="queue-notice" role="status"></p>
  <div class="records-panel"><div class="record-columns task-row" aria-hidden="true"><span>资源 / 字段</span><span>状态</span><span>翻译进度</span><span>更新时间</span><span>操作</span></div><div id="tasks" class="record-list" aria-label="翻译任务记录"></div></div>
</section>
</div></div></main>
<dialog id="review-dialog"><form id="review-form"><div class="section-caption">CLIENT QUOTA</div><h2 id="review-name">批准申请</h2><p class="muted small">独立额度仍受平台全局预算及 IP 防滥用限额约束。</p><input id="review-id" type="hidden"><label>每小时翻译字段数<input id="review-fields" type="number" min="1" max="100000" required></label><label>每分钟请求数<input id="review-rpm" type="number" min="1" max="10000" required></label><div class="action-row"><button type="button" id="review-cancel" class="secondary">取消</button><button type="submit">确认启用</button></div></form></dialog>
</template>
