<script setup lang="ts">
import {useCloudStorageStore} from './stores/cloudStorage';
const cloud=useCloudStorageStore();
</script>
<template>
  <div class="upload-control" :class="{'is-paused':cloud.snapshot.paused}" :aria-busy="cloud.uploadChanging">
    <div class="upload-control-copy"><span class="upload-label">云端上传控制 · 已生效</span>
      <strong><span class="upload-dot" aria-hidden="true"></span>{{cloud.snapshot.paused?'上传已暂停':'后台上传已启用'}}</strong>
      <p>{{cloud.snapshot.paused?'排队文件会保留，当前文件完成后暂停。点击右侧按钮即可恢复，无需再到表单勾选保存。':'按各空间的容量及每日上传预算处理队列。暂停只影响新上传，云端下载和已上传文件继续可用。'}}</p>
    </div>
    <button type="button" :class="{'secondary':!cloud.snapshot.paused}" :disabled="!cloud.ready||cloud.uploadChanging||cloud.saving" @click="cloud.setUploadsPaused(!cloud.snapshot.paused)">{{cloud.uploadChanging?'正在更新…':cloud.snapshot.paused?'启用后台上传':'暂停新上传'}}</button>
  </div>
</template>
<style scoped>
.upload-control{display:flex;justify-content:space-between;align-items:center;gap:14px;padding:13px 16px;border:1px solid #497b72;border-radius:10px;background:linear-gradient(120deg,#19312c66,#201b3044)}.upload-control.is-paused{border-color:#a48348;background:linear-gradient(120deg,#45332066,#201b3044)}.upload-control-copy{display:grid;gap:4px}.upload-label{color:var(--muted,#b7afc9);font-size:10px}.upload-control strong{font-size:14px;display:flex;align-items:center;gap:8px}.upload-dot{width:7px;height:7px;border-radius:50%;background:#71d3b0}.is-paused .upload-dot{background:#eabf72}.upload-control p{margin:0;max-width:75ch;color:var(--muted,#b7afc9);font-size:11px;line-height:1.45}.upload-control button{flex-shrink:0;min-width:130px}@media(max-width:700px){.upload-control{align-items:stretch;flex-direction:column;gap:12px}}
</style>
