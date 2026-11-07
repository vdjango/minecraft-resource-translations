<script setup lang="ts">
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import {computed,ref} from 'vue';
import {providerName,useCloudStorageStore} from './stores/cloudStorage';
import {bytes,stamp} from './stores/mirrorWorkspace';
import WorkspaceTabs from './WorkspaceTabs.vue';
import WorkspaceDrawer from './WorkspaceDrawer.vue';
import CloudUploadControl from './CloudUploadControl.vue';
import RoomStorageSettings from './RoomStorageSettings.vue';
const cloud=useCloudStorageStore(),editing=ref(''),provider=ref('rainyun');
const profile=computed(()=>cloud.profiles.find(p=>p.id===editing.value));
const status=computed(()=>cloud.snapshot.profiles.find((p:any)=>p.id===editing.value)||{});
const usage=(id:string)=>cloud.snapshot.profiles.find((p:any)=>p.id===id)||{};
function add(){cloud.add(provider.value);editing.value=cloud.profiles.at(-1)?.id||'';}
</script>
<template>
<div class="workspace-stack">
<CloudUploadControl/>
<WorkspaceTabs :items="[['spaces','云空间'],['local','本地磁盘'],['room','临时文件容量'],['links','下载链接']]" v-slot="{active}" param="view">
  <section v-if="active==='spaces'" class="workspace-card">
    <WorkspaceCardHeader title="存储空间"><select v-model="provider" aria-label="新增存储类型"><option value="rainyun">雨云 S3</option><option value="qiniu">七牛云</option><option value="s3">其他 S3</option></select><button type="button" @click="add">添加空间</button></WorkspaceCardHeader>
    <div class="workspace-table"><table><thead><tr><th>空间</th><th>已使用 / 上限</th><th>本月下载 / 预算</th><th>下载入口</th><th></th></tr></thead><tbody><tr v-for="p in cloud.profiles" :key="p.id"><td><strong>{{p.name||p.bucket||p.id}}</strong><small>{{providerName(p.provider)}} · {{p.bucket||'待配置'}}</small></td><td>{{bytes(usage(p.id).bytes)}} / {{p.quota_gib}} {{p.quota_unit||'GiB'}}</td><td>{{bytes(usage(p.id).egress?.bytes)}} / {{p.monthly_egress_gib||'不限'}} {{p.monthly_egress_gib?(p.quota_unit||'GiB'):''}}</td><td><span class="workspace-badge" :class="{warning:usage(p.id).health?.healthy===false}">{{!usage(p.id).configured?'待配置凭据':usage(p.id).health?.healthy===false?'线路异常':usage(p.id).health?.healthy?'可用':'等待检查'}}</span></td><td><button class="workspace-link" @click="editing=p.id">管理</button></td></tr><tr v-if="!cloud.profiles.length"><td colspan="5" class="workspace-empty">添加一个空间，再为资源指定存储去向。</td></tr></tbody></table></div>
    <div class="workspace-footnote">每个空间独立计算容量和下载预算。<RouterLink to="/admin/mirror/settings/downloads">分配资源去向 →</RouterLink></div>
  </section>
  <section v-else-if="active==='local'" class="workspace-card"><WorkspaceCardHeader title="服务器磁盘"><span class="workspace-hint">已缓存 {{bytes(cloud.usage.bytes)}} · 剩余 {{bytes(cloud.usage.free)}}</span></WorkspaceCardHeader><div class="workspace-form-grid"><label>普通镜像本地上限 / GiB<input v-model.number="cloud.local.quota_gib" type="number" min="1" max="100000"></label><label>磁盘至少保留 / GiB<input v-model.number="cloud.local.reserve_gib" type="number" min="10" max="100000"></label><label>空间不足时<select v-model="cloud.local.eviction"><option value="oldest_version">先清理旧版本</option><option value="lru">先清理最久未使用文件</option><option value="stop">停止新增</option></select></label><label>保护最新版本数<input v-model.number="cloud.local.protect_latest" type="number" min="0" max="50"></label></div><p class="workspace-hint">这些限制只作用于服务器磁盘。云端容量独立；上传仍需要短暂落盘。</p></section>
  <RoomStorageSettings v-else-if="active==='room'"/>
  <section v-else class="workspace-card"><WorkspaceCardHeader title="限时下载链接"/><div class="workspace-form"><label>签名链接有效期 / 分钟<input v-model.number="cloud.signedMinutes" type="number" min="30" max="1440"></label></div><p class="workspace-hint">仅影响私有空间的下载授权；玩家补传的临时副本按客户端加速的保留时间清理。</p></section>
</WorkspaceTabs>
<WorkspaceDrawer v-if="profile" :title="(profile.name||profile.id)+' · 存储配置'" @close="editing=''">
  <div class="workspace-form-grid"><label>名称<input v-model.trim="profile.name" maxlength="80"></label><label>Bucket<input v-model.trim="profile.bucket"></label><label>容量 / {{profile.quota_unit||'GiB'}}<input v-model.number="profile.quota_gib" type="number" min="1"></label><label>每月下载预算（0 不限）<input v-model.number="profile.monthly_egress_gib" type="number" min="0"></label><label class="full">HTTPS 下载域名<input v-model.trim="profile.domain" type="url"></label><label>每日上传上限<input v-model.number="profile.daily_upload_gib" type="number" min="1"></label><label>预算分流阈值 / %<input v-model.number="profile.egress_threshold" type="number" min="10" max="100"></label><label class="workspace-check full"><input v-model="profile.private" type="checkbox">私有空间，使用限时授权</label></div>
  <details class="workspace-help"><summary>连接参数</summary><div class="workspace-form-grid"><label>空间标识<input :value="profile.id" disabled></label><label>计量单位<select v-model="profile.quota_unit"><option value="GB">GB</option><option value="GiB">GiB</option></select></label><label class="full">对象前缀<input v-model.trim="profile.prefix"></label><template v-if="profile.provider!=='qiniu'"><label class="full">S3 Endpoint<input v-model.trim="profile.endpoint" type="url"></label><label>区域<input v-model.trim="profile.region"></label><label>寻址方式<select v-model="profile.addressing_style"><option value="path">路径</option><option value="virtual">Bucket 子域名</option></select></label></template><label v-else class="full">上传域名<input v-model.trim="profile.upload_host"></label></div></details>
  <details class="workspace-help"><summary>连接诊断与 HTTPS 证书</summary><p class="workspace-hint">{{status.configured?'服务端凭据已配置':'请在服务端配置此空间的凭据'}} · {{status.health?.error||status.health?.domain_error||'无已知线路错误'}}</p><p v-if="status.tls" class="workspace-hint">{{status.tls.message}} · 到期 {{stamp(status.tls.expires)}}</p><label v-if="profile.provider==='rainyun'" class="workspace-check"><input v-model="profile.tls_auto" type="checkbox">自动申请和续期 HTTPS 证书</label><div class="workspace-toolbar"><button type="button" class="secondary" :disabled="cloud.busy||!status.configured" @click="cloud.action('cloud_test',{profile:profile.id})">小文件读写检查</button><button v-if="profile.provider==='rainyun'" type="button" class="secondary" :disabled="cloud.busy" @click="cloud.action('cloud_tls_issue',{profile:profile.id})">检查证书</button></div><a v-if="status.tls?.downloadable" :href="'/v1/admin/mirror/certificate?profile='+encodeURIComponent(profile.id)" download>下载证书包</a></details>
  <template #footer><button type="button" class="secondary" @click="cloud.remove(cloud.profiles.indexOf(profile));editing=''">移除配置</button><button type="button" @click="editing=''">完成编辑</button></template>
</WorkspaceDrawer>
</div>
</template>
