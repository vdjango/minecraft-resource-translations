import {defineStore} from 'pinia';
import {computed,reactive,ref} from 'vue';
import {useNotificationsStore} from './notifications';

export const qualityDefaults={quality_enabled:true,quality_adaptive_cache:true,
  quality_interval_hours:6,quality_daily_mib:64,quality_sample_kib:256,
  quality_fast_kibps:2048,quality_slow_kibps:128,quality_content_minutes:5,
  quality_backup_minutes:10,quality_modrinth_minutes:15};
export const useSourceQualityStore=defineStore('source-quality',()=>{
  const settings=reactive({...qualityDefaults}),ready=ref(false),running=ref(false);
  const observation=ref<any>({}),registries=ref<any[]>([]),busy=ref(false);
  const history=ref<any[]|null>(null),historyName=ref(''),historyLoading=ref(false);
  const mappings=ref<any[]>([]),mappingTotal=ref(0),mappingCursor=ref(''),mappingBusy=ref(false),mappingError=ref('');
  const filters=reactive({query:'',provider:'',state:''});
  let mappingRequest=0;
  let historyRequest=0;
  const sources=computed(()=>[...(observation.value.content_sources||[]),
    ...registries.value.map(source=>({...source,...observation.value.sources?.[source.id]}))]);
  function hydrate(data:any,replace=false){
    running.value=!!data.running;observation.value=data.source_quality||{};
    registries.value=data.settings?.source_registries||[];
    if(!ready.value||replace){
      for(const key of Object.keys(qualityDefaults) as (keyof typeof qualityDefaults)[])
        (settings as any)[key]=data.settings?.[key]??qualityDefaults[key];
      ready.value=true;
    }
  }
  function payload(){return {...settings};}
  async function request(body:Record<string,unknown>){
    const response=await fetch('/v1/admin/mirror',{method:'POST',credentials:'same-origin',
      signal:AbortSignal.timeout(12000),headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json();
    if(!response.ok)throw Error(data.error||data.message||'无法读取来源质量，请稍后重试。');
    return data;
  }
  async function probe(source?:string){
    busy.value=true;
    try{hydrate(await request({action:'quality_probe',...(source?{source}:{})}));
      useNotificationsStore().notify('已安排错峰采样；达到每日预算后暂停。');
    }catch(error){useNotificationsStore().notify(error instanceof Error?error.message:String(error),true);}
    finally{busy.value=false;}
  }
  async function showHistory(source:any){
    const sequence=++historyRequest;
    historyName.value=source.host||source.name;history.value=[];historyLoading.value=true;
    try{const data=await request({action:'quality_history',source:source.id});
      if(sequence===historyRequest)history.value=data.samples||[];
    }catch(error){useNotificationsStore().notify(error instanceof Error?error.message:String(error),true);}
    finally{if(sequence===historyRequest)historyLoading.value=false;}
  }
  function closeHistory(){historyRequest++;history.value=null;}
  async function loadMappings(next=false){
    const sequence=++mappingRequest;mappingBusy.value=true;mappingError.value='';
    try{const data=await request({action:'content_files',...filters,cursor:next?mappingCursor.value:''});
      if(sequence!==mappingRequest)return;
      mappings.value=next?[...mappings.value,...data.files]:data.files;
      mappingTotal.value=data.total;mappingCursor.value=data.cursor;
    }catch(error){if(sequence===mappingRequest)mappingError.value=error instanceof Error?error.message:String(error);}
    finally{if(sequence===mappingRequest)mappingBusy.value=false;}
  }
  return {settings,ready,running,observation,sources,busy,history,historyName,historyLoading,
    mappings,mappingTotal,mappingCursor,mappingBusy,mappingError,filters,loadMappings,
    hydrate,payload,probe,showHistory,closeHistory};
});
