<script setup lang="ts">
import {computed,ref,watch} from 'vue';
import {useRoute,useRouter} from 'vue-router';
import {bytes,filename,mirrorCategories,stamp,useMirrorWorkspaceStore} from './stores/mirrorWorkspace';
import WorkspaceDrawer from './WorkspaceDrawer.vue';
import LocalCacheCleanup from './LocalCacheCleanup.vue';
const props=defineProps<{temporary?:boolean}>();
const route=useRoute(),router=useRouter(),storage=computed(()=>String(route.query.storage||''));
const store=useMirrorWorkspaceStore(),query=ref(''),category=ref(''),files=ref<any[]>([]),busy=ref(false),error=ref(''),details=ref<any>(null),next=ref<string|null>(null),cursors=ref<(string|null)[]>([null]),page=ref(0),cleanup=ref(false);
const labels:Record<string,string>={uploading:'上传中',cached:'可下载',awaiting_upload:'等待补传',redirect:'外部直下',expired:'已过期'};
const kinds=Object.fromEntries(mirrorCategories);
const room=computed(()=>store.data.room_client_cache||{});
let revision=0;
async function load(reset=false){
  if(reset){page.value=0;cursors.value=[null];}
  const request=++revision;busy.value=true;error.value='';
  try{const value=await store.request({action:props.temporary?'room_files':'cache_files',query:query.value,category:props.temporary?'':category.value,storage:props.temporary?'':storage.value,cursor:cursors.value[page.value]});if(request!==revision)return;files.value=value.files||[];next.value=value.next_cursor||null;}
  catch(cause){if(request===revision)error.value=(cause as Error).message;}
  finally{if(request===revision)busy.value=false;}
}
function move(direction:number){if(direction>0){if(!next.value)return;cursors.value[page.value+1]=next.value;}page.value+=direction;void load();}
watch(()=>[props.temporary,storage.value],()=>{details.value=null;void load(true);},{immediate:true});
watch(()=>store.data.operations?.observed_at,()=>{if(!busy.value)void load();});
</script>
<template>
  <div v-if="temporary" class="workspace-status"><span>过期文件自动清理 · 当前上传 {{room.uploads||room.active_uploads||0}} 个</span><RouterLink to="/admin">副本与容量统计 →</RouterLink></div>
  <section class="workspace-card">
    <form class="workspace-toolbar" @submit.prevent="load(true)"><input v-model.trim="query" type="search" maxlength="160" aria-label="文件名或哈希" placeholder="搜索文件名或哈希"><select v-if="!temporary" v-model="category" aria-label="资源类别" @change="load(true)"><option value="">全部类别</option><option v-for="[key,label] in mirrorCategories" :key="key" :value="key">{{label}}</option></select><select v-if="!temporary" :value="storage" aria-label="实际存储位置" @change="router.replace({query:{...route.query,storage:($event.target as HTMLSelectElement).value||undefined}})"><option value="">全部存储位置</option><option value="local">本地磁盘</option><option value="cloud">云端副本</option></select><button type="submit" class="secondary">搜索</button><button v-if="!temporary" type="button" class="secondary toolbar-end" @click="cleanup=true">清理历史副本</button></form>
    <p v-if="error" class="workspace-error" role="status">{{error}} · 已有数据保留</p>
    <div class="workspace-table" :aria-busy="busy"><table><thead><tr><th>文件</th><th>{{temporary?'状态':'类别'}}</th><th>大小</th><th>实际位置</th><th>{{temporary?'到期时间':'缓存时间'}}</th><th></th></tr></thead><tbody>
      <template v-if="busy&&!files.length"><tr v-for="n in 6" :key="n"><td v-for="j in 6" :key="j"><span class="workspace-skeleton"/></td></tr></template>
      <tr v-for="file in files" :key="file.url||file.digest"><td class="file-name" :title="file.path">{{filename(file.path||file.url)}}<small>{{(file.sha1||file.object||file.digest||'').slice(0,16)}}…</small></td><td><span class="workspace-pill">{{temporary?labels[file.state]||file.state:kinds[file.category]||file.category}}</span></td><td>{{bytes(file.size)}}</td><td>{{file.cloud_profiles?'云端 · '+file.cloud_profiles:''}}{{file.cloud_profiles&&(file.local||file.stored_bytes)?' + ':''}}{{file.local||file.stored_bytes?'本地':''}}{{!file.cloud_profiles&&!file.local&&!file.stored_bytes?'无实体':''}}</td><td>{{stamp(temporary?file.expires_at:file.fetched)}}</td><td><button type="button" class="text-action" @click="details=file">详情</button></td></tr>
      <tr v-if="!busy&&!files.length"><td colspan="6" class="workspace-empty">{{temporary?'暂无有效临时文件，过期后自动清理':'暂无符合条件的缓存文件'}}</td></tr>
    </tbody></table></div>
    <footer class="workspace-pagination"><span>第 {{page+1}} 页 · {{files.length}} 项</span><button type="button" class="secondary" :disabled="busy||page===0" @click="move(-1)">上一页</button><button type="button" class="secondary" :disabled="busy||!next" @click="move(1)">下一页</button></footer>
  </section>
  <WorkspaceDrawer v-if="details" :title="filename(details.path||details.url)" @close="details=null"><dl class="workspace-details"><dt>完整路径</dt><dd>{{details.path||details.url}}</dd><dt>文件身份</dt><dd>{{details.digest||details.object||details.sha1}}</dd><dt>文件大小</dt><dd>{{bytes(details.size)}}</dd><dt>{{temporary?'当前原因':'下载来源'}}</dt><dd>{{details.reason||details.source||details.url}}</dd><template v-if="temporary"><dt>到期清理</dt><dd>{{stamp(details.expires_at)}} · {{details.references_count||0}} 处引用</dd></template><dt>存储空间</dt><dd>{{details.cloud_profiles||'无云端副本'}}</dd></dl></WorkspaceDrawer>
  <WorkspaceDrawer v-if="cleanup" title="清理历史本地副本" @close="cleanup=false"><LocalCacheCleanup/></WorkspaceDrawer>
</template>
