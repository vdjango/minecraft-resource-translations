import {computed, inject} from 'vue';
import {routerKey} from 'vue-router';
import {platformRoute} from './platformRoutes';

/** Keep all platform routes in the same Vue application. */
export function usePlatformNavigation(initialPath='/'){
  const router=inject(routerKey,null);
  const path=computed(()=>{
    const current=router?.currentRoute.value.path;
    const browserPath=typeof location==='undefined'?initialPath:location.pathname;
    return current&&!(current==='/'&&browserPath!=='/')?current:browserPath;
  });
  function navigate(event:MouseEvent,href:string){
    const current=platformRoute(path.value),destination=platformRoute(href.split(/[?#]/,1)[0]);
    if(!router||!current||!destination)return;
    if(event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
    event.preventDefault();
    void router.push(href);
  }
  return {path,navigate};
}
