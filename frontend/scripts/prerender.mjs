import {build} from 'vite';
import vue from '@vitejs/plugin-vue';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const directory=await mkdtemp(resolve('.prerender-'));
try{
  await build({configFile:false,plugins:[vue({template:{transformAssetUrls:false}})],build:{ssr:'src/prerender.ts',outDir:directory,emptyOutDir:true,rollupOptions:{output:{entryFileNames:'render.mjs'}}},logLevel:'warn'});
  const {renderPages}=await import(pathToFileURL(resolve(directory,'render.mjs')).href);
  await writeFile('../src/mc_translate/web/public-prerender.json',JSON.stringify(await renderPages()));
  console.log('Vue public pages prerendered');
}finally{await rm(directory,{recursive:true,force:true});}
