import { reactive } from 'vue';
import {type Project} from './Catalog.vue';
import {type AccessOptions} from './AdminAccess.vue';
// The session survives route component changes; every page keeps its own callbacks.
export const adminSession=reactive({authenticated:false});
export function createViews(catalogueSeed?:string) {
  let openProject:(resource:Project['resource'])=>void=()=>{};
  const access=reactive({options:null as AccessOptions|null,visible:!adminSession.authenticated,authenticated:adminSession.authenticated});
  function visible(value:boolean){access.visible=value;}
  function authenticated(value:boolean){adminSession.authenticated=value;access.authenticated=value;document.body.classList.toggle('admin-authenticated',value);visible(!value);}
  let seed:{projects:Project[];total_count:number;next_cursor:string|null}|undefined;
  try{if(catalogueSeed)seed=JSON.parse(catalogueSeed);}catch{}
  const state=reactive({projects:seed?.projects||[] as Project[],language:'简体中文',loading:false});
  return {
    adminAccess:{mount(options:AccessOptions){if(options.mode==='setup')authenticated(false);else if(!adminSession.authenticated)visible(options.mode!=='session');access.options={...options,unauthorized(){authenticated(false);}};},visible,authenticated},
    catalog:{
      state,
      open(resource:Project['resource']){openProject(resource);},
      seeded(){return seed;},
      detail(resource:Project['resource'],fields:{field:string;state:string;is_current:boolean;translation?:{text?:string;translation_required?:boolean};update?:{notice?:string}}[],language:string){const p=state.projects.find(p=>p.resource.provider===resource.provider&&p.resource.project_id===resource.project_id);if(!p)return;const ready=fields.length===2&&fields.every(f=>f.state==='ready'&&f.is_current);p.translation_state=ready?'ready':'partial';const summary=fields.find(f=>f.field==='summary');if(summary?.state==='ready'&&summary.translation?.text!==undefined)p.localized_summary=summary.translation.text;p.badge=ready?(fields.every(f=>f.translation?.translation_required===false)?'无需翻译，保留原文':language+' · 简介与正文已就绪'):fields.find(f=>f.state!=='ready')?.update?.notice||'译文正在准备';},
      mount(open:(resource:Project['resource'])=>void){openProject=open;},
      loading(value:boolean){state.loading=value;},
      set(projects:Project[],language:string){state.projects=projects;state.language=language;},
      update(resource:Project['resource'],text:string|undefined,badge:string){const p=state.projects.find(p=>p.resource.provider===resource.provider&&p.resource.project_id===resource.project_id);if(p){if(text!==undefined)p.localized_summary=text;p.badge=badge;}},
    },
    access,
    dispose(){openProject=()=>{};},
  };
}
