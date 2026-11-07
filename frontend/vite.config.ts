import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { readFileSync, writeFileSync, existsSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';

const web = resolve(import.meta.dirname, '../src/mc_translate/web');
const pages = JSON.parse(readFileSync(new URL('./pages.json', import.meta.url), 'utf8')) as Record<string, string[]>;
function handwrittenHtml(root:string):string[] {
  if(!existsSync(root))return [];
  return readdirSync(root,{withFileTypes:true}).flatMap(item=>{
    const path=resolve(root,item.name);
    return item.isDirectory()?handwrittenHtml(path):item.name.endsWith('.html')?[path]:[];
  });
}
export default defineConfig({
  base: '/assets/',
  define:{__PLATFORM_BUILD_REVISION__:JSON.stringify(Date.now().toString(16).padStart(12,'0'))},
  plugins: [vue(), {
    name: 'existing-page-controllers',
    buildStart() {
      const files=[...handwrittenHtml(resolve(import.meta.dirname,'src')),...handwrittenHtml(web)];
      if(existsSync(resolve(import.meta.dirname,'index.html')))files.push(resolve(import.meta.dirname,'index.html'));
      if(files.length)this.error('Use Vue components instead of .html files: '+files.join(', '));
    },
    writeBundle(_options,bundle) {
      // The server shell loads this stable CSS file for every admin route.
      // Vite extracts shared workspace styles into a second asset; fold them
      // into the stable file so client-side navigation and static deploys agree.
      const adminCss=resolve(web,'vue-page-admin-mirror.css');
      // Rollup can rename a shared CSS chunk as the component graph changes.
      // Use this build's assets rather than guessing a filename or reading an
      // old artifact left in outDir (emptyOutDir is deliberately disabled).
      const styles=Object.values(bundle).filter(asset=>asset.type==='asset'&&asset.fileName.endsWith('.css')&&asset.fileName!=='vue-runtime.css');
      const shared=styles.filter(asset=>asset.fileName!=='vue-page-admin-mirror.css');
      const page=styles.find(asset=>asset.fileName==='vue-page-admin-mirror.css');
      if(styles.length)writeFileSync(adminCss,[...shared,...(page?[page]:[])].map(asset=>asset.type==='asset'?String(asset.source):'').join('\n'));
      const lists=readFileSync(resolve(import.meta.dirname,'list-loading.js'),'utf8');
      const cssPath=resolve(web,'platform.css');
      const listCSS=lists.match(/const styles=`([\s\S]*?)`;/)?.[1]||'';
      writeFileSync(cssPath,readFileSync(cssPath,'utf8').split('/* BUGGCRAFT LOADING STYLES */')[0].trimEnd()+'\n/* BUGGCRAFT LOADING STYLES */\n'+listCSS.trim()+'\n');
      const themePath=resolve(web,'theme.js');
      if(existsSync(themePath)){
        const theme=readFileSync(themePath,'utf8').split('/* BUGGCRAFT PAGE SHELL */')[0].split('/* BUGGCRAFT LIST LOADING */')[0].trimEnd();
        writeFileSync(themePath,theme+'\n/* BUGGCRAFT LIST LOADING */\n'+lists);
      }

    },
    resolveId(id) { if (id.startsWith('page:')) return '\0' + id; },
    load(id) {
      if (!id.startsWith('\0page:')) return;
      const name = id.slice(6);
      let source = pages[name].map(file => {
        const override=resolve(import.meta.dirname,'src/controllers',file);
        const path=existsSync(override)?override:resolve(web,file);
        this.addWatchFile(path);
        return readFileSync(path,'utf8');
      }).join('\n;\n');
      source = source.replace(/(?:window|document)\.addEventListener\(['"]DOMContentLoaded['"],/g, 'scope.ready(');
      source = source.replace(/(window|document)\.addEventListener\(/g, 'scope.listen($1,');
      const exports = ['accountView', 'loadSessions', 'refresh'].map(key => `if(typeof ${key}==='function') scope.expose('${key}',${key});`).join('\n');
      return `import {mountPage} from '${resolve(import.meta.dirname,'src/runtime.ts').replaceAll('\\','/')}';
        import {useNotificationsStore} from '${resolve(import.meta.dirname,'src/stores/notifications.ts').replaceAll('\\','/')}';
        ${name==='index'?`import {prepareContentRenderer} from '${resolve(import.meta.dirname,'src/contentRenderer.ts').replaceAll('\\','/')}';`:''}
        ${name==='admin-mirror'?`import {useRoomClientCacheStore} from '${resolve(import.meta.dirname,'src/stores/roomClientCache.ts').replaceAll('\\','/')}'; import {useCloudStorageStore} from '${resolve(import.meta.dirname,'src/stores/cloudStorage.ts').replaceAll('\\','/')}'; import {useMirrorPrefetchStore} from '${resolve(import.meta.dirname,'src/stores/mirrorPrefetch.ts').replaceAll('\\','/')}'; import {useSourceQualityStore} from '${resolve(import.meta.dirname,'src/stores/sourceQuality.ts').replaceAll('\\','/')}';`:''}
        import Page from '@pages/${name}.vue';
        export function initialize(scope, pageViews, pageSeed) {
          const {fetch,setTimeout,clearTimeout,setInterval,clearInterval}=scope;
          const notifications=useNotificationsStore();
          ${name==='admin-mirror'?'const roomCacheStore=useRoomClientCacheStore();const cloudStore=useCloudStorageStore();const prefetchStore=useMirrorPrefetchStore();const qualityStore=useSourceQualityStore();':''}
          ${source}\n${exports}\n
        }
        export {Page};
        export function mount(){ mountPage(initialize, Page, '${name}'); }
        if(!window.__buggcraftRouteLoading)mount();`;
    },
  }],
  resolve:{alias:{'@pages':resolve(import.meta.dirname,'src/pages')}},
  build: {
    target: 'es2022', outDir: web, emptyOutDir: false, sourcemap: false,
    rollupOptions: {
      preserveEntrySignatures: 'exports-only',
      input: Object.fromEntries(Object.keys(pages).map(name => ['page-' + name, 'page:' + name])),
      output: {entryFileNames:'vue-[name].js', chunkFileNames:'vue-runtime-[hash].js', assetFileNames:'vue-[name][extname]'},
    },
  },
});
