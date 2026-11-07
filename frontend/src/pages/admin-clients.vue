<script setup lang="ts">
import WorkspaceCardHeader from '../WorkspaceCardHeader.vue';
import {onMounted,reactive,ref,watch} from 'vue';
import AdminAccess from '../AdminAccess.vue';
import {useClientKeysStore,type ClientKey} from '../stores/clientKeys';
import {useNotificationsStore} from '../stores/notifications';
import '../mirrorWorkspace.css';
import '../mirrorWorkspaceCompact.css';

const props=defineProps<{seed:Record<string,string>;views:any}>();
const keys=useClientKeysStore(),notifications=useNotificationsStore();
const opened=ref(false),editing=ref<ClientKey|null>(null),issued=ref('');
const form=reactive({name:'',contact:'',website:'',purpose:'',fields_per_hour:1000,requests_per_minute:600});
onMounted(()=>{
  props.views.adminAccess.mount({session:'/v1/admin/session',login:'/v1/admin/session',setup:'/v1/admin/setup',mode:props.seed.authMode||'login',authorized(){keys.unauthorized=false;props.views.adminAccess.authenticated(true);void keys.refresh();}});
  if(props.seed.authMode==='session'){props.views.adminAccess.authenticated(true);void keys.refresh();}
});
watch(()=>keys.unauthorized,value=>{if(value)props.views.adminAccess.authenticated(false);});
function create(){editing.value=null;Object.assign(form,{name:'',contact:'',website:'',purpose:'',fields_per_hour:1000,requests_per_minute:600});opened.value=true;}
function edit(client:ClientKey){editing.value=client;Object.assign(form,{name:client.name,contact:client.contact,website:client.website,purpose:client.purpose,fields_per_hour:client.fields_per_hour,requests_per_minute:client.requests_per_minute});opened.value=true;}
async function save(){
  try{
    if(editing.value){await keys.update(editing.value,editing.value.state==='active'?'active':'revoked',Number(form.fields_per_hour),Number(form.requests_per_minute));notifications.notify('客户端额度已保存。');}
    else{issued.value=await keys.create({...form,fields_per_hour:Number(form.fields_per_hour),requests_per_minute:Number(form.requests_per_minute)});notifications.notify('客户端密钥已创建，只显示这一次。');}
    opened.value=false;
  }catch(e){notifications.notify((e as Error).message,true);}
}
async function toggle(client:ClientKey){
  try{await keys.update(client,client.state==='active'?'revoked':'active');notifications.notify(client.state==='active'?'客户端密钥已撤销。':'客户端密钥已启用。');}
  catch(e){notifications.notify((e as Error).message,true);}
}
async function copy(){try{await navigator.clipboard.writeText(issued.value);notifications.notify('密钥已复制。');}catch{notifications.notify('复制失败，请手动复制密钥。',true);}}
async function logout(){await fetch('/v1/admin/session',{method:'DELETE',credentials:'same-origin'});props.views.adminAccess.authenticated(false);props.views.access.options={...props.views.access.options,mode:'login'};}
function date(value:number){return value?new Date(value*1000).toLocaleDateString('zh-CN'):'—';}
</script>

<template>
<div class="admin-content mirror-workspace">
  <section v-if="views.access.visible" class="admin-access-host"><AdminAccess v-if="views.access.options" :options="views.access.options" :visible="true"/></section>
  <template v-if="views.access.authenticated">
    <header class="workspace-header"><div><h1>客户端密钥</h1><p>为需要独立译文生成额度的客户端发放密钥；公开下载不需要密钥。</p></div><button type="button" class="workspace-link" @click="logout">退出</button></header>
    <section class="workspace-card">
      <WorkspaceCardHeader title="已接入客户端" description="完整密钥仅在创建时显示一次；此处显示前缀和使用额度。"><button type="button" class="secondary" :disabled="keys.loading" @click="keys.refresh">刷新</button><button type="button" @click="create">新建客户端</button></WorkspaceCardHeader>
      <p v-if="keys.error" class="workspace-alert" role="alert">{{keys.error}}</p>
      <div v-if="issued" class="workspace-alert" role="status"><strong>新密钥已生成，请立即保存</strong><br><code>{{issued}}</code><div class="workspace-actions"><button type="button" class="secondary" @click="copy">复制密钥</button><button type="button" class="workspace-link" @click="issued=''">我已保存</button></div></div>
      <form v-if="opened" class="workspace-card client-key-form" @submit.prevent="save">
        <div class="workspace-toolbar"><h3>{{editing?'调整客户端额度':'新建客户端'}}</h3><button type="button" class="workspace-link" @click="opened=false">取消</button></div>
        <div v-if="!editing" class="client-key-fields"><label>客户端名称<input v-model.trim="form.name" maxlength="100" required placeholder="例如 Buggcraft"></label><label>联系信息（可选）<input v-model.trim="form.contact" maxlength="200" placeholder="邮箱或维护者"></label><label>主页（可选）<input v-model.trim="form.website" maxlength="500" placeholder="https://…"></label><label>用途（可选）<input v-model.trim="form.purpose" maxlength="1000" placeholder="译文生成"></label></div>
        <div class="client-key-fields"><label>每小时翻译字段数<input v-model.number="form.fields_per_hour" type="number" min="1" max="100000" required></label><label>每分钟请求数<input v-model.number="form.requests_per_minute" type="number" min="1" max="10000" required></label></div>
        <button type="submit" :disabled="keys.busy">{{keys.busy?'正在保存…':editing?'保存额度':'生成密钥'}}</button>
      </form>
      <div class="workspace-table" :aria-busy="keys.loading"><table><thead><tr><th>客户端</th><th>状态</th><th>每小时字段</th><th>每分钟请求</th><th>创建时间</th><th>操作</th></tr></thead><tbody>
        <template v-if="keys.loading&&!keys.ready"><tr v-for="n in 4" :key="n"><td colspan="6"><span class="workspace-skeleton"/></td></tr></template>
        <tr v-for="client in keys.clients" :key="client.id"><td><strong>{{client.name}}</strong><small>{{client.prefix}}… · {{client.id}}</small></td><td><span class="workspace-badge" :class="{warning:client.state!=='active'}">{{client.state==='active'?'已启用':client.state==='pending'?'待启用':'已停用'}}</span></td><td>{{client.fields_used_this_hour||0}} / {{client.fields_per_hour}}</td><td>{{client.requests_per_minute}}</td><td>{{date(client.created)}}</td><td><button type="button" class="workspace-link" @click="edit(client)">额度</button> <button type="button" class="workspace-link" :disabled="keys.busy" @click="toggle(client)">{{client.state==='active'?'停用':'启用'}}</button></td></tr>
        <tr v-if="keys.ready&&!keys.clients.length"><td colspan="6" class="workspace-empty">暂无客户端密钥。点击“新建客户端”开始。</td></tr>
      </tbody></table></div>
      <button v-if="keys.cursor" type="button" class="secondary" :disabled="keys.loading" @click="keys.more">加载更多</button>
    </section>
  </template>
</div>
</template>

<style scoped>
.client-key-form{margin:16px 0;background:var(--surface,#1f1b2d)}
.client-key-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:12px 0}
.client-key-fields label{display:flex;flex-direction:column;gap:5px;min-width:0}
.client-key-fields input{width:100%;min-height:36px;box-sizing:border-box}
.workspace-table td small{display:block;opacity:.7;margin-top:3px}
.workspace-alert code{overflow-wrap:anywhere;user-select:all}
@media(max-width:680px){.client-key-fields{grid-template-columns:1fr}}
</style>
