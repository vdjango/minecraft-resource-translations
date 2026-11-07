<script setup lang="ts">
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import {computed} from 'vue';
import {cloudKinds,profileName,useCloudStorageStore} from './stores/cloudStorage';
import {useRoomClientCacheStore} from './stores/roomClientCache';
const cloud=useCloudStorageStore(),room=useRoomClientCacheStore();
const kinds=cloudKinds.filter(([key])=>key.startsWith('room_'));
const local=computed({get:()=>room.settings.room_cache_quota_mib/1024,set:(n:number)=>{room.settings.room_cache_quota_mib=Math.round(n*1024);}});
const staging=computed({get:()=>room.settings.room_cache_staging_mib/1024,set:(n:number)=>{room.settings.room_cache_staging_mib=Math.round(n*1024);}});
</script>
<template>
<section class="workspace-card"><WorkspaceCardHeader title="客户端临时文件存储"><RouterLink to="/admin/mirror/settings/room-cache">保留与缓存规则 →</RouterLink></WorkspaceCardHeader>
  <div class="workspace-form-grid"><label>本地保留上限 / GiB<input v-model.number="local" type="number" min="0" max="1024" step="0.01"><small>0 表示不新增本地保留副本，不影响云端容量。</small></label><label>上传中转暂存 / GiB<input v-model.number="staging" type="number" min="0.01" max="1024" step="0.01"><small>云端核验后释放；玩家直传云端时不占此项。</small></label></div>
  <div class="workspace-table"><table><thead><tr><th>类别</th><th>存储空间</th><th>本地副本</th></tr></thead><tbody><tr v-for="[key,label] in kinds" :key="key"><td>{{label}}</td><td><select :value="cloud.routes[key]?.mode==='local'?'':cloud.routes[key]?.profile" :aria-label="label+'存储空间'" @change="cloud.selectStorage(key,($event.target as HTMLSelectElement).value)"><option value="">本机磁盘</option><option v-for="p in cloud.profiles.filter(p=>p.private)" :key="p.id" :value="p.id">{{profileName(p)}}</option></select><small v-if="cloud.routeErrors[key]">{{cloud.routeErrors[key]}}</small></td><td><select v-if="cloud.routes[key]?.mode!=='local'" v-model="cloud.routes[key].mode" :aria-label="label+'本地副本'"><option value="cloud">仅云端</option><option value="copy">本地与云端</option></select><span v-else>本地保留</span><small v-if="!local&&cloud.routes[key]?.mode!=='cloud'">本地上限为 0，请选择仅云端。</small></td></tr></tbody></table></div>
  <p class="workspace-hint">云端使用目标空间自己的容量和流量预算。</p>
</section>
</template>
