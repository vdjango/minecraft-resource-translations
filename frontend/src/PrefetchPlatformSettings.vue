<script setup lang="ts">
import {prefetchPlatforms,useMirrorPrefetchStore} from './stores/mirrorPrefetch';
const prefetch=useMirrorPrefetchStore();
</script>

<template>
  <fieldset class="prefetch-platforms" :disabled="!prefetch.ready">
    <legend>预下载平台 · 可多选</legend>
    <p class="muted small">只预下载所选平台；通用文件按哈希复用。</p>
    <div class="prefetch-options">
      <label v-for="platform in prefetchPlatforms" :key="platform.key">
        <input v-model="prefetch.platforms" type="checkbox" :value="platform.key">
        <span>{{platform.name}}</span>
      </label>
    </div>
    <p class="muted small" role="status">{{prefetch.platforms.length?'当前：'+prefetch.label:'未选择平台：只更新索引'}}。保存后自动补齐，其他平台仍支持按需下载。</p>
  </fieldset>
</template>

<style scoped>
.prefetch-platforms{margin:14px 0 0;padding:12px 14px;border:1px solid var(--line,#41354e);border-radius:9px;min-width:0}
.prefetch-platforms legend{padding:0 6px;font-size:12px;font-weight:600}
.prefetch-platforms p{margin:4px 0;font-size:11px;line-height:1.5}
.prefetch-options{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0}
.prefetch-options label{display:inline-flex;align-items:center;gap:7px;min-height:30px;padding:5px 9px;border:1px solid var(--line,#41354e);border-radius:7px;font-size:12px;cursor:pointer}
.prefetch-options label:has(input:checked){border-color:var(--accent,#9976ed);background:color-mix(in srgb,var(--accent,#9976ed) 10%,transparent)}
.prefetch-options input{width:15px;height:15px;margin:0;accent-color:var(--accent,#9976ed)}
</style>
