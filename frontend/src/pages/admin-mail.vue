<script setup lang="ts">
import AdminAccess from '../AdminAccess.vue';
defineProps<{seed:Record<string,string>;views:any}>();
</script>

<template>

<div class="admin-content">
<header class="admin-header"><div><div class="eyebrow">PLATFORM MANAGEMENT</div><h1 id="page-title">邮件通知</h1><p class="muted">统一管理资源、译文、服务节点与开发者接入。</p></div><button id="logout" type="button" class="secondary hidden">退出管理</button></header>
<section class="admin-access-host" v-show="views.access.visible"><AdminAccess v-if="views.access.options" :options="views.access.options" :visible="views.access.visible" /></section><p id="message" class="admin-message" role="status" aria-live="polite"></p>
<div id="panel" class="hidden">


<section id="mail-section" class="admin-section admin-card"><div class="section-caption">NOTIFICATIONS</div><h2>申请通知邮件</h2><p class="muted">新申请自动通知指定邮箱。独立后台队列发送，失败自动重试。</p><div id="mail-form-host"></div></section>


</div>
</div><dialog id="review-dialog"><form id="review-form"><div class="section-caption">CLIENT QUOTA</div><h2 id="review-name">批准申请</h2><p class="muted small">独立额度仍受平台全局预算及 IP 防滥用限额约束。</p><input id="review-id" type="hidden"><label>每小时翻译字段数<input id="review-fields" type="number" min="1" max="100000" required></label><label>每分钟请求数<input id="review-rpm" type="number" min="1" max="10000" required></label><div class="action-row"><button type="button" id="review-cancel" class="secondary">取消</button><button type="submit">确认启用</button></div></form></dialog>
</template>
