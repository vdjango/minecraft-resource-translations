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
<header class="admin-header"><div><div class="eyebrow">PLATFORM MANAGEMENT</div><h1 id="page-title">接入申请</h1><p class="muted">审核启动器接入申请，管理密钥状态与独立额度。</p></div><button id="logout" type="button" class="secondary hidden">退出管理</button></header>
<section id="admin-access" :data-mode="seed.authMode" class="admin-access-host"><AdminAccess v-if="views.access.options" :options="views.access.options" :visible="views.access.visible" /></section><p id="message" class="admin-message" role="status" aria-live="polite"></p>
<div id="panel" class="hidden">
<section id="client-section" class="admin-section records-page">
  <div class="records-toolbar"><div><h2>申请列表</h2><p>公开查询无需密钥。批准后开放生成额度，撤销后立即停用。</p></div><div class="records-controls"><span id="client-count" class="muted small"></span><button id="refresh-clients" class="secondary">刷新</button></div></div>
  <div class="records-panel"><div class="record-columns client-row" aria-hidden="true"><span>应用 / 联系方式</span><span>状态</span><span>接入额度</span><span>申请时间</span><span>操作</span></div><div id="clients" class="record-list" aria-label="接入申请记录"></div></div>
</section>
</div></div></main>
<dialog id="review-dialog"><form id="review-form"><div class="section-caption">CLIENT QUOTA</div><h2 id="review-name">批准申请</h2><p class="muted small">独立额度仍受平台全局预算及 IP 防滥用限额约束。</p><input id="review-id" type="hidden"><label>每小时翻译字段数<input id="review-fields" type="number" min="1" max="100000" required></label><label>每分钟请求数<input id="review-rpm" type="number" min="1" max="10000" required></label><div class="action-row"><button type="button" id="review-cancel" class="secondary">取消</button><button type="submit">确认启用</button></div></form></dialog>
</template>
