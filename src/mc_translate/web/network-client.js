"use strict";
// Only anonymous public reads go to mirrors. Credentials and writes stay at hub.
class TranslationNetwork {
  constructor() { this.hub=location.origin;this.selected=location.origin;this.selectedId="hub";this.candidates=[];this.failedUntil=new Map();this.refreshing=false;this.ready=this.initialize(); }
  static read(path,options) { const method=(options.method||"GET").toUpperCase();return method==="POST"&&path==="translations:resolve"||method==="GET"&&(path.startsWith("artifacts/")||path.startsWith("catalog/")||/^projects\/[^/]+\/[^/]+\/(manifest|source|history)(\?|$)/.test(path)||path==="capabilities"); }
  async initialize() {
    try {const response=await fetch("/v1/federation/health",{signal:AbortSignal.timeout(1800)});if(response.ok){const data=await response.json();if(data.role==="mirror"){this.hub=data.hub_url;this.selected=this.hub;}}}catch(error){/* Existing independent deployments remain usable. */}
    void this.select();
  }
  async select() {
    if(this.refreshing)return;this.refreshing=true;
    try {
      const response=await fetch(this.hub+"/v1/federation/nodes",{credentials:"omit",signal:AbortSignal.timeout(2000)});
      if(!response.ok)return;const directory=await response.json();
      if(directory.hub_url!==this.hub||directory.protocol!=="translation-mirror-v1")return;
      const nodes=directory.nodes.filter(node=>{try{const url=new URL(node.base_url);return url.origin===node.base_url&&(!url.username&&!url.password)&&(url.protocol==="https:"||location.hostname==="127.0.0.1"&&url.hostname==="127.0.0.1");}catch(error){return false;}}).slice(0,7);
      const attempts=await Promise.allSettled(nodes.map(async node=>{const start=performance.now();const ping=await fetch(node.base_url+"/v1/federation/health",{mode:"cors",credentials:"omit",cache:"no-store",signal:AbortSignal.timeout(1500)});if(!ping.ok)throw Error("offline");const data=await ping.json();if(!data.ready||data.hub_url!==this.hub||data.node_id!==node.id)throw Error("wrong node");return {...node,rtt:performance.now()-start};}));
      this.candidates=attempts.filter(result=>result.status==="fulfilled").map(result=>result.value).sort((a,b)=>a.rtt-b.rtt);
      const available=this.candidates.filter(node=>(this.failedUntil.get(node.base_url)||0)<Date.now());
      if(available.length){const fastest=available[0];const nearbyMirror=available.find(node=>node.role==="mirror"&&node.rtt<=fastest.rtt+5);const chosen=nearbyMirror||fastest;this.selected=chosen.base_url;this.selectedId=chosen.id;}
      window.dispatchEvent(new CustomEvent("translation-node-changed",{detail:{id:this.selectedId,url:this.selected,candidates:this.candidates}}));
    }catch(error){/* Discovery is optional; current hub remains the fallback. */}
    finally{this.refreshing=false;}
  }
  async fetch(path,options={}) {
    const timed=ms=>({...options,signal:options.signal?AbortSignal.any([options.signal,AbortSignal.timeout(ms)]):AbortSignal.timeout(ms)});
    // Start local cached reads immediately; federation discovery runs in parallel.
    if(!TranslationNetwork.read(path,options)){await this.ready;return fetch(this.hub+"/v1/"+path,timed(15000));}
    const preferred=this.selected;
    if(preferred!==this.hub&&(this.failedUntil.get(preferred)||0)<Date.now()){
      try{const headers=new Headers(options.headers||{});headers.delete("Authorization");headers.delete("Cookie");headers.delete("Idempotency-Key");const response=await fetch(preferred+"/v1/"+path,{...timed(2500),headers,credentials:"omit"});if(response.ok||response.status===304)return response;throw Error("node unavailable");}
      catch(error){if(options.signal?.aborted)throw error;this.failedUntil.set(preferred,Date.now()+30000);this.selected=this.hub;this.selectedId="hub";}
    }
    return fetch(this.hub+"/v1/"+path,timed(12000));
  }
}
window.translationNetwork=new TranslationNetwork();
setInterval(()=>{if(!document.hidden)void window.translationNetwork.select();},300000);
