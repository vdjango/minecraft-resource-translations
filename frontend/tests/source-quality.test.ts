import {beforeEach,describe,expect,it} from 'vitest';
import {createPinia,setActivePinia} from 'pinia';
import {qualityDefaults,useSourceQualityStore} from '../src/stores/sourceQuality';

describe('shared source quality settings',()=>{
  beforeEach(()=>setActivePinia(createPinia()));
  it('refreshes observations without replacing unsaved settings',()=>{
    const store=useSourceQualityStore();
    store.hydrate({settings:{...qualityDefaults},source_quality:{daily_bytes:123},running:true});
    store.settings.quality_slow_kibps=256;
    store.hydrate({settings:qualityDefaults,source_quality:{daily_bytes:456},running:true});
    expect(store.observation.daily_bytes).toBe(456);
    expect(store.payload().quality_slow_kibps).toBe(256);
    store.hydrate({settings:qualityDefaults},true);
    expect(store.settings.quality_slow_kibps).toBe(128);
  });
  it('combines CDN health and repository history without conflating their identities',()=>{
    const store=useSourceQualityStore();
    store.hydrate({settings:{source_registries:[{id:'maven',name:'Maven',enabled:true}]},
      source_quality:{content_sources:[{id:'cdn:edge.forgecdn.net',health:'slow'}],
        sources:{maven:{score:90,grade:'excellent'}}}});
    expect(store.sources.map(source=>source.id)).toEqual(['cdn:edge.forgecdn.net','maven']);
    expect(store.sources[0].health).toBe('slow');
    expect(store.sources[1].score).toBe(90);
  });
});
