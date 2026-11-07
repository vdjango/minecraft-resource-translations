<script setup lang="ts">
import WorkspaceCardHeader from '../WorkspaceCardHeader.vue';
import {computed,onMounted,ref,watch} from 'vue';
import AdminAccess from '../AdminAccess.vue';
import WorkspaceDrawer from '../WorkspaceDrawer.vue';
import RoomNodeInvitation from '../RoomNodeInvitation.vue';
import {useNodeAccessStore} from '../stores/nodeAccess';
import {useRoomClientCacheStore} from '../stores/roomClientCache';
import {stamp} from '../stores/mirrorWorkspace';
import '../mirrorWorkspace.css';
import '../mirrorWorkspaceCompact.css';

const props=defineProps<{seed:Record<string,string>;views:any}>();
const federation=useNodeAccessStore(),rooms=useRoomClientCacheStore();
const onboarding=ref(false);
const entries=computed(()=>[
  ...federation.nodes.map(node=>({key:'federation:'+node.id,kind:'federation',name:node.name||node.id,address:node.public_url||'',region:node.region||'',state:node.state,online:!!node.online,seen:node.last_seen||0,record:node})),
  ...rooms.nodes.map(node=>({key:'room:'+node.issuer,kind:'room',name:node.name||node.node_id||node.issuer,address:node.node_id||'',region:'',state:node.state,online:!!node.enabled&&node.state==='connected',seen:node.claimed_at||0,record:node})),
]);
function refresh(){void federation.refresh();void rooms.refresh().catch(()=>{});}
onMounted(()=>{
  props.views.adminAccess.mount({session:'/v1/admin/session',login:'/v1/admin/session',setup:'/v1/admin/setup',mode:props.seed.authMode||'login',authorized(){federation.unauthorized=false;props.views.adminAccess.authenticated(true);refresh();}});
  if(props.seed.authMode==='session'){props.views.adminAccess.authenticated(true);refresh();}
});
watch(()=>federation.unauthorized,value=>{if(value)props.views.adminAccess.authenticated(false);});
async function logout(){await fetch('/v1/admin/session',{method:'DELETE',credentials:'same-origin'});props.views.adminAccess.authenticated(false);props.views.access.options={...props.views.access.options,mode:'login'};}
async function revoke(issuer:string){try{await rooms.revoke(issuer);}catch{}}
function status(entry:(typeof entries.value)[number]){
  if(entry.kind==='room')return !entry.record.enabled?'已撤销':entry.state==='pending'?'等待领取':entry.state==='expired'?'邀请已过期':'已授权';
  return entry.online?'在线':entry.state==='pending'?'待验证':entry.state==='revoked'?'已撤销':'离线';
}
</script>

<template>
<main class="admin-content mirror-workspace node-console">
  <section v-if="views.access.visible" class="admin-access-host"><AdminAccess v-if="views.access.options" :options="views.access.options" :visible="true"/></section>
  <template v-if="views.access.authenticated">
    <header class="workspace-header"><div><h1>节点管理</h1><p>同一套节点程序，按已启用的能力参与服务。</p></div><button type="button" class="workspace-link" @click="logout">退出</button></header>
    <section class="workspace-card">
      <WorkspaceCardHeader title="已接入节点" description="统一查看节点及其服务能力。"><button type="button" class="secondary" :disabled="federation.loading||rooms.busy" @click="refresh">刷新</button><button type="button" @click="onboarding=true">接入节点</button></WorkspaceCardHeader>
      <div class="workspace-inline-stats"><span>译文分发 <b>{{federation.nodes.length}}</b></span><span>房间文件 <b>{{rooms.nodes.filter(node=>node.enabled).length}}</b></span><span>译文在线 <b>{{federation.online}}</b></span></div>
      <p v-if="federation.error||rooms.error" class="workspace-alert" role="alert">{{federation.error||rooms.error}}</p>
      <p v-if="rooms.feedback" class="workspace-hint" role="status">{{rooms.feedback}}</p>
      <div class="workspace-table" :aria-busy="federation.loading||rooms.busy"><table><thead><tr><th>节点</th><th>参与能力</th><th>状态</th><th>最近活动</th><th>操作</th></tr></thead><tbody>
        <template v-if="federation.loading&&!federation.ready"><tr v-for="n in 4" :key="n"><td colspan="5"><span class="workspace-skeleton"/></td></tr></template>
        <tr v-for="entry in entries" :key="entry.key"><td><strong>{{entry.name}}</strong><small v-if="entry.address">{{entry.address}}{{entry.region?' · '+entry.region:''}}</small></td><td><span class="workspace-badge">{{entry.kind==='room'?'房间文件':'译文分发'}}</span></td><td><span class="workspace-badge" :class="{warning:!entry.online}">{{status(entry)}}</span></td><td>{{stamp(entry.seen)}}</td><td><button v-if="entry.kind==='federation'" type="button" class="workspace-link" :disabled="!!federation.changing" @click="federation.change(entry.record)">{{entry.state==='active'?'撤销':'验证并启用'}}</button><button v-else type="button" class="workspace-link" :disabled="rooms.busy||!entry.record.enabled" @click="revoke(entry.record.issuer)">撤销授权</button></td></tr>
        <tr v-if="!entries.length&&!federation.loading"><td colspan="5" class="workspace-empty">还没有节点。部署后从“接入节点”完成授权。</td></tr>
      </tbody></table></div>
    </section>
    <WorkspaceDrawer v-if="onboarding" title="接入节点" @close="onboarding=false">
      <div class="node-onboarding"><p>部署一次即可运行节点程序。译文分发启动后自动登记，管理员在列表验证并启用；房间文件需要额外授权，避免节点获得不必要的上传权限。</p>
        <a href="/network" target="_blank" rel="noopener">查看节点部署与接入指南 ↗</a>
        <h3>授权房间文件能力</h3><RoomNodeInvitation/>
      </div>
    </WorkspaceDrawer>
  </template>
</main>
</template>

<style scoped>
.node-onboarding{display:grid;gap:14px}.node-onboarding p{margin:0}.node-onboarding .workspace-form{margin:0}.node-onboarding h3{margin:4px 0 0}.workspace-invite{display:grid;gap:10px}.workspace-invite code{overflow-wrap:anywhere;user-select:all}
</style>
