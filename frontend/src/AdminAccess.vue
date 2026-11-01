<script setup lang="ts">
import {computed,onBeforeUnmount,onMounted,ref} from 'vue';
export type AccessOptions={session:string;login:string;setup:string;mode:string;authorized:(data:Record<string,unknown>)=>void;unauthorized?:()=>void};
const props=defineProps<{options:AccessOptions;visible:boolean}>();
const setupPage=location.pathname==='/admin/setup';
const mode=ref(props.options.mode==='setup'||setupPage?'setup':'login');
const username=ref(''),password=ref(''),confirmation=ref(''),token=ref('');
const legacy=ref(props.options.mode==='legacy'),busy=ref(false),notice=ref(''),error=ref('');
const setup=computed(()=>mode.value==='setup');
// Fragments are never sent to the web server or included in access logs.
let setupToken=setupPage?location.hash.slice(1):'';
let accessRevision=0;
if(setupToken){history.replaceState(null,'',location.pathname+location.search);}
async function request(url:string,init:RequestInit={}){
  let response:Response;
  try{response=await fetch(url,{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(6000),...init});}
  catch{throw new Error('无法连接登录服务，请检查网络后重试。');}
  let data:Record<string,any>={};
  try{data=await response.json();}catch{if(response.ok)throw new Error('登录服务返回异常，请稍后重试。');}
  if(!response.ok){const error=new Error(data.error||({unauthorized:'用户名或密码错误。',invalid_setup:'初始化链接无效或已使用。',invalid_credentials:'请检查用户名、密码和确认密码。'} as Record<string,string>)[data.code]||'请求未完成，请稍后重试。');Object.assign(error,{status:response.status});throw error;}
  return data;
}
async function restore(){
  if(setup.value||busy.value)return;
  const revision=++accessRevision;
  notice.value='正在检查管理会话…';
  try{
    const data=await request(props.options.session);
    if(revision!==accessRevision)return;
    if(data.authenticated){notice.value='';props.options.authorized(data);return;}
    if(data.initialized===false&&data.setup_available)mode.value='setup';
    notice.value='';props.options.unauthorized?.();
  }catch(error){if(revision!==accessRevision)return;props.options.unauthorized?.();notice.value=(error as Error&{status?:number}).status===401?'':'连接暂时未完成。可以直接登录，或重新检查会话。';}
}
async function submit(){
  if(busy.value)return;
  if(setup.value&&password.value!==confirmation.value){error.value='两次输入的密码不一致。';return;}
  const revision=++accessRevision;
  busy.value=true;error.value='';notice.value='';
  try{
    const headers:Record<string,string>={'Content-Type':'application/json'};
    if(setup.value)headers['X-Admin-Setup']=setupToken;
    if(legacy.value&&!setup.value)headers.Authorization='Bearer '+token.value;
    const data=await request(setup.value?props.options.setup:props.options.login,{method:'POST',headers,body:JSON.stringify({username:username.value,password:password.value,...(setup.value?{password_confirm:confirmation.value}:{})})});
    if(revision!==accessRevision)return;
    password.value='';confirmation.value='';token.value='';setupToken='';
    props.options.authorized(data);
  }catch(e){if(revision===accessRevision)error.value=e instanceof Error?e.message:'无法连接管理服务，请重试。';}
  finally{busy.value=false;}
}
// The server authenticated this HTML response; protected data requests check
// the session again. Avoid an extra round trip before loading page data.
onMounted(()=>{if(props.options.mode==='session')props.options.authorized({});else void restore();});
onBeforeUnmount(()=>{accessRevision++;});
</script>

<template>
  <section v-if="visible" class="admin-access" aria-labelledby="access-title">
    <div class="access-symbol" aria-hidden="true">B<span>·</span></div>
    <p class="access-eyebrow">{{setup?'FIRST TIME SETUP':'ADMINISTRATOR'}}</p>
    <h1 id="access-title">{{setup?'设置管理员账户':'登录'}}</h1>
    <p class="access-intro">{{setup?'设置用户名和密码，开始使用管理后台。':'登录 Buggcraft 管理后台'}}</p>
    <div v-if="setup&&!setupToken" class="access-note">请使用安装程序提供的完整初始化链接。忘记密码时，在服务器生成新的账户设置链接。</div>
    <form @submit.prevent="submit" method="post" autocomplete="on">
      <template v-if="!legacy||setup">
        <label>管理员用户名<input v-model.trim="username" name="username" autocomplete="username" pattern="[A-Za-z0-9_-]{1,32}" maxlength="32" required placeholder="输入用户名"></label>
        <label>管理员密码<input v-model="password" name="password" type="password" :autocomplete="setup?'new-password':'current-password'" :minlength="setup?12:undefined" maxlength="256" required :placeholder="setup?'至少 12 个字符':'输入管理员密码'"></label>
        <label v-if="setup">确认密码<input v-model="confirmation" name="password_confirm" type="password" autocomplete="new-password" minlength="12" maxlength="256" required placeholder="再次输入密码"></label>
      </template>
      <label v-else>现有管理员令牌<input v-model="token" type="password" autocomplete="off" required placeholder="输入服务端管理令牌"></label>
      <p v-if="error" class="access-error" role="alert">{{error}}</p>
      <button class="primary access-submit" type="submit" :disabled="busy||(setup&&!setupToken)">{{busy?(setup?'正在保存…':'正在登录…'):setup?'保存并继续':'登录'}}</button>
    </form>
    <p v-if="notice" class="access-note" role="status">{{notice}} <button type="button" class="access-link" @click="restore">重试</button></p>
    <p class="access-footnote">{{setup?'密码仅保存为加密摘要，请自行妥善保管。':'忘记密码？使用服务器的账户恢复命令，重新设置后再登录。'}}</p>
  </section>
</template>
