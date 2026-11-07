import {defineStore} from 'pinia';
import {ref} from 'vue';

export type ClientKey={
  id:string;prefix:string;name:string;contact:string;website:string;purpose:string;
  state:string;fields_per_hour:number;requests_per_minute:number;fields_used_this_hour:number;
  created:number;updated:number;
};

export const useClientKeysStore=defineStore('client-keys',()=>{
  const clients=ref<ClientKey[]>([]),cursor=ref<string|null>(null),loading=ref(false);
  const ready=ref(false),error=ref(''),unauthorized=ref(false),busy=ref(false);
  async function request(body?:object,after?:string){
    const response=await fetch('/v1/admin/clients'+(after?'?after='+encodeURIComponent(after):''),{
      method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(15000),
      headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{}),
    });
    const data=await response.json().catch(()=>({}));
    if(response.status===401)unauthorized.value=true;
    if(!response.ok)throw Error(data.code||data.error||(response.status===401?'登录已过期':'请求失败，请重试'));
    return data;
  }
  async function refresh(){
    if(loading.value)return;
    loading.value=true;error.value='';
    try{const data=await request();clients.value=data.clients||[];cursor.value=data.next_cursor||null;ready.value=true;}
    catch(e){error.value=(e as Error).message;}
    finally{loading.value=false;}
  }
  async function more(){
    if(loading.value||!cursor.value)return;
    loading.value=true;error.value='';
    try{const data=await request(undefined,cursor.value);clients.value.push(...(data.clients||[]));cursor.value=data.next_cursor||null;}
    catch(e){error.value=(e as Error).message;}
    finally{loading.value=false;}
  }
  async function create(details:{name:string;contact:string;website:string;purpose:string;fields_per_hour:number;requests_per_minute:number}){
    if(busy.value)return '';
    busy.value=true;
    try{const data=await request({action:'create',...details});await refresh();return String(data.access_token||'');}
    finally{busy.value=false;}
  }
  async function update(client:ClientKey,state:string,fields_per_hour=client.fields_per_hour,requests_per_minute=client.requests_per_minute){
    if(busy.value)return;
    busy.value=true;
    try{await request({id:client.id,state,fields_per_hour,requests_per_minute});await refresh();}
    finally{busy.value=false;}
  }
  return {clients,cursor,loading,ready,error,unauthorized,busy,refresh,more,create,update};
});
