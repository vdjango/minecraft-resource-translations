<script setup lang="ts">
import {computed,ref,watch} from 'vue';
import {useRoute,useRouter} from 'vue-router';
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import WorkspaceDrawer from './WorkspaceDrawer.vue';
import {useMirrorInventoryStore,inventoryReasons} from './stores/mirrorInventory';
import {bytes,stamp,useMirrorWorkspaceStore} from './stores/mirrorWorkspace';
const route=useRoute(),router=useRouter(),inventory=useMirrorInventoryStore(),workspace=useMirrorWorkspaceStore();
const detail=ref<any>(null),query=ref(String(route.query.q||''));
const page=computed(()=>inventory.page),category=computed(()=>String(route.query.category||'')),handling=computed(()=>String(route.query.handling||''));
const states:Record<string,string>={stored:'已有副本',retrying:'等待自动重试',eligible:'自动缓存范围内',excluded:'不自动下载'};
const platforms:Record<string,string>={windows:'Windows',macos:'macOS',linux:'Linux'};
const profile=(id:string)=>workspace.data.settings?.qiniu_profiles?.find((p:any)=>p.id===id)?.name||id;
const destination=(file:any)=>!file.eligible?'不因自动同步保存':file.destination.mode==='local'?'服务器磁盘':profile(file.destination.profile)+(file.destination.mode==='copy'?' + 本地':'');
function filter(values:Record<string,string|number|undefined>){void router.replace({path:route.path,query:{...route.query,...values,offset:undefined}});}
function load(){void inventory.load({category:category.value,handling:handling.value,query:String(route.query.q||''),offset:Number(route.query.offset||0)});}
watch(()=>[route.query.category,route.query.handling,route.query.q,route.query.offset],load,{immediate:true});
</script>
<template>
<section class="workspace-card">
  <WorkspaceCardHeader title="资源清单" :description="inventory.summary?.coverage||'查看已登记文件及其缓存去向。'"><RouterLink to="/admin">返回总览 →</RouterLink></WorkspaceCardHeader>
  <form class="workspace-toolbar inventory-filters" @submit.prevent="filter({q:query||undefined})">
    <label>资源类型<select :value="category" @change="filter({category:($event.target as HTMLSelectElement).value||undefined})"><option value="">全部类型</option><option v-for="item in inventory.summary?.categories||[]" :key="item.key" :value="item.key">{{item.label}} · {{item.total}}</option></select></label>
    <label>处理范围<select :value="handling" @change="filter({handling:($event.target as HTMLSelectElement).value||undefined})"><option value="">全部条目</option><option value="eligible">符合自动缓存条件</option><option value="stored">已有本地或云端副本</option><option value="excluded">不自动下载</option><option v-for="(label,key) in inventoryReasons" :key="key" :value="key">{{label}}</option></select></label>
    <label class="inventory-search">查找文件<input v-model="query" placeholder="文件名、路径或 SHA1" maxlength="160" type="search"></label><button type="submit" class="secondary">查找</button><button type="button" class="workspace-link" :disabled="inventory.loading" @click="load">刷新列表</button>
  </form>
  <p v-if="inventory.error" role="alert" class="workspace-alert">{{inventory.error}}</p>
  <div class="workspace-footnote"><span>筛选结果 {{page?.matched?.toLocaleString()??'—'}} 条 / 清单共 {{page?.total?.toLocaleString()??'—'}} 条</span><span>清单更新 {{stamp(page?.updated_at)}}</span></div>
  <div class="workspace-table inventory-table" :aria-busy="inventory.loading"><table><thead><tr><th>文件 / 类型</th><th>平台</th><th>处理方式</th><th>实际副本</th><th>大小</th><th></th></tr></thead><tbody>
    <template v-if="!page&&inventory.loading"><tr v-for="n in 7" :key="n"><td colspan="6"><span class="workspace-skeleton"/></td></tr></template>
    <tr v-for="file in page?.files||[]" :key="file.url"><td><strong class="workspace-filename" :title="file.path">{{file.name}}</strong><small>{{file.label}}</small></td><td>{{file.platforms.length?file.platforms.map((p:string)=>platforms[p]||p).join('、'):'未限定'}}</td><td><span>{{file.reason_label||file.policy_label}}</span><small>{{file.eligible?destination(file):states[file.state]}}</small></td><td><span v-if="file.local">本地磁盘</span><span v-for="copy in file.cloud" :key="copy.profile" class="inventory-copy">{{profile(copy.profile)}}</span><small v-if="!file.local&&!file.cloud.length">未保存实体{{file.failure?' · 自动重试中':''}}</small></td><td class="workspace-nowrap">{{file.size==null?'未标注':bytes(file.size)}}</td><td><button type="button" class="workspace-link" @click="detail=file">详情</button></td></tr>
    <tr v-if="!inventory.loading&&!page?.files?.length"><td colspan="6" class="workspace-empty">没有符合条件的清单条目</td></tr>
  </tbody></table></div>
  <footer class="workspace-pagination"><span>每页 50 条 · 第 {{Math.floor((page?.offset||0)/50)+1}} 页</span><button type="button" class="secondary" :disabled="!page?.offset||inventory.loading" @click="router.replace({query:{...route.query,offset:Math.max(0,page.offset-50)}})">上一页</button><button type="button" class="secondary" :disabled="page?.next_offset==null||inventory.loading" @click="router.replace({query:{...route.query,offset:page.next_offset}})">下一页</button></footer>
</section>
<WorkspaceDrawer v-if="detail" title="清单文件详情" @close="detail=null"><dl class="workspace-details"><dt>文件</dt><dd>{{detail.path}}</dd><dt>资源类型</dt><dd>{{detail.label}}</dd><dt>原始下载地址</dt><dd>{{detail.url}}</dd><dt>SHA1</dt><dd>{{detail.sha1||'清单未提供'}}</dd><dt>类别下载策略</dt><dd>{{detail.policy_label}}</dd><dt>自动同步判断</dt><dd>{{detail.reason_label||'符合平台和类别条件，自动按存储设置补齐'}}</dd><dt>计划存放位置</dt><dd>{{destination(detail)}}<template v-if="detail.eligible&&detail.destination.mode!=='local'"> / {{detail.destination.path}}</template></dd><dt>当前实际位置</dt><dd><p v-if="detail.local">服务器磁盘</p><p v-for="copy in detail.cloud" :key="copy.profile">{{profile(copy.profile)}} / {{copy.object_key}}</p><span v-if="!detail.local&&!detail.cloud.length">未保存实体文件</span></dd><template v-if="detail.failure"><dt>失败原因</dt><dd>{{detail.failure.status?'HTTP '+detail.failure.status+' · ':''}}{{detail.failure.error}}</dd><dt>下次重试</dt><dd>{{stamp(detail.failure.next_retry)}}</dd></template></dl></WorkspaceDrawer>
</template>
<style scoped>
.inventory-filters{align-items:end;gap:10px}.inventory-search{flex:1;min-width:180px}.inventory-table{max-height:530px}.inventory-table td:first-child{max-width:300px}.inventory-copy{display:block}.inventory-filters select{max-width:260px}@media(max-width:650px){.inventory-filters label{flex:1;min-width:140px}.inventory-filters select{max-width:100%}}
</style>
