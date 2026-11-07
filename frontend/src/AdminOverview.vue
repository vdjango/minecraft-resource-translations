<script setup lang="ts">
import {computed,onMounted,onUnmounted,watch} from 'vue';
import {useRoute,useRouter} from 'vue-router';
import AdminAccess from './AdminAccess.vue';
import WorkspaceTabs from './WorkspaceTabs.vue';
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import AdminTrafficStats from './AdminTrafficStats.vue';
import AdminTranslationStats from './AdminTranslationStats.vue';
import {bytes,stamp,useMirrorWorkspaceStore} from './stores/mirrorWorkspace';
import {useMirrorInventoryStore,inventoryLink} from './stores/mirrorInventory';
import './mirrorWorkspace.css';
import './mirrorWorkspaceCompact.css';
const props=defineProps<{seed:Record<string,string>;views:any}>();
const route=useRoute(),router=useRouter(),workspace=useMirrorWorkspaceStore(),inventory=useMirrorInventoryStore();
const summary=computed(()=>inventory.summary),s=computed(()=>workspace.data);
const cloud=computed(()=>(summary.value?.storage?.cloud||[]).filter((p:any)=>p.scope==='mirror'));
const temporary=computed(()=>(summary.value?.storage?.cloud||[]).filter((p:any)=>p.scope==='room'));
const cloudFiles=computed(()=>cloud.value.reduce((n:number,p:any)=>n+p.files,0)),cloudBytes=computed(()=>cloud.value.reduce((n:number,p:any)=>n+p.bytes,0));
const tempFiles=computed(()=>temporary.value.reduce((n:number,p:any)=>n+p.files,0));
const profiles=computed(()=>s.value.settings?.qiniu_profiles||[]);
const cloudName=(id:string)=>profiles.value.find((p:any)=>p.id===id)?.name||id;
let timer:ReturnType<typeof setInterval>|undefined;
async function refresh(){await Promise.all([workspace.refresh(),inventory.load({},true)]);}
function authorized(){workspace.unauthorized=false;props.views.adminAccess.authenticated(true);void refresh();}
function access(){props.views.adminAccess.mount({session:'/v1/admin/session',login:'/v1/admin/session',setup:'/v1/admin/setup',mode:props.seed.authMode||'login',authorized});if(props.seed.authMode==='session'||props.views.access.authenticated)authorized();}
async function logout(){await fetch('/v1/admin/session',{method:'DELETE',credentials:'same-origin'});props.views.adminAccess.authenticated(false);props.views.access.options={...props.views.access.options,mode:'login'};}
watch(()=>workspace.unauthorized,value=>{if(value)props.views.adminAccess.authenticated(false);});
watch(()=>route.path,path=>{const aliases:Record<string,string>={'/admin/mirror':'resources','/admin/traffic':'traffic','/admin/usage':'usage'};if(aliases[path])void router.replace({path:'/admin',query:{...route.query,view:aliases[path]}});},{immediate:true});
onMounted(()=>{access();timer=setInterval(()=>{if(props.views.access.authenticated&&!document.hidden&&(!route.query.view||route.query.view==='resources'))void refresh();},30000);});
onUnmounted(()=>{if(timer)clearInterval(timer);});
</script>
<template>
<main class="admin-content mirror-workspace">
<section v-if="views.access.visible" class="admin-access-host"><AdminAccess v-if="views.access.options" :options="views.access.options" :visible="true"/></section>
<template v-if="views.access.authenticated">
  <header class="workspace-header"><div><h1>总览</h1><p>资源、访问与翻译费用，统一查看。</p></div><div class="workspace-header-actions"><span class="workspace-live"><i/> 资源每 30 秒更新</span><button type="button" class="workspace-link" @click="logout">退出</button></div></header>
  <WorkspaceTabs :items="[['resources','资源与存储'],['traffic','翻译接口访问'],['usage','翻译用量与费用']]" v-slot="{active}">
    <div v-if="active==='resources'" class="workspace-stack">
      <p v-if="inventory.error||workspace.error" class="workspace-alert" role="alert">{{inventory.error||workspace.error}}</p>
      <div v-if="!summary" class="workspace-card" :aria-busy="inventory.loading"><span v-for="n in 7" :key="n" class="workspace-skeleton"/><button v-if="!inventory.loading" type="button" class="secondary" @click="refresh">重新读取</button></div>
      <template v-else>
        <div class="workspace-metrics overview-metrics">
          <RouterLink :to="inventoryLink()"><span>资源清单条目</span><strong>{{summary.total.toLocaleString()}}</strong><small>下载地址登记 · 查看组成 →</small></RouterLink>
          <RouterLink :to="inventoryLink('','eligible')"><span>符合自动缓存条件</span><strong>{{summary.eligible.toLocaleString()}}</strong><small>按类别和平台筛选 · 查看文件 →</small></RouterLink>
          <RouterLink to="/admin/mirror/resources?storage=cloud"><span>镜像云端副本</span><strong>{{cloudFiles.toLocaleString()}}</strong><small>{{bytes(cloudBytes)}} · 按存储空间计数 →</small></RouterLink>
          <RouterLink to="/admin/mirror/resources?storage=local"><span>镜像本地副本</span><strong>{{summary.storage.local_files.toLocaleString()}}</strong><small>{{bytes(summary.storage.local_bytes)}} · 服务器磁盘 →</small></RouterLink>
        </div>
        <section class="workspace-card"><WorkspaceCardHeader title="清单里有什么"><span class="workspace-hint">{{summary.categories.length}} 种类型</span><button type="button" class="workspace-link" :disabled="inventory.loading" @click="refresh">刷新</button><RouterLink :to="inventoryLink()">查看全部文件 →</RouterLink></WorkspaceCardHeader>
          <div class="workspace-table overview-composition"><table><thead><tr><th>资源类型</th><th>清单条目</th><th>符合自动缓存</th><th>已有副本的条目</th></tr></thead><tbody><tr v-for="item in summary.categories" :key="item.key"><td><RouterLink :to="inventoryLink(item.key)" :title="item.purpose">{{item.label}}</RouterLink><div class="composition-bar"><span :style="{width:(item.total/summary.total*100)+'%'}"/></div></td><td><RouterLink :to="inventoryLink(item.key)">{{item.total.toLocaleString()}}</RouterLink></td><td><RouterLink :to="inventoryLink(item.key,'eligible')">{{item.eligible.toLocaleString()}}</RouterLink></td><td><RouterLink :to="inventoryLink(item.key,'stored')">{{item.stored.toLocaleString()}}</RouterLink></td></tr></tbody></table></div>
          <details class="workspace-help"><summary>清单、缓存条件和实际副本有什么区别？</summary><p>清单是已登记的下载地址；符合自动缓存条件的文件才进入自动补齐范围；本地和云端副本是已保存的实体。重复内容可以共用实体，因此条目数与副本数不一定相同。</p><p>{{summary.coverage}} 未选平台、仅重定向、开发映射、服务端和重复压缩格式会单独标明，不会全部下载。</p></details>
        </section>
        <div class="overview-bottom">
          <section class="workspace-card"><WorkspaceCardHeader title="实际存放位置"><RouterLink to="/admin/mirror/settings/cloud">存储设置 →</RouterLink></WorkspaceCardHeader><ul class="overview-rows"><li><span>服务器磁盘<small>普通镜像副本</small></span><strong>{{bytes(summary.storage.local_bytes)}}</strong></li><li v-for="space in cloud" :key="space.profile"><span>{{cloudName(space.profile)}}<small>普通镜像 · {{space.files}} 个副本</small></span><strong>{{bytes(space.bytes)}}</strong></li><li><RouterLink to="/admin/mirror/resources?view=room">客户端临时文件<small>单独管理保留时间与容量</small></RouterLink><strong>{{tempFiles}} 个云端副本</strong></li></ul></section>
          <section class="workspace-card"><WorkspaceCardHeader title="自动任务状态"><RouterLink to="/admin/mirror/tasks">查看任务 →</RouterLink></WorkspaceCardHeader><ul class="overview-rows"><li><span>清单筛选<small>{{s.automation?.state==='watching'?'本轮完成':'后台分批处理'}}</small></span><RouterLink :to="inventoryLink()">{{s.automation?.position||0}} / {{s.automation?.total||summary.total}} 条</RouterLink></li><li><span>回源下载<small>下载中 {{s.operations?.transfers?.length||0}} · 等待重试 {{s.operations?.failed_queue?.count||0}}</small></span><RouterLink to="/admin/mirror/tasks">查看文件 →</RouterLink></li><li><span>云端上传<small>{{s.cloud_storage?.paused?'已暂停':'自动运行'}} · 排队 {{s.cloud_storage?.queue?.queued?.count||0}}</small></span><RouterLink to="/admin/mirror/tasks?channel=cloud">查看队列 →</RouterLink></li></ul></section>
        </div>
        <div class="workspace-footnote"><span>清单更新 {{stamp(summary.updated_at)}} · 状态读取 {{stamp(summary.generated_at)}}</span><span>这里只统计当前源站；另一台节点不会混算。</span></div>
      </template>
    </div>
    <AdminTrafficStats v-else-if="active==='traffic'"/>
    <AdminTranslationStats v-else/>
  </WorkspaceTabs>
</template>
</main>
</template>
<style scoped>
.overview-metrics>a{display:grid;gap:5px;padding:16px;border:1px solid var(--border,#363048);border-radius:10px;color:inherit;text-decoration:none;background:var(--surface,#1a1528)}.overview-metrics>a:hover{border-color:var(--accent,#9876ec)}.overview-metrics span,.overview-metrics small{font-size:12px;color:var(--muted,#aaa2bd)}.overview-metrics strong{font-size:25px}.overview-composition{max-height:350px}.overview-composition td{padding-top:9px;padding-bottom:9px}.overview-composition th:not(:first-child),.overview-composition td:not(:first-child){text-align:right}.composition-bar{height:3px;background:var(--border,#363048);border-radius:3px;margin-top:7px;max-width:220px}.composition-bar span{display:block;height:100%;min-width:2px;background:var(--accent,#9876ec);border-radius:3px}.overview-bottom{display:grid;grid-template-columns:1fr 1fr;gap:14px}.overview-rows{list-style:none;margin:0;padding:0}.overview-rows li{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:11px 0;border-bottom:1px solid var(--border,#363048)}.overview-rows li:last-child{border:0}.overview-rows small{display:block;margin-top:4px;color:var(--muted,#aaa2bd);font-size:12px}.overview-rows strong{font-size:13px;white-space:nowrap}@media(max-width:800px){.overview-bottom{grid-template-columns:1fr}.overview-metrics{grid-template-columns:1fr 1fr}}
</style>
