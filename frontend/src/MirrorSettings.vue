<script setup lang="ts">
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import {computed,ref} from 'vue';
import WorkspaceTabs from './WorkspaceTabs.vue';
import WorkspaceDrawer from './WorkspaceDrawer.vue';
import ResourceRoutingSettings from './ResourceRoutingSettings.vue';
import CloudStorageSettings from './CloudStorageSettings.vue';
import SourceQualitySettings from './SourceQualitySettings.vue';
import RoomClientCacheSettings from './RoomClientCacheSettings.vue';
import PrefetchPlatformSettings from './PrefetchPlatformSettings.vue';
import MirrorCdnSettings from './MirrorCdnSettings.vue';
import {useMirrorWorkspaceStore} from './stores/mirrorWorkspace';
import {useCloudStorageStore} from './stores/cloudStorage';
defineProps<{section:string}>();
const workspace=useMirrorWorkspaceStore(),cloud=useCloudStorageStore(),edit=ref<any>(null),editIndex=ref(-1);
const s=computed(()=>workspace.draft);
const localServing=computed(()=>Object.values(cloud.routes).some(r=>r.mode!=='cloud'));
function editSource(index:number){editIndex.value=index;edit.value=index<0?{id:'source_'+Date.now().toString(36),name:'',url:'https://',kind:'maven',region:'cn',enabled:true}:{...s.value.source_registries[index]};}
function keepSource(){if(!edit.value.name||!edit.value.url)return;const list=s.value.source_registries||=[];if(editIndex.value<0)list.push(edit.value);else list[editIndex.value]=edit.value;edit.value=null;}
</script>
<template>
<template v-if="section==='downloads'">
  <WorkspaceTabs :items="[['rules','资源去向'],['sources','下载来源'],['quality','选源规则'],['entry','下载入口']]" v-slot="{active}">
    <ResourceRoutingSettings v-if="active==='rules'"/>
    <section v-else-if="active==='sources'" class="workspace-card"><WorkspaceCardHeader title="参与自动匹配的仓库"><button type="button" @click="editSource(-1)">添加来源</button></WorkspaceCardHeader><div class="workspace-inline-stats"><label class="workspace-check"><input v-model="s.auto_sources" type="checkbox">自动扫描与映射</label><label class="workspace-check"><input v-model="s.source_prefer_cn" type="checkbox">优先国内来源</label></div><div class="workspace-table"><table><thead><tr><th>来源</th><th>类型</th><th>区域</th><th>启用</th><th></th></tr></thead><tbody><tr v-for="(item,index) in s.source_registries" :key="item.id"><td><strong>{{item.name}}</strong><small class="workspace-truncate">{{item.url}}</small></td><td>{{{maven:'Maven 仓库',minecraft:'Minecraft 镜像',catalogue:'文件清单'}[item.kind as string]||item.kind}}</td><td>{{item.region==='cn'?'国内':'全球'}}</td><td><input v-model="item.enabled" type="checkbox" :aria-label="'启用'+item.name"></td><td><button type="button" class="workspace-link" @click="editSource(Number(index))">编辑</button></td></tr></tbody></table></div><details class="workspace-help"><summary>匹配后如何处理已有缓存</summary><label class="workspace-check"><input v-model="s.source_cleanup" type="checkbox">找到已核验的替代来源后自动清理本地副本</label><p>只删除可替代的本地副本；不会因匹配数量增加而整批缓存仓库。</p></details></section>
    <SourceQualitySettings v-else-if="active==='quality'"/>
    <section v-else class="workspace-card"><WorkspaceCardHeader title="平台下载入口"/><div class="workspace-form"><label class="workspace-check"><input v-model="s.enabled" type="checkbox">启用镜像下载服务</label><label>公开 HTTPS 地址<input v-model.trim="s.public_url" type="url" placeholder="https://mirror.bugg.icu"></label></div></section>
  </WorkspaceTabs>
</template>
<CloudStorageSettings v-else-if="section==='cloud'"/>
<WorkspaceTabs v-else-if="section==='storage'" :items="[['sync','自动同步'],['egress','本机分流']]" v-slot="{active}">
  <section v-if="active==='sync'" class="workspace-card">
    <WorkspaceCardHeader title="自动同步范围"><RouterLink to="/admin/mirror/tasks">观察自动任务 →</RouterLink></WorkspaceCardHeader>
    <label class="workspace-check"><input v-model="s.auto_refresh" type="checkbox">自动更新清单并补齐缺失副本</label>
    <div class="workspace-form-grid">
      <label>清单更新间隔 / 分钟<input v-model.number="s.refresh_minutes" type="number" min="5" max="1440"></label>
      <label>回源下载并发<input v-model.number="s.concurrency" type="number" min="1" max="8"></label>
    </div>
    <PrefetchPlatformSettings/>
    <div class="workspace-footnote">只准备客户端所需文件，跳过服务端、开发映射和重复压缩表示。失败单独退避重试。</div>
    <details class="workspace-help">
      <summary>版本清单缓存</summary>
      <label>重新检查间隔 / 秒<input v-model.number="s.metadata_ttl" type="number" min="60" max="3600"></label>
      <p>版本列表、加载器清单等信息在此时间内复用已有结果，到期后重新向上游检查。不会删除游戏核心、依赖库、assets 等已缓存文件，也不影响临时副本的保留时间。</p>
    </details>
  </section>
  <section v-else class="workspace-card"><WorkspaceCardHeader title="本机出口与协作节点"/><p v-if="!localServing" class="workspace-status">全部类别使用仅云端存储，本机出口预算不限制对象存储。</p><div class="workspace-form-grid"><label>本机月出口预算 / GiB（0 不限）<input v-model.number="s.monthly_egress_gib" type="number" min="0"></label><label>达到预算后分流 / %<input v-model.number="s.egress_threshold" type="number" min="10" max="100"></label><label>带宽规划 / Mbps（0 不限）<input v-model.number="s.bandwidth_mbps" type="number" min="0"></label><label>同时发送文件数<input v-model.number="s.max_serving" type="number" min="1" max="512"></label></div><details class="workspace-help"><summary>协作节点</summary><div class="workspace-form"><label>本机权重<input v-model.number="s.node_weight" type="number" min="0" max="100"></label><div v-for="(node,index) in s.nodes" :key="index" class="workspace-node-row"><input v-model.trim="node.url" type="url" aria-label="节点地址"><input v-model.number="node.weight" type="number" min="1" max="100" aria-label="节点权重"><button type="button" class="secondary" @click="s.nodes.splice(index,1)">移除</button></div><button type="button" class="secondary" @click="s.nodes.push({url:'https://',weight:1,enabled:true})">添加下载节点</button></div></details></section>
</WorkspaceTabs>
<RoomClientCacheSettings v-else-if="section==='room-cache'"/>
<MirrorCdnSettings v-else-if="section==='cloudflare'"/>
<WorkspaceDrawer v-if="edit" title="下载来源" @close="edit=null"><div class="workspace-form"><label>名称<input v-model.trim="edit.name" maxlength="80"></label><label>类型<select v-model="edit.kind"><option value="maven">Maven 仓库</option><option value="minecraft">Minecraft 镜像</option><option value="catalogue">JSON 文件清单</option></select></label><label>HTTPS 根地址<input v-model.trim="edit.url" type="url"><small>仓库根地址以 / 结尾；JSON 清单填写完整地址。</small></label><label>区域<select v-model="edit.region"><option value="cn">国内</option><option value="global">全球</option></select></label></div><template #footer><button v-if="editIndex>=0" type="button" class="secondary" @click="s.source_registries.splice(editIndex,1);edit=null">移除来源</button><button type="button" @click="keepSource">完成编辑</button></template></WorkspaceDrawer>
</template>
