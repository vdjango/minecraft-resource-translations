import {computed,ref} from 'vue';
import {defineStore} from 'pinia';
import {useNotificationsStore} from './notifications';

export const useNodeAccessStore=defineStore('node-access',()=>{
  const data=ref<any>({nodes:[]}),loading=ref(false),ready=ref(false),error=ref(''),changing=ref(''),unauthorized=ref(false);
  const nodes=computed<any[]>(()=>data.value.nodes||[]);
  const pending=computed(()=>nodes.value.filter(node=>node.state==='pending').length);
  const online=computed(()=>nodes.value.filter(node=>node.online).length);
  async function request(body?:any){
    const response=await fetch('/v1/admin/nodes',{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(15000),headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
    const value=await response.json();
    if(response.status===401)unauthorized.value=true;
    if(!response.ok)throw Error(value.code==='node_verification_failed'?'验证未通过，请检查节点域名、HTTPS 和主服务连接。':value.message||value.error||(response.status===401?'登录已过期，请重新登录。':'节点配置读取失败，请重试。'));
    return value;
  }
  async function refresh(){
    if(loading.value)return;
    loading.value=true;error.value='';
    try{data.value=await request();ready.value=true;}catch(e){error.value=(e as Error).message;}finally{loading.value=false;}
  }
  async function change(node:any){
    if(changing.value)return;
    changing.value=node.id;
    try{await request({id:node.id,state:node.state==='active'?'revoked':'active'});await refresh();useNotificationsStore().notify(node.state==='active'?'节点已退出分发。':'节点已启用，收到心跳后参与分发。');}
    catch(e){useNotificationsStore().notify((e as Error).message,true);}
    finally{changing.value='';}
  }
  return {data,nodes,pending,online,loading,ready,error,changing,unauthorized,refresh,change};
});
