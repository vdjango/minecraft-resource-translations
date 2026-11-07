<script setup lang="ts">
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import {computed,ref,watch} from 'vue';
import {useRoute} from 'vue-router';
import {useMirrorWorkspaceStore,bytes,stamp} from './stores/mirrorWorkspace';
import WorkspaceTabs from './WorkspaceTabs.vue';
import PublicFileMappings from './PublicFileMappings.vue';
import SourceQualityPanel from './SourceQualityPanel.vue';
import MirrorInventoryFiles from './MirrorInventoryFiles.vue';
const workspace=useMirrorWorkspaceStore();
const route=useRoute(),source=ref(''),offset=ref(0),page=ref<any>({files:[],total:0}),loading=ref(false),error=ref('');
const sources=computed(()=>(workspace.data.settings?.source_registries||[]).map((registry:any)=>({...registry,...(workspace.data.source_statistics||[]).find((item:any)=>item.source===registry.id)})));
const scan=computed(()=>workspace.data.automatic_sources||{});
async function load(){loading.value=true;try{page.value=await workspace.request({action:'source_files',source:source.value,offset:offset.value});error.value='';}catch(cause){error.value=(cause as Error).message;}finally{loading.value=false;}}
watch(()=>[route.query.view,source.value,offset.value],()=>{if(route.query.view==='repositories')void load();},{immediate:true});
</script>
<template>
<WorkspaceTabs :items="[['inventory','资源清单'],['sources','扫描与覆盖'],['repositories','仓库映射'],['files','模组映射'],['quality','来源质量']]" v-slot="{active}">
  <MirrorInventoryFiles v-if="active==='inventory'"/>
  <section v-else-if="active==='sources'" class="workspace-card">
    <WorkspaceCardHeader title="公开来源覆盖" description="自动扫描仓库、核验文件，再用于下载重定向。"><RouterLink to="/admin/mirror/settings/downloads?view=sources">管理下载来源 →</RouterLink></WorkspaceCardHeader>
    <div class="workspace-status"><span class="status-dot"/><span>{{workspace.data.operations?.source_scanning?'正在分批核验':'自动扫描中'}} · 最近完成 {{stamp(scan.generated_at)}} · 匹配 {{scan.matched||0}} 个文件</span></div>
    <div class="workspace-table"><table><thead><tr><th>来源</th><th>已核验文件</th><th>本地副本</th><th>下载重定向</th></tr></thead><tbody>
      <tr v-for="item in sources" :key="item.id"><td><strong>{{item.name}}</strong><small>{{item.enabled?'自动参与匹配':'已停用'}}</small></td><td>{{item.files||0}}</td><td>{{item.local_files||0}} · {{bytes(item.local_bytes)}}</td><td>{{item.redirects||0}}</td></tr>
      <tr v-if="!sources.length"><td colspan="4" class="workspace-empty">扫描结果会自动显示在这里。</td></tr>
    </tbody></table></div>
  </section>
  <section v-else-if="active==='repositories'" class="workspace-card">
    <WorkspaceCardHeader title="仓库映射" description="已核验文件的来源与下载去向。"><select v-model="source" aria-label="来源" @change="offset=0"><option value="">全部来源</option><option v-for="item in sources" :key="item.id" :value="item.id">{{item.name}}</option></select><span class="workspace-hint">{{page.total}} 条</span></WorkspaceCardHeader>
    <p v-if="error" class="workspace-alert">{{error}} · 已有数据保留</p>
    <div class="workspace-table" :aria-busy="loading"><table><thead><tr><th>文件</th><th>来源</th><th>本地副本</th><th>下载重定向</th></tr></thead><tbody>
      <template v-if="loading&&!page.files.length"><tr v-for="n in 5" :key="n"><td colspan="4"><span class="workspace-skeleton"/></td></tr></template>
      <tr v-for="file in page.files" :key="file.source+file.path"><td><span class="workspace-filename" :title="file.path">{{file.path}}</span></td><td>{{sources.find((item:any)=>item.id===file.source)?.name||file.source}}</td><td>{{file.local_bytes?bytes(file.local_bytes):'无'}}</td><td>{{file.redirects||0}}</td></tr>
      <tr v-if="!loading&&!page.files.length"><td colspan="4" class="workspace-empty">暂无匹配文件</td></tr>
    </tbody></table></div>
    <div class="workspace-pagination"><span>第 {{Math.floor(offset/50)+1}} 页</span><button class="secondary" :disabled="!offset||loading" @click="offset=Math.max(0,offset-50)">上一页</button><button class="secondary" :disabled="!page.next_offset||loading" @click="offset=page.next_offset">下一页</button></div>
  </section>
  <PublicFileMappings v-else-if="active==='files'" :active="true" :authenticated="true"/>
  <SourceQualityPanel v-else/>
</WorkspaceTabs>
</template>
