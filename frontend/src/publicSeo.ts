import config from '../../src/mc_translate/public-seo.json';

type Seo = {title:string;description:string;keywords?:string;url?:string};
let state:Record<string,string>={};
export function configurePublicSeo(seed:Record<string,string>){state=seed;}
export function isMirrorHome(){return state.publicMode==='mirror';}
export function publicLinks(mirrorHome=isMirrorHome()){
  const origin=typeof location==='undefined'?'':location.origin;
  const mirror=state.mirrorBase||'https://mirror.bugg.icu';
  const translation=state.translationBase||'https://translate.bugg.icu';
  return {mirror:mirrorHome?'/':mirror+'/',translation:!mirrorHome&&origin===translation?'/':translation+'/'};
}
export const productLinks=config.links;
export function updatePageMetadata(path:string,query:string,fallbackTitle:string){
  const key=path==='/'?(isMirrorHome()?'mirror':'index'):path.slice(1);
  let seo:Seo|undefined=(config.pages as Record<string,Seo>)[key];
  if(path.startsWith('/projects/')&&state.seo){try{const project=JSON.parse(state.seo);if(new URL(project.url).pathname===path)seo=project;}catch{}}
  const privatePage=!seo||!!query;
  document.title=seo?.title||fallbackTitle;
  function meta(name:string,content:string,property=false){
    const attribute=property?'property':'name';
    let tag=document.head.querySelector<HTMLMetaElement>(`meta[${attribute}="${name}"]`);
    if(!tag){tag=document.createElement('meta');tag.setAttribute(attribute,name);document.head.append(tag);}
    tag.content=content;
  }
  meta('robots',privatePage?'noindex,follow':'index,follow');
  meta('description',seo?.description||'');meta('keywords',seo?.keywords||'');
  if(!seo)return;
  const base=key==='index'||path.startsWith('/projects/')?state.translationBase:state.mirrorBase;
  const canonical=seo.url||((base||location.origin)+(key==='mirror'?'/':path));
  let link=document.head.querySelector<HTMLLinkElement>('link[rel="canonical"]');
  if(!link){link=document.createElement('link');link.rel='canonical';document.head.append(link);}link.href=canonical;
  meta('og:title',seo.title,true);meta('og:description',seo.description,true);meta('og:url',canonical,true);
  meta('twitter:title',seo.title);meta('twitter:description',seo.description);
}
