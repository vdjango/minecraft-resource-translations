import { createApp, defineComponent, onMounted, onBeforeUnmount, h, reactive, ref, type Component } from 'vue';
import {createPinia} from 'pinia';
import {createRouter,createWebHistory,RouterView,type RouteRecordRaw} from 'vue-router';
import { createViews } from './views';
import { capturePageSeed } from './pageSeed';
import {platformRoute,platformRoutes} from './platformRoutes';
import {adminSession} from './views';
import SiteNav from './SiteNav.vue';
import ProductLinks from './ProductLinks.vue';
import {configurePublicSeo,isMirrorHome,updatePageMetadata} from './publicSeo';
import AdminNav from './AdminNav.vue';
import ToastNotifications from './ToastNotifications.vue';
declare const __PLATFORM_BUILD_REVISION__:string;
const buildRevision=typeof __PLATFORM_BUILD_REVISION__==='string'?__PLATFORM_BUILD_REVISION__:'dev';

export type PageScope = ReturnType<typeof createScope>;
export function createScope() {
  let active = true;
  const timers = new Set<number>();
  const controllers = new Set<AbortController>();
  const readyCallbacks = new Set<() => void>();
  const listeners = new AbortController();
  const exposed = new Map<string, unknown>();
  const cleanup = new Set<() => void>();
  const state = reactive({ pending: 0, error: '' });
  const pending = () => window.dispatchEvent(new CustomEvent('buggcraft:page-pending',{detail:state.pending}));
  const scope = {
    state,
    get active(){return active;},
    setTimeout(handler: TimerHandler, delay?: number, ...args: unknown[]): number {
      const id = window.setTimeout(() => { timers.delete(id); if(active && typeof handler === 'function') handler(...args); }, delay);
      timers.add(id); return id;
    },
    clearTimeout(id?: number) { window.clearTimeout(id); if(id !== undefined)timers.delete(id); },
    setInterval(handler: TimerHandler, delay?: number, ...args: unknown[]): number {
      const id = window.setInterval(() => { if(active && typeof handler === 'function') handler(...args); }, delay);
      timers.add(id); return id;
    },
    clearInterval(id?: number) { window.clearInterval(id); if(id !== undefined)timers.delete(id); },
    async fetch(input: RequestInfo | URL, init: RequestInit = {}) {
      if(!active) throw new DOMException('Page closed', 'AbortError');
      const controller = new AbortController(); controllers.add(controller); state.pending++; pending();
      const supplied=init.signal||(input instanceof Request?input.signal:undefined);
      const signal=controller.signal;
      const forwardAbort=()=>controller.abort(supplied?.reason);
      const deadline=supplied?undefined:window.setTimeout(()=>controller.abort(new DOMException('Request timed out','TimeoutError')),25000);
      let finished=false;
      const finish=()=>{if(!finished){finished=true;window.clearTimeout(deadline);supplied?.removeEventListener('abort',forwardAbort);controllers.delete(controller);state.pending--;pending();}};
      signal.addEventListener('abort',finish,{once:true});
      if(supplied?.aborted)forwardAbort();else supplied?.addEventListener('abort',forwardAbort,{once:true});
      try {
        const response=await window.fetch(input, {...init, signal});
        return new Proxy(response,{get(target,key){
          const value=Reflect.get(target,key,target);
          if(typeof value!=='function')return value;
          if(['json','text','blob','arrayBuffer','formData','bytes'].includes(String(key)))return async(...args:unknown[])=>{try{return await value.apply(target,args);}finally{finish();}};
          return value.bind(target);
        }});
      }catch(error){finish();throw error;}
    },
    ready(callback: () => void) {
      const run = () => { readyCallbacks.delete(run); if(active)callback(); };
      if(document.readyState === 'loading') { readyCallbacks.add(run); document.addEventListener('DOMContentLoaded',run,{once:true}); }
      else queueMicrotask(run);
    },
    expose(name: string, value: unknown) {
      const target = window as unknown as Record<string,unknown>;
      exposed.set(name,target[name]); target[name]=value;
    },
    listen(target:EventTarget,event:string,callback:EventListenerOrEventListenerObject,options?:boolean|AddEventListenerOptions){
      target.addEventListener(event,callback,typeof options==='boolean'?{capture:options,signal:listeners.signal}:{...options,signal:listeners.signal});
    },
    onDispose(callback:()=>void){cleanup.add(callback);},
    dispose() {
      if(!active)return;
      active=false;
      listeners.abort();
      for(const id of timers) { window.clearTimeout(id); window.clearInterval(id); } timers.clear();
      for(const controller of controllers)controller.abort(); controllers.clear();
      for(const callback of readyCallbacks)document.removeEventListener('DOMContentLoaded',callback); readyCallbacks.clear();
      for(const callback of cleanup)callback();cleanup.clear();
      const target=window as unknown as Record<string,unknown>;
      for(const [name,previous] of exposed) {if(previous===undefined)delete target[name];else target[name]=previous;} exposed.clear();
    },
  };
  return scope;
}

type Initialize=(scope:PageScope,views:ReturnType<typeof createViews>,seed:Record<string,string>)=>void;
type PageModule={Page:Component;initialize:Initialize};

function routeView(module:PageModule,seed:Record<string,string>,restore?:()=>void){
  return defineComponent({
    setup(){
      const scope=createScope(),views=createViews(seed.catalogueData);
      onMounted(()=>{
        restore?.();
        try{module.initialize(scope,views,seed);}catch(error){
          scope.state.error=String(error);console.error(error);
          document.dispatchEvent(new CustomEvent('page-initialization-error',{detail:String(error)}));
        }
      });
      onBeforeUnmount(()=>{scope.dispose();views.dispose();});
      return ()=>h(module.Page,{seed,views});
    },
  });
}

/** Mount the Vue application into the generated browser bootstrap. */
export function mountPage(initialize:Initialize,Page:Component,name:string){
  if(document.querySelector('[data-vue-page]'))return;
  const seed=capturePageSeed();
  configurePublicSeo(seed.state);
  const host=document.createElement('div');
  host.className='vue-page-root';host.dataset.vuePage='';host.style.display='contents';
  document.body.replaceChildren(host);
  const initialView=routeView({Page,initialize},seed.state,seed.restore);
  const initial=platformRoute(location.pathname,isMirrorHome());
  const navigating=ref(false);
  const views=new Map<string,Component>([[name,initialView]]);
  async function resolve(entry:string):Promise<Component>{
    const cached=views.get(entry);if(cached)return cached;
    (window as Window & {__buggcraftRouteLoading?:boolean}).__buggcraftRouteLoading=true;
    try{
      const module=await import(/* @vite-ignore */ '/assets/vue-page-'+entry+'.js?v='+buildRevision) as PageModule;
      const next=routeView(module,{authMode:seed.state.authMode==='legacy'?'legacy':'login',catalogue:'',serviceState:'正在连接服务…'});
      views.set(entry,next);return next;
    }finally{(window as Window & {__buggcraftRouteLoading?:boolean}).__buggcraftRouteLoading=false;}
  }
  const routes:RouteRecordRaw[]=platformRoutes.map(item=>({
    path:item.path,component:(item.path==='/'&&isMirrorHome()?'mirror':item.entry)===name?initialView:()=>resolve(item.path==='/'&&isMirrorHome()?'mirror':item.entry),meta:{title:item.title},
  }));
  const router=initial&&initial.entry===name?createRouter({
    history:createWebHistory(),routes,scrollBehavior(to,_from,saved){
      if(saved)return saved;if(to.hash)return {el:to.hash,behavior:'smooth'};return {top:0};
    },
  }):null;
  router?.beforeEach((to,from)=>{navigating.value=to.fullPath!==from.fullPath;});
  router?.afterEach(to=>{updatePageMetadata(to.path,to.fullPath.includes('?')?to.fullPath.split('?')[1].split('#')[0]:'',String(to.meta.title||document.title));navigating.value=false;});
  router?.onError(error=>{console.error('Route load failed',error);location.assign(location.href);});
  function routeClick(event:MouseEvent){
    if(!router||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
    const anchor=event.target instanceof Element?event.target.closest<HTMLAnchorElement>('a[href]'):null;
    if(!anchor||anchor.hasAttribute('download')||(anchor.target&&anchor.target!=='_self'))return;
    const destination=new URL(anchor.href,location.href);
    const from=platformRoute(router.currentRoute.value.path),to=platformRoute(destination.pathname);
    if(destination.origin!==location.origin||!from||!to)return;
    // Each project response carries its own server-validated detail seed.
    if(to.entry==='project'&&from.entry!=='project')return;
    event.preventDefault();event.stopPropagation();
    void router.push(destination.pathname+destination.search+destination.hash);
  }
  const application=createApp(defineComponent({
    setup(){
      if(!router)return ()=>h('div',{style:{display:'contents'}},[h(initialView),h(ToastNotifications)]);
      return ()=>{
        const path=router.currentRoute.value.path;
        const admin=path==='/admin'||path.startsWith('/admin/');
        const authenticated=adminSession.authenticated&&path!=='/admin/setup';
        const content=h(RouterView);
        const loading=navigating.value?h('div',{class:'route-skeleton-overlay',role:'status','aria-live':'polite'},[
          h('p',{class:'site-load-heading'},'正在打开页面…'),
          h('div',{class:'site-skeleton-layout','aria-hidden':'true'},Array.from({length:4},()=>h('div',{class:'site-skeleton'}))),
        ]):null;
        return h('div',{class:'platform-router-root',style:{display:'contents'},onClickCapture:routeClick},[
          h(SiteNav,{authenticated}),
          admin?h('main',{class:'admin-shell'},[h(AdminNav,{authenticated}),content,loading]):content,
          admin?null:loading,
          admin?null:h(ProductLinks),
          h(ToastNotifications),
        ]);
      };
    },
  }));
  application.use(createPinia());
  if(router)application.use(router);
  application.mount(host);
  window.dispatchEvent(new Event('buggcraft:page-ready'));
  const close=(event:PageTransitionEvent)=>{if(!event.persisted){application.unmount();host.remove();window.removeEventListener('pagehide',close);}};
  window.addEventListener('pagehide',close);
}
