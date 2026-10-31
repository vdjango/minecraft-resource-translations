<script setup lang="ts">
import {onMounted, ref} from 'vue';
const state=ref('正在检查资源下载'), count=ref('—'), size=ref('—');
const policies=ref<Record<string,string>>({});
const nodes=ref<{url:string,weight:number}[]>([]), accepting=ref(true);
const sourceCount=ref(0);
const mirrorAddress=window.location.origin;
const modes:Record<string,string>={cache:'本地缓存',redirect:'官方重定向',backup:'备份并重定向',disabled:'本地关闭 · 官方下载'};
const endpoints=[['游戏核心','/mc/game/ · /v1/packages/ · /v1/objects/','Mojang','core'],['加载器','/maven/ · /fabric-meta/ · /quilt-meta/…','Forge / Fabric / Quilt / NeoForged','loaders'],['模组文件','/mods/modrinth/ · /mods/curseforge/','Modrinth / CurseForge','mods'],['Java 运行时','/java/v1/…','Mojang','java'],['游戏依赖','/maven/…','Mojang','libraries'],['原版资源','/assets/{前两位}/{SHA-1}','Mojang','assets']];
onMounted(async()=>{try{const response=await fetch('/mirror-api/status',{signal:AbortSignal.timeout(5000)});if(!response.ok)throw Error();const data=await response.json();state.value=data.running?'资源下载在线':'资源下载待启动';policies.value=data.policies||{};nodes.value=data.nodes||[];accepting.value=data.accepting!==false;sourceCount.value=data.source_mappings||0;count.value=Number(data.cached_files||0).toLocaleString('zh-CN');size.value=((data.cached_bytes||0)/1024**3).toFixed(2)+' GiB';}catch{state.value='暂时无法读取下载状态';}});
const copyState=ref('复制地址');
async function copy(){try{await navigator.clipboard.writeText(mirrorAddress);copyState.value='地址已复制';}catch{copyState.value='请手动复制下方地址';}}
</script>
<template>
<section id="downloads" class="platform-downloads"><div class="platform-section-heading"><div><div class="eyebrow">DOWNLOAD &amp; PLAY</div><h2>游戏下载与运行环境</h2><p class="muted">游戏核心、加载器、模组、Java、依赖库与原版资源，同一地址接入。</p></div><span class="platform-status">{{ state }}</span></div><section class="mirror-facts" aria-label="下载状态"><div><span>本地缓存文件</span><strong>{{ count }}</strong></div><div><span>已缓存数据</span><strong>{{ size }}</strong></div><div><span>文件校验</span><strong>SHA-1 / SHA-256</strong></div><div><span>内容范围</span><strong>游戏 · 依赖 · 加载器</strong></div></section>

<section id="download-paths" class="mirror-connect"><div class="mirror-section-head"><div><div class="eyebrow">ONE ADDRESS. MANY WORLDS.</div><h2>从这里，开始下载。</h2></div><button class="secondary" type="button" @click="copy">{{ copyState }}</button></div><div class="mirror-address"><span>当前镜像地址</span><code>{{ mirrorAddress }}</code><small>与译文平台共用域名</small></div><div class="table-scroll"><table class="mirror-endpoints"><thead><tr><th>内容</th><th>下载路径</th><th>官方来源</th><th>当前策略</th></tr></thead><tbody><tr v-for="row in endpoints" :key="row[0]"><td>{{ row[0] }}</td><td><code>{{ row[1] }}</code></td><td>{{ row[2] }}</td><td>{{ modes[policies[row[3]]] || "读取中" }}</td></tr></tbody></table></div><p class="muted small">启动器需按文档映射下载 URL；元数据保留官方内容，尚未提供 BMCLAPI 的 Forge 查询等全部接口。模组默认直接跳转原站，管理员可按文件启用缓存或备份。整合包、第三方资源包与光影不提供。OptiFine、旧版 Oracle Java 与 LiteLoader 暂不提供。</p></section>
<section class="mirror-section"><div class="eyebrow">DIRECT TO THE SOURCE</div><h2>已有稳定来源的文件，直接抵达。</h2><p class="muted">当前启用 {{ sourceCount }} 条公开文件直接映射。命中映射的依赖由对应 Maven 或公开源直接提供，跳过本地缓存和预下载；客户端下载后继续验证官方清单的哈希。</p><p class="muted small">Java 发行版的 Linux 安装包可以独立获取，Mojang 运行时仍按原清单下载。具体来源规则由管理员维护。</p></section>
</section>
</template>
