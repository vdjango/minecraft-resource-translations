<script setup lang="ts">
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import {watch} from 'vue';
import {useSourceQualityStore} from './stores/sourceQuality';
const props=defineProps<{active:boolean;authenticated:boolean}>();
const store=useSourceQualityStore();
const status:Record<string,string>={healthy:'可用',unknown:'待观测',slow:'持续慢',unavailable:'当前线路不可达'};
const name=(url:string)=>{const value=url.split('/').pop()||url;try{return decodeURIComponent(value);}catch{return value;}};
const host=(url:string)=>new URL(url).hostname;
watch(()=>props.active&&props.authenticated,active=>{if(active)void store.loadMappings();},{immediate:true});
</script>
<template>
<section class="workspace-card public-mappings">
  <WorkspaceCardHeader title="内容资源公开映射" description="普通下载、整合包补齐和房间共用的公开文件地址。记录信息不占实体缓存；私人文件不收录。"><span class="badge">{{store.mappingTotal.toLocaleString()}} 条地址</span></WorkspaceCardHeader>
  <div class="mapping-filters"><label>文件名或完整哈希<input v-model.trim="store.filters.query" placeholder="搜索文件名、SHA-1 / SHA-256" maxlength="160" @keyup.enter="store.loadMappings()"></label><label>公开平台<select v-model="store.filters.provider" @change="store.loadMappings()"><option value="">全部</option><option value="curseforge">CurseForge</option><option value="modrinth">Modrinth</option><option value="modrinth_only">仅有 Modrinth 地址</option></select></label><label>目录状态<select v-model="store.filters.state" @change="store.loadMappings()"><option value="">全部</option><option value="available">可用地址</option><option value="slow">已标记慢速</option><option value="unavailable">地址失效</option></select></label><button type="button" class="secondary" :disabled="store.mappingBusy" @click="store.loadMappings()">刷新列表</button></div>
  <p v-if="store.mappingError" role="status">{{store.mappingError}} · 已有数据保留</p>
  <div class="table-scroll" :aria-busy="store.mappingBusy"><table><thead><tr><th>文件 / 哈希</th><th>下载去向</th><th>当前质量</th><th>核对时间</th></tr></thead><tbody>
    <template v-if="store.mappingBusy&&!store.mappings.length"><tr v-for="i in 4" :key="i"><td v-for="j in 4" :key="j"><span class="mapping-skeleton"></span></td></tr></template>
    <tr v-else-if="!store.mappings.length"><td colspan="4" class="record-empty">没有符合条件的公开映射</td></tr>
    <tr v-for="file in store.mappings" :key="file.url"><td class="mapping-name">{{name(file.url)}}<small>{{file.sha1||file.sha256}}</small><small>{{(file.size/1048576).toFixed(2)}} MiB</small></td><td><a :href="file.url" rel="noopener noreferrer" target="_blank">{{host(file.url)}}</a><small>{{file.confirmed_fast?'已确认快速，优先直下':file.health==='slow'||file.health==='unavailable'?'优先已就绪缓存，无副本时尝试其他来源':'速度待观测；按下载策略和可用副本选路'}} </small></td><td>{{file.state==='unavailable'?'此地址失效':status[file.health]||'待观测'}}<small>{{file.effective_speed_bps==null?'尚无速度数据':(file.effective_speed_bps/1024).toFixed(0)+' KiB/s'}}</small></td><td>{{new Date(file.updated*1000).toLocaleString('zh-CN')}}</td></tr>
  </tbody></table></div>
  <button v-if="store.mappingCursor" type="button" class="secondary" :disabled="store.mappingBusy" @click="store.loadMappings(true)">加载更多</button>
  <p class="muted small">只有文件哈希和大小相同才作为替代地址。当前速度来自本站到来源的采样；目录状态是上次文件核对结果。关闭房间后，私人文件和搜索记录清理，公开映射独立保留。</p>
</section>
</template>
<style scoped>
.mapping-filters{display:grid;grid-template-columns:minmax(160px,2fr) 1fr 1fr auto;align-items:end;gap:12px;margin-bottom:18px}.mapping-filters label{display:grid;gap:8px}.mapping-name{max-width:300px;overflow-wrap:anywhere}.public-mappings small{display:block;margin-top:6px;color:var(--muted,#bfb7cf);overflow-wrap:anywhere}.mapping-skeleton{display:block;min-width:70px;height:18px;border-radius:5px;background:#3e344e;animation:pulse 1.2s infinite alternate}@keyframes pulse{to{opacity:.35}}@media(max-width:800px){.mapping-filters{grid-template-columns:1fr 1fr}}
</style>
