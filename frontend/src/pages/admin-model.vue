<script setup lang="ts">
import SiteNav from '../SiteNav.vue';
import AdminNav from '../AdminNav.vue';
import AdminAccess from '../AdminAccess.vue';
if(['downloads','storage','distribution','sources','quality','cloudflare'].includes(location.hash.slice(1)))location.replace('/admin/mirror'+location.hash);
defineProps<{seed:Record<string,string>;views:any}>();
</script>
<template>
<SiteNav />
<main class="admin-shell">
<AdminNav />
<div class="admin-content">
<header class="admin-header"><div><div class="eyebrow">PLATFORM MANAGEMENT</div><h1 id="page-title">翻译设置</h1><p class="muted">配置翻译模型、Token 预算与参考单价。镜像源在独立菜单管理。</p></div><button id="logout" type="button" class="secondary hidden">退出管理</button></header>
<section id="admin-access" :data-mode="seed.authMode" class="admin-access-host"><AdminAccess v-if="views.access.options" :options="views.access.options" :visible="views.access.visible" /></section><p id="message" class="admin-message" role="status" aria-live="polite"></p>
<div id="panel" class="hidden">

<section id="settings-translation" class="admin-section admin-card"><div class="section-heading"><div><div class="section-caption">MODEL PROVIDER</div><h2>模型服务</h2></div><span class="badge">OpenAI 兼容</span></div><p class="muted">从服务商模型列表选择模型。配置用于后续请求，正在执行的翻译继续完成。</p>
<form id="settings" class="admin-form"><div class="form-grid">
<label class="wide">API 基础地址<input id="base-url" type="url" required placeholder="https://api.example.com/v1"><small>包含 /v1；更换服务商时填写新密钥，再读取模型。</small></label>
<label class="wide">替换 API Key<input id="api-key" type="password" autocomplete="new-password" placeholder="留空保留当前密钥"><small id="key-state"></small></label>
<label>模型<select id="model" required aria-describedby="model-state"><option value="">正在读取模型…</option></select></label>
<label>推理档位<select id="effort"><option value="none">关闭推理</option><option value="minimal">极简</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">极高</option></select></label>
<div class="wide model-tools"><button id="refresh-models" type="button" class="secondary">读取服务商模型</button><span id="model-state" role="status" class="muted small"></span></div>
</div><h3 class="form-divider">用量与性能</h3><div class="form-grid three">
<label>每日 Token 上限<input id="daily-tokens" type="number" min="0" required></label><label>每月 Token 上限<input id="monthly-tokens" type="number" min="0" required></label><label>每日调用上限<input id="daily-calls" type="number" min="0" required></label>
<label>最大输出 Token<input id="completion-tokens" type="number" min="256" max="131072" required></label><label>调用超时 / 秒<input id="timeout" type="number" min="10" max="600" required></label></div><h3 class="form-divider">当前模型费用 · 美元 / 百万 Token</h3><p class="muted small">GPT 留空时按内置 OpenAI 标准单价估算；也可填写供应商单价覆盖。以实际账单为准，历史调用保留当时单价。</p><div class="form-grid three"><label>输入<input id="price-input" type="number" min="0" step="0.000001"></label><label>缓存输入<input id="price-cached" type="number" min="0" step="0.000001"></label><label>输出<input id="price-output" type="number" min="0" step="0.000001"></label></div><p id="price-reference" class="muted small" role="status"></p><div class="form-footer"><span class="muted small">上游密钥保存后不会回显。</span><button type="submit">保存模型配置</button></div></form></section>




</div></div></main></template>
