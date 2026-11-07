<script setup lang="ts">
import {useCloudStorageStore} from './stores/cloudStorage';
const cloud=useCloudStorageStore();
function size(value:number){return value>=1024**3?(value/1024**3).toFixed(2)+' GiB':value>=1024**2?(value/1024**2).toFixed(2)+' MiB':value>=1024?(value/1024).toFixed(1)+' KiB':value+' B';}
</script>
<template>
  <article class="admin-card"><div class="section-heading"><div><h3>清理不再同步的本地资源</h3><p class="muted small">仅列出已设置“仅重定向”的类别。选中文件后清理服务器本地副本；云端文件保留，正在使用或被其他缓存类别引用的文件会跳过。</p></div><button type="button" class="secondary" :disabled="cloud.cleanupLoading" @click="cloud.loadCleanup()">查看可清理资源</button></div>
    <template v-if="cloud.cleanupReady"><p class="muted small">本批 {{cloud.cleanupPage.files.length}} 个文件 · {{size(cloud.cleanupPage.bytes)}}。勾选后再清理。</p><div class="table-scroll"><table><thead><tr><th>选择</th><th>文件</th><th>大小</th></tr></thead><tbody><tr v-for="file in cloud.cleanupPage.files" :key="file.digest"><td><input v-model="cloud.cleanupSelected" type="checkbox" :value="file.digest" :aria-label="'选择清理 '+file.path"></td><td><div class="cloud-job-path">{{file.path}}</div></td><td>{{size(file.size)}}</td></tr><tr v-if="!cloud.cleanupPage.files.length"><td colspan="3" class="transfer-empty">本批没有可清理的本地副本。</td></tr></tbody></table></div><div class="cleanup-actions"><button type="button" class="secondary" :disabled="cloud.cleanupLoading||!cloud.cleanupPage.files.length" @click="cloud.cleanupSelected=cloud.cleanupPage.files.map((file:any)=>file.digest)">选择本批</button><button type="button" :disabled="cloud.cleanupLoading||!cloud.cleanupSelected.length" @click="cloud.cleanupLocal">清理选中的本地副本（{{cloud.cleanupSelected.length}}）</button><button v-if="cloud.cleanupPage.next_cursor" type="button" class="secondary" :disabled="cloud.cleanupLoading" @click="cloud.loadCleanup(cloud.cleanupPage.next_cursor)">下一批</button></div></template>
  </article>
</template>
<style scoped>
.cleanup-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}.cloud-job-path{overflow-wrap:anywhere;max-width:650px;font-size:12px;line-height:1.6}.transfer-empty{text-align:center;color:var(--muted);padding:28px}
</style>
