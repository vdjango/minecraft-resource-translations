<script setup lang="ts">
import SiteNav from '../SiteNav.vue';
import AdminAccess from '../AdminAccess.vue';
defineProps<{seed:Record<string,string>;views:any}>();
</script>

<template>
<SiteNav />
<main class="admin-shell">
<aside class="admin-sidebar"><span class="eyebrow">工作区</span><a href="/admin">运行概览</a><a href="/admin/traffic">访问统计</a><a class="active" aria-current="page" href="/admin/nodes">服务节点</a><a href="/admin/settings">翻译设置</a><a href="/admin/mirror">镜像源管理</a><a href="/admin/tasks">翻译任务</a><a href="/admin/usage">用量与费用</a><a href="/admin/mail">邮件通知</a><a href="/admin/clients">接入申请</a><a href="/admin/feedback">译文反馈</a><div class="sidebar-bottom">Buggcraft</div></aside>
<div class="admin-content">
<header class="admin-header"><div><div class="eyebrow">PLATFORM MANAGEMENT</div><h1 id="page-title">服务节点</h1><p class="muted">在同一工作台查看译文与下载节点，按节点能力管理审批与分流。</p></div><button id="logout" type="button" class="secondary hidden">退出管理</button></header>
<section id="admin-access" :data-mode="seed.authMode" class="admin-access-host"><AdminAccess v-if="views.access.options" :options="views.access.options" :visible="views.access.visible" /></section><p id="message" class="admin-message" role="status" aria-live="polite"></p>
<div id="panel" class="hidden">
<section class="admin-section records-page"><div class="records-toolbar"><div><h2>社区节点</h2><p id="node-role">正在读取节点状态…</p></div><div class="records-controls"><a class="button secondary" href="/network" target="_blank" rel="noopener">加入指南 ↗</a><button id="refresh-nodes" class="secondary" type="button">刷新节点</button></div></div><div id="node-metrics" class="admin-metrics compact-metrics"></div><div class="records-panel"><div class="records-panel-title"><h3>节点审批与在线状态</h3><span>最多展示最近 100 个节点</span></div><div id="federation-nodes"></div></div><section class="admin-card traffic-notes"><h3>分发与安全边界</h3><p>节点先申请，管理员验证地址所有权后启用。客户端通过实际往返测速选择节点；节点离线或被撤销后退出发现列表，读取失败自动回主服务。</p><p>镜像只缓存公共译文与项目介绍，不持有主服务模型密钥、管理令牌或启动器应用密钥。申请翻译、任务票据和其他写入始终发送主服务。</p><p>节点上报数字单独显示，主服务回源请求不与节点请求相加冒充独立用户。节点异常时撤销即可停止分发。</p></section></section><section id="resource-overview" class="admin-section admin-card"><div class="section-heading"><div><div class="section-caption">RESOURCE DELIVERY</div><h2>资源下载与分发</h2></div><a href="/admin/mirror#downloads">配置资源策略 →</a></div><p id="resource-overview-state" role="status" class="muted">正在读取下载状态…</p><div id="resource-overview-metrics" class="admin-metrics compact-metrics"></div><p id="resource-overview-budget" class="muted small"></p></section><p class="muted small">下载节点的地址、权重与出口预算在 <a href="/admin/settings#distribution">平台设置 · 节点与流量</a> 中配置；本页列表用于译文节点的身份审批和心跳管理。</p></div></div></main>
<dialog id="review-dialog"><form id="review-form"><div class="section-caption">CLIENT QUOTA</div><h2 id="review-name">批准申请</h2><p class="muted small">独立额度仍受平台全局预算及 IP 防滥用限额约束。</p><input id="review-id" type="hidden"><label>每小时翻译字段数<input id="review-fields" type="number" min="1" max="100000" required></label><label>每分钟请求数<input id="review-rpm" type="number" min="1" max="10000" required></label><div class="action-row"><button type="button" id="review-cancel" class="secondary">取消</button><button type="submit">确认启用</button></div></form></dialog>
</template>
