<script setup lang="ts">
import {useRoomClientCacheStore} from './stores/roomClientCache';
const rooms=useRoomClientCacheStore();
async function invite(){try{await rooms.invite();}catch{}}
</script>
<template>
  <div class="room-node-invitation">
    <p>源站生成一次性接入邀请，联机节点领取时自动生成并保存密钥。</p>
    <form class="workspace-form" @submit.prevent="invite">
      <label>节点名称<input v-model.trim="rooms.nodeName" maxlength="80" placeholder="例如 上海"></label>
      <button type="submit" :disabled="rooms.busy||!rooms.nodeName.trim()">{{rooms.busy?'正在生成…':'生成一次性接入邀请'}}</button>
    </form>
    <div v-if="rooms.invitation" class="workspace-alert workspace-invite" role="status">
      <strong>10 分钟内在联机节点后台粘贴，仅可领取一次</strong>
      <p>联机节点后台 → 节点设置 → 资源下载加速 → 接入源站</p>
      <code>{{rooms.invitation}}</code>
      <button type="button" class="secondary" @click="rooms.copy()">复制接入邀请</button>
    </div>
    <p v-if="rooms.feedback" class="workspace-hint" role="status">{{rooms.feedback}}</p>
    <p v-if="rooms.error" class="workspace-alert" role="alert">{{rooms.error}}</p>
    <RouterLink to="/admin/nodes">在节点管理中查看或撤销授权 →</RouterLink>
  </div>
</template>
<style scoped>
.room-node-invitation,.workspace-invite{display:grid;gap:14px}.room-node-invitation p{margin:0}.workspace-invite code{overflow-wrap:anywhere;user-select:all}
</style>
