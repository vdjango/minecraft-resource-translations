<script setup lang="ts">
import ServiceNetwork from '../ServiceNetwork.vue';
defineProps<{seed:Record<string,string>}>();
</script>

<template>
<main class="platform-developers">
  <header class="platform-developer-hero">
    <div class="eyebrow">BUILD WITH BUGGCRAFT</div>
    <h1>资源服务开发文档</h1>
    <p>启动器按统一地址获取资源与译文；也可以部署节点，与社区一起分担服务。</p>
    <div class="mirror-actions">
      <a class="button" href="/docs#downloads">查看接入文档 →</a>
      <a class="button secondary" href="#network">部署与加入节点 ↓</a>
    </div>
  </header>
  <section class="platform-access" aria-labelledby="launcher-access-title">
    <div class="eyebrow">LAUNCHER ACCESS</div>
    <h2 id="launcher-access-title">启动器接入方法</h2>
    <p>公开文件下载和译文读取可直接使用 <code>https://mirror.bugg.icu</code>，无需填写申请表或携带客户端密钥。将启动器已有的下载地址映射到平台路径，保留文件哈希校验，并支持 Range 与 307 跳转。</p>
    <p>请求请带可辨认的 <code>User-Agent</code>：<code>启动器名称/版本号</code>，例如 <code>Buggcraft/1.0.2-alpha.2</code>。名称使用稳定的英文、数字或连接符；版本号随发布更新。空值、浏览器默认值、冒用他人名称或不符合格式的自动化请求，后续可能被限速或拒绝。</p>
    <p>后台 <a href="/admin/clients">客户端密钥</a> 用于给特定客户端分配译文生成额度；它不是下载源站的入场券，也不是管理员密码。若使用生成能力，按分配的额度与凭据调用；跳转到外部下载源时不要转发凭据。</p>
    <a href="/docs#authentication">阅读密钥与接口说明 →</a>
  </section>
  <ServiceNetwork />
</main>
</template>
