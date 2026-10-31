<script setup lang="ts">
import SiteNav from '../SiteNav.vue';
import AdminNav from '../AdminNav.vue';
import AdminAccess from '../AdminAccess.vue';
defineProps<{seed:Record<string,string>;views:any}>();
</script>

<template>
<SiteNav />
<main class="admin-shell">
<AdminNav />
<div class="admin-content">
<header class="admin-header"><div><div class="eyebrow">PLATFORM MANAGEMENT</div><h1 id="page-title">用量与费用</h1><p class="muted">查看模型调用、Token 消耗与费用估算。</p></div><button id="logout" type="button" class="secondary hidden">退出管理</button></header>
<section id="admin-access" :data-mode="seed.authMode" class="admin-access-host"><AdminAccess v-if="views.access.options" :options="views.access.options" :visible="views.access.visible" /></section><p id="message" class="admin-message" role="status" aria-live="polite"></p>
<div id="panel" class="hidden">
<section id="usage-section" class="admin-section records-page">
  <div class="records-toolbar"><div><h2>用量统计</h2><p>统计范围包含所选周期内的全部调用。</p></div><div class="records-controls"><label for="usage-period">统计周期</label><select id="usage-period"><option value="day">今天</option><option value="month">本月</option></select><label for="usage-filter">调用状态</label><select id="usage-filter"><option value="">全部状态</option><option value="succeeded">成功</option><option value="failed">失败</option><option value="pending">调用中</option><option value="unknown">结果未确认</option></select><button id="refresh-usage" class="secondary">刷新</button></div></div>
  <div id="usage-summary" class="admin-metrics compact-metrics"></div>
  <div class="records-panel"><div class="records-panel-title"><h3>调用明细</h3><span>费用单位 USD · 北京时间</span></div><div class="record-columns usage-row" aria-hidden="true"><span>模型</span><span>状态</span><span>Token 总量</span><span>费用 · USD</span><span>调用时间</span><span>操作</span></div><div id="usage-calls" class="record-list" aria-label="模型调用记录"></div></div>
  <p class="records-footnote">缓存输入按缓存价格单独计费，推理 Token 包含在输出中。费用按调用时配置的价格估算，以服务商账单为准。</p>
</section>
</div></div></main>
<dialog id="review-dialog"><form id="review-form"><div class="section-caption">CLIENT QUOTA</div><h2 id="review-name">批准申请</h2><p class="muted small">独立额度仍受平台全局预算及 IP 防滥用限额约束。</p><input id="review-id" type="hidden"><label>每小时翻译字段数<input id="review-fields" type="number" min="1" max="100000" required></label><label>每分钟请求数<input id="review-rpm" type="number" min="1" max="10000" required></label><div class="action-row"><button type="button" id="review-cancel" class="secondary">取消</button><button type="submit">确认启用</button></div></form></dialog>
</template>
