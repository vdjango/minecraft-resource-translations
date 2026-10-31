<script setup lang="ts">
import SiteNav from '../SiteNav.vue';
import ServiceNetwork from '../ServiceNetwork.vue';
defineProps<{seed:Record<string,string>}>();
</script>

<template>
<SiteNav /><main class="platform-developers"><header class="platform-developer-hero"><div class="eyebrow">BUILD WITH BUGGCRAFT</div><h1>接入资源，也成为服务的一部分。</h1><p>一份文档接入项目译文与游戏下载；部署自己的节点，与社区一起分担服务。</p><div class="mirror-actions"><a class="button" href="/docs">阅读开发文档 →</a><a class="button secondary" href="#network">部署与加入节点 ↓</a><a class="button secondary" href="#access">申请生成额度 ↓</a></div></header><ServiceNetwork /><section id="access" class="platform-access"><div class="eyebrow">LAUNCHER ACCESS</div><h2>申请启动器生成额度</h2><p>游戏下载、公开资源查询、译文读取和个人试用不需要申请密钥。申请密钥用于给正式启动器分配独立生成额度、统计用量和管理停用；模型供应商密钥绝不会发给接入方。</p><form id="application"><label>启动器 / 应用名称<input id="name" maxlength="100" required></label><label>维护者联系信息<input id="contact" maxlength="200" required placeholder="邮箱或公开主页"></label><label>项目主页（可选）<input id="website" type="text" maxlength="500" placeholder="支持 HTTP、HTTPS 或其他主页地址"></label><label>接入用途与预计用户规模<textarea id="purpose" maxlength="1000" required></textarea></label><button>提交申请并生成待审批凭据</button></form><p id="message" role="status" aria-live="polite"></p><section id="result" class="hidden"><h2>待审批凭据</h2><p>密钥只显示这一次，请先安全保存。批准前不能创建翻译任务；无需密钥的只读接口仍可直接访问。</p><pre v-pre id="issued-key"></pre></section><section id="status-section"><h2>查询审批状态</h2><form id="status-form"><label>申请时保存的接入密钥<input id="status-key" type="password" autocomplete="off" required></label><button>查询状态</button></form><p id="application-status" role="status" aria-live="polite"></p></section><p><a href="/docs">查看平台接入文档 →</a></p></section></main>
</template>
