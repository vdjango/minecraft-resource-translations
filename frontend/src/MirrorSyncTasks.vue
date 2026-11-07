<script setup lang="ts">
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import {computed,ref} from 'vue';
import WorkspaceTabs from './WorkspaceTabs.vue';
import WorkspaceDrawer from './WorkspaceDrawer.vue';
import {bytes,filename,stamp,useMirrorWorkspaceStore} from './stores/mirrorWorkspace';
import {inventoryLink} from './stores/mirrorInventory';
const store=useMirrorWorkspaceStore(),details=ref<any>(null);
const job=computed(()=>store.data.job||{}),operations=computed(()=>store.data.operations||{}),failed=computed(()=>operations.value.failed_queue||{}),automatic=computed(()=>store.data.automation||{});
const states:Record<string,string>={running:'运行中',idle:'等待调度',finished:'已完成',incomplete:'已完成 · 部分文件自动补齐',failed:'稍后自动重试',disk_limit:'等待可用空间',interrupted:'已中断 · 自动接续',cancelled:'已结束'};
const phases:Record<string,string>={catalogue:'更新资源清单',sources:'核验公开来源',selection:'筛选所需文件',download:'下载并校验',loader_metadata:'更新加载器索引'};
const actions:Record<string,string>={refresh:'清单更新',automatic:'自动补齐',prefetch:'资源准备',retry_failures:'失败补齐',inventory:'清单登记',source_cleanup:'清理副本'};
const progress=computed(()=>job.value.total?Math.min(100,(job.value.done||0)/job.value.total*100):undefined);
const current=computed(()=>job.value.state==='running');
const counts=computed(()=>automatic.value.round_counts||{});
const eligible=computed(()=>(counts.value.existing||0)+(counts.value.queued||0)+(counts.value.retrying||0));
const exclusions:Record<string,string>={redirect_policy:'仅重定向',platform:'未选择的平台',server_file:'服务端文件',developer_mapping:'开发映射',duplicate_compression:'重复压缩格式'};
const host=(url:string)=>{try{return new URL(url).hostname;}catch{return '未知来源';}};
const destination=(file:any)=>file.destination==='cloud'?('云端 · '+(store.data.settings?.qiniu_profiles?.find((p:any)=>p.id===file.profile)?.name||file.profile)):file.destination==='local'?'本地磁盘':'按类别存储';
</script>
<template>
  <div class="workspace-status"><span class="status-dot"/><span>自动同步{{store.data.settings?.auto_refresh?'已启用':'已关闭'}} · 下次清单更新 {{stamp(store.data.refresh?.next_at)}}</span><RouterLink to="/admin">总览统计 →</RouterLink></div>
  <WorkspaceTabs :items="[['current','正在处理'],['failures','自动重试 · '+(failed.count||0)],['history','最近记录']]" v-slot="{active}">
    <template v-if="active==='current'">
      <article class="workspace-card"><WorkspaceCardHeader :title="current?phases[job.phase]||'自动处理资源':'自动调度已就绪'" :description="current?'单个文件失败进入独立重试队列':'新资源和缺失副本将按设置自动补齐'"><span class="workspace-pill" :class="{working:current}">{{states[job.state]||'等待任务'}}</span></WorkspaceCardHeader><progress v-if="current" :value="progress" max="100" aria-label="同步进度"/><div class="workspace-inline-stats"><span>本批 {{job.done||0}} / {{job.total||0}}</span><span>成功 {{job.cached||0}}</span><span>跳过 {{job.skipped||0}}</span><span>失败 {{job.errors||0}}</span></div><p v-if="job.error" class="workspace-error">{{job.error}}</p><div class="workspace-footnote">清单筛选 {{automatic.position||0}} / {{automatic.total||0}} 条 · {{automatic.state==='waiting_capacity'?'等待容量恢复，5 分钟后复查':automatic.state==='watching'?'本轮筛选完成':'分批检查中'}}<RouterLink to="/admin/mirror/settings/storage">调整同步范围 →</RouterLink></div>
        <div v-if="automatic.round_counts" class="workspace-inline-stats"><span>符合缓存条件 <b>{{eligible}}</b></span><span>已有副本 <b>{{counts.existing||0}}</b></span><span>本轮安排 <b>{{counts.queued||0}}</b></span><span>等待重试 <b>{{counts.retrying||0}}</b></span><button class="workspace-link" type="button" @click="details={selection:true}">查看跳过原因</button></div>
        <div class="workspace-footnote"><RouterLink :to="inventoryLink()">查看清单文件与处理原因 →</RouterLink><RouterLink to="/admin/mirror/resources">查看实际副本 →</RouterLink></div>
      </article>
      <section class="workspace-card"><WorkspaceCardHeader title="实时回源"><span class="muted">{{operations.transfers?.length||0}} 路传输</span></WorkspaceCardHeader><div class="workspace-table"><table><thead><tr><th>文件</th><th>最终位置</th><th>进度</th><th>速度</th></tr></thead><tbody><tr v-for="file in operations.transfers||[]" :key="file.url"><td class="file-name">{{filename(file.url)}}<small>{{file.source_host||file.source||host(file.url)}}</small></td><td>{{file.destination||file.storage||'按类别存储'}}</td><td>{{bytes(file.bytes)}} / {{file.total?bytes(file.total):'未知'}}</td><td>{{bytes(file.speed)}}/s</td></tr><tr v-if="!operations.transfers?.length"><td colspan="4" class="workspace-empty">当前没有正在传输的文件</td></tr></tbody></table></div></section>
    </template>
    <section v-else-if="active==='failures'" class="workspace-card"><WorkspaceCardHeader title="失败自动补齐" description="先尝试替代来源，仍失败则退避重试；其他文件继续。"/><div class="workspace-table"><table><thead><tr><th>文件</th><th>错误</th><th>失败次数</th><th>下次自动重试</th><th></th></tr></thead><tbody><tr v-for="file in failed.files||[]" :key="file.url"><td class="file-name">{{filename(file.url)}}</td><td><span class="workspace-pill warning">{{file.status?'HTTP '+file.status:'网络错误'}}</span><small class="line-clamp">{{file.error}}</small></td><td>{{file.attempts}}</td><td>{{stamp(file.next_retry)}}</td><td><button class="text-action" type="button" @click="details=file">原因</button></td></tr><tr v-if="!failed.files?.length"><td colspan="5" class="workspace-empty">没有等待重试的文件</td></tr></tbody></table></div></section>
    <section v-else class="workspace-card"><div class="workspace-table"><table><thead><tr><th>自动任务</th><th>状态</th><th>处理 / 总数</th><th>开始时间</th><th></th></tr></thead><tbody><tr v-for="item in operations.history||[]" :key="item.started_at"><td>{{actions[item.action]||item.action}}</td><td>{{states[item.state]||item.state}}</td><td>{{item.done||0}} / {{item.total||0}}</td><td>{{stamp(item.started_at)}}</td><td><button class="text-action" type="button" @click="details=item">详情</button></td></tr><tr v-if="!operations.history?.length"><td colspan="5" class="workspace-empty">暂无任务记录</td></tr></tbody></table></div></section>
  </WorkspaceTabs>
  <WorkspaceDrawer v-if="details" :title="details.selection?'本轮清单筛选':'任务详情'" @close="details=null"><dl v-if="details.selection" class="workspace-details"><template v-for="(label,key) in exclusions" :key="key"><dt>{{label}}</dt><dd>{{counts[key]||0}} 条</dd></template></dl><dl v-else class="workspace-details"><dt>文件 / 任务</dt><dd>{{details.url||actions[details.action]||'自动任务'}}</dd><dt>状态</dt><dd>{{details.status?'HTTP '+details.status:states[details.state]||'网络错误'}}</dd><dt>具体原因</dt><dd>{{details.error||details.last_error||'无异常'}}</dd><dt>失败次数</dt><dd>{{details.attempts||details.errors||0}}</dd><dt>下次自动重试</dt><dd>{{stamp(details.next_retry)}}</dd></dl></WorkspaceDrawer>
</template>
