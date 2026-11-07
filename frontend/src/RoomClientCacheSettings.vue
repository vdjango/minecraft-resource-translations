<script setup lang="ts">
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import WorkspaceDrawer from './WorkspaceDrawer.vue';
import RoomNodeInvitation from './RoomNodeInvitation.vue';
import {ref} from 'vue';
import {useRoomClientCacheStore} from './stores/roomClientCache';
const cache=useRoomClientCacheStore();
const s=cache.settings;
const onboarding=ref(false);
</script>
<template>
  <section class="workspace-card">
    <WorkspaceCardHeader title="客户端文件加速" description="整合包导入、下载补齐与房间同步共用同一份资源索引。"><RouterLink to="/admin/mirror/resources?view=room">查看临时副本 →</RouterLink></WorkspaceCardHeader>
    <div class="workspace-footnote"><span>授权联机节点后即可使用源站补齐房间文件，接入密钥自动生成。</span><button type="button" class="secondary" @click="onboarding=true">接入联机节点</button></div>
    <div class="workspace-form-grid">
      <label class="workspace-check"><input v-model="s.room_cache_enabled" type="checkbox">启用临时文件加速</label>
      <label class="workspace-check"><input v-model="s.room_cache_public_lookup_enabled" type="checkbox">后台补查模组的公开发布地址</label>
      <label>保留时间 / 小时<input v-model.number="s.room_cache_ttl_hours" type="number" min="1" max="32"></label>
      <label>单文件上限 / MiB<input v-model.number="s.room_cache_max_file_mib" type="number" min="1" max="2048"></label>
    </div>
    <div class="workspace-footnote">优先使用已核验的快速来源，其次复用已有副本；未命中立即回退原下载流程，不等待源站下载。<RouterLink to="/admin/mirror/settings/downloads?view=sources">管理公共来源 →</RouterLink></div>
    <details class="workspace-help"><summary>匹配范围与有效时间</summary><p>依赖库、游戏核心等匹配阿里云、华为云、Maven 和已启用的 Minecraft 镜像；模组、资源包、光影匹配公开发布地址。只认相同哈希与文件大小，不按名称替换。</p><p>公共来源查询与临时补传开关独立；仓库映射按下载来源设置参与选路。补查开关只控制后台查找模组发布地址。保留时间仅约束玩家补传的临时副本；普通镜像副本按各类别的存储规则管理。</p><RouterLink to="/admin/mirror/settings/cloud?view=room">配置临时文件的本地与云端容量 →</RouterLink></details>
  </section>
  <WorkspaceDrawer v-if="onboarding" title="接入联机节点" @close="onboarding=false"><RoomNodeInvitation/></WorkspaceDrawer>
</template>
