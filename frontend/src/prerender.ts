import {createSSRApp,h,type Component} from 'vue';
import {renderToString} from 'vue/server-renderer';
import SiteNav from './SiteNav.vue';
import ProductLinks from './ProductLinks.vue';
import Index from './pages/index.vue';
import Mirror from './pages/mirror.vue';
import Docs from './pages/docs.vue';
import Developers from './pages/developers.vue';

/** The same Vue components render for people and crawlers; no HTML page copies. */
export async function renderPages(){
  const result:Record<string,string>={};
  for(const [entry,Page] of Object.entries({index:Index,mirror:Mirror,docs:Docs,developers:Developers}) as [string,Component][]){
    for(const mode of ['mirror','translation']){
      const seed={publicMode:mode,publicOrigin:'https://mirror.bugg.icu'};
      const views={catalog:{state:{projects:[],language:'zh-Hans',loading:true},open:()=>{}}};
      const path=entry==='index'||entry==='mirror'&&mode==='mirror'?'/':'/'+entry;
      result[entry+':'+mode]=await renderToString(createSSRApp({render:()=>h('div',{style:{display:'contents'}},[
        h(SiteNav,{initialPath:path,mirrorHome:mode==='mirror'}),h(Page,{seed,views}),h(ProductLinks),
      ])}));
    }
  }
  return result;
}
