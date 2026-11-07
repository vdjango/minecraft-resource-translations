import {defineStore} from 'pinia';
import {computed,ref} from 'vue';

export const prefetchPlatforms=[
  {key:'windows',name:'Windows'},
  {key:'macos',name:'macOS'},
  {key:'linux',name:'Linux'},
] as const;
export type PrefetchPlatform=typeof prefetchPlatforms[number]['key'];

function normalize(value:unknown):PrefetchPlatform[]{
  const values=value==='all'?prefetchPlatforms.map(p=>p.key):Array.isArray(value)?value:[value??'windows'];
  return prefetchPlatforms.filter(p=>values.includes(p.key)).map(p=>p.key);
}

export function describePrefetchPlatforms(value:unknown):string{
  const selected=normalize(value);
  return selected.length?prefetchPlatforms.filter(p=>selected.includes(p.key)).map(p=>p.name).join('、'):'未选择平台（不预下载）';
}

export const useMirrorPrefetchStore=defineStore('mirror-prefetch',()=>{
  const platforms=ref<PrefetchPlatform[]>(['windows']),ready=ref(false);
  const label=computed(()=>describePrefetchPlatforms(platforms.value));
  function hydrate(value:unknown,replace=false){
    if(ready.value&&!replace)return;
    platforms.value=normalize(value);ready.value=true;
  }
  function payload(){return normalize(platforms.value);}
  return {platforms,ready,label,hydrate,payload,describe:describePrefetchPlatforms};
});
