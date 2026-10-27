import { createApp, h, reactive, type App } from 'vue';
import Catalog, {type Project} from './Catalog.vue';
export function createViews() {
  let app:App|undefined;
  let seed:{projects:Project[];total_count:number;next_cursor:string|null}|undefined;
  try{const raw=document.getElementById('catalog-seed')?.getAttribute('data-catalog');if(raw)seed=JSON.parse(raw);}catch{}
  const state=reactive({projects:seed?.projects||[] as Project[],language:'简体中文',loading:false});
  return {
    catalog:{
      seeded(){return seed;},
      detail(resource:Project['resource'],fields:{field:string;state:string;is_current:boolean;translation?:{text?:string;translation_required?:boolean};update?:{notice?:string}}[],language:string){const p=state.projects.find(p=>p.resource.provider===resource.provider&&p.resource.project_id===resource.project_id);if(!p)return;const ready=fields.length===2&&fields.every(f=>f.state==='ready'&&f.is_current);p.translation_state=ready?'ready':'partial';const summary=fields.find(f=>f.field==='summary');if(summary?.state==='ready'&&summary.translation?.text!==undefined)p.localized_summary=summary.translation.text;p.badge=ready?(fields.every(f=>f.translation?.translation_required===false)?'无需翻译，保留原文':language+' · 简介与正文已就绪'):fields.find(f=>f.state!=='ready')?.update?.notice||'译文正在准备';},
      mount(open:(resource:Project['resource'])=>void){const host=document.getElementById('projects');if(host){app=createApp({render:()=>h(Catalog,{...state,open})});app.mount(host);}},
      loading(value:boolean){state.loading=value;},
      set(projects:Project[],language:string){state.projects=projects;state.language=language;},
      update(resource:Project['resource'],text:string|undefined,badge:string){const p=state.projects.find(p=>p.resource.provider===resource.provider&&p.resource.project_id===resource.project_id);if(p){if(text!==undefined)p.localized_summary=text;p.badge=badge;}},
    },
    dispose(){app?.unmount();},
  };
}
