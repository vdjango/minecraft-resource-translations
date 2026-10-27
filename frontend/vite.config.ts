import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';

const web = resolve(import.meta.dirname, '../src/mc_translate/web');
const pages = JSON.parse(readFileSync(new URL('./pages.json', import.meta.url), 'utf8')) as Record<string, string[]>;
export default defineConfig({
  base: '/assets/',
  plugins: [vue(), {
    name: 'existing-page-controllers',
    writeBundle(_options,bundle) {
      for(const name of Object.keys(pages)) {
        const entry=bundle['vue-page-'+name+'.js'];
        if(!entry || entry.type!=='chunk')continue;
        const revision=createHash('sha256').update(entry.code).digest('hex').slice(0,12);
        const html=resolve(web, name+'.html');
        const previous=readFileSync(html,'utf8');
        const needle='vue-page-'+name+'.js';
        const start=previous.indexOf(needle);
        const end=previous.indexOf('"',start);
        const updated=start<0||end<0?previous:previous.slice(0,start+needle.length)+'?v='+revision+previous.slice(end);
        if(previous!==updated)writeFileSync(html,updated);
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
        mountPage(function(scope, pageViews) {
          const {fetch,setTimeout,clearTimeout,setInterval,clearInterval}=scope;
          ${source}\n${exports}\n
        });`;
    },
  }],
  build: {
    target: 'es2022', outDir: web, emptyOutDir: false, sourcemap: false,
    rollupOptions: {
      input: Object.fromEntries(Object.keys(pages).map(name => ['page-' + name, 'page:' + name])),
      output: {entryFileNames:'vue-[name].js', chunkFileNames:'vue-runtime-[hash].js', assetFileNames:'vue-[name][extname]'},
    },
  },
});
