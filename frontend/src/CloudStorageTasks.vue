<script setup lang="ts">
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import {computed,ref,watch} from 'vue';
import {useRoute} from 'vue-router';
import {cloudKinds,useCloudStorageStore} from './stores/cloudStorage';
import {bytes,stamp,filename} from './stores/mirrorWorkspace';
import WorkspaceTabs from './WorkspaceTabs.vue';
import WorkspaceDrawer from './WorkspaceDrawer.vue';
const cloud=useCloudStorageStore(),route=useRoute(),detail=ref<any>(null);
const queue=computed(()=>cloud.snapshot.queue||{});
const count=(key:string)=>queue.value[key]?.count||0;
const active=computed(()=>cloud.snapshot.active_jobs||[]);
const target=(id:string)=>cloud.delivery.profiles.find((p:any)=>p.id===id)?.name||id;
const category=(key:string)=>cloudKinds.find(([name])=>name===key)?.[1]||key;
const states:Record<string,string>={queued:'排队中',uploading:'上传中',finished:'已存入云空间',failed:'等待自动重试',waiting_capacity:'等待容量',waiting_configuration:'等待配置'};
const worker:Record<string,string>={paused:'上传已暂停',uploading:'正在上传',verifying:'正在核验云端副本',queued:'队列正在处理',idle:'自动巡检中',attention:'异常文件等待自动重试',waiting_budget:'等待上传预算'};
const label=(job:any)=>job.state==='finished'&&job.download_health?.healthy===false?'已上传 · 下载线路异常':states[job.state]||job.state;
const percentage=(job:any)=>job.size?Math.min(100,100*(job.progress||0)/job.size):0;
watch(()=>route.query.view,view=>{cloud.jobFilter.state=view==='failed'?'failed':view==='history'?'finished':'';cloud.jobFilter.offset=0;void cloud.loadJobs();},{immediate:true});
watch(()=>[cloud.jobFilter.route,cloud.jobFilter.state,cloud.jobFilter.offset],()=>{void cloud.loadJobs();});
watch(()=>cloud.snapshot,()=>{if(!cloud.jobsLoading)void cloud.loadJobs();});
</script>
<template>
<div class="workspace-stack">
  <div class="workspace-inline-stats"><span>等待上传 <b>{{count('queued')}}</b> 项</span><span>异常 / 等待 <b>{{count('failed')+count('waiting_capacity')+count('waiting_configuration')}}</b> 项</span><span>{{active.length}} 路上传 · {{active.length?bytes(cloud.snapshot.upload_bytes_per_second)+'/s':'暂无传输'}}</span><RouterLink to="/admin">副本与容量统计 →</RouterLink></div>
  <div class="workspace-status"><span class="status-dot" :class="{warning:cloud.snapshot.paused}"/><strong>{{worker[cloud.snapshot.worker_state]||'正在读取状态'}}</strong><RouterLink to="/admin/mirror/settings/cloud">存储设置 →</RouterLink></div>
  <section v-if="active.length" class="workspace-card"><WorkspaceCardHeader title="正在写入云空间"><span class="workspace-hint">{{active.length}} / {{cloud.snapshot.upload_concurrency||1}} 路</span></WorkspaceCardHeader><div class="workspace-active-grid"><article v-for="job in active" :key="job.id"><div class="workspace-toolbar"><strong>{{category(job.route)}} → {{target(job.profile)}}</strong><span>{{bytes(job.upload_bytes_per_second)}}/s</span></div><p class="workspace-truncate">{{filename(job.file_path||job.digest)}}</p><progress :value="percentage(job)" max="100"/><small>{{bytes(job.progress)}} / {{bytes(job.size)}} · {{percentage(job).toFixed(0)}}%</small></article></div></section>
  <WorkspaceTabs :items="[['queue','传输队列'],['failed','异常与等待'],['history','已完成']]">
    <section class="workspace-card">
      <div class="workspace-toolbar"><label>资源类别<select v-model="cloud.jobFilter.route"><option value="">全部类别</option><option v-for="[key,name] in cloudKinds" :key="key" :value="key">{{name}}</option></select></label><label v-if="route.query.view==='failed'">状态<select v-model="cloud.jobFilter.state"><option value="failed">等待重试</option><option value="waiting_capacity">等待容量</option><option value="waiting_configuration">等待配置</option></select></label><span class="workspace-hint">{{cloud.jobPage.total}} 项</span><button class="secondary" :disabled="cloud.jobsLoading" @click="cloud.loadJobs()">刷新列表</button></div>
      <p v-if="cloud.jobsError" class="workspace-alert">{{cloud.jobsError}}</p>
      <div class="workspace-table" :aria-busy="cloud.jobsLoading"><table><thead><tr><th>文件</th><th>目标空间</th><th>大小</th><th>状态</th><th></th></tr></thead><tbody>
        <tr v-if="cloud.jobsLoading&&!cloud.jobPage.jobs.length" v-for="n in 5" :key="n"><td colspan="5"><span class="workspace-skeleton"/></td></tr>
        <tr v-for="job in cloud.jobPage.jobs" :key="job.id"><td><strong class="workspace-filename">{{filename(job.file_path||job.digest)}}</strong><small>{{category(job.route)}}</small></td><td>{{target(job.profile)}}</td><td class="workspace-nowrap">{{bytes(job.size)}}</td><td><span class="workspace-badge" :class="{warning:job.state!=='finished'}">{{label(job)}}</span><small v-if="job.error" class="workspace-reason">{{job.error}}</small><small v-if="job.retry_at">{{stamp(job.retry_at)}} 自动继续</small></td><td><button class="workspace-link" @click="detail=job">详情</button></td></tr>
        <tr v-if="!cloud.jobsLoading&&!cloud.jobPage.jobs.length"><td colspan="5" class="workspace-empty">暂无此类任务，后台会自动发现并排队。</td></tr>
      </tbody></table></div>
      <div class="workspace-pagination"><span>第 {{Math.floor(cloud.jobFilter.offset/20)+1}} 页</span><button class="secondary" :disabled="!cloud.jobFilter.offset||cloud.jobsLoading" @click="cloud.jobFilter.offset=Math.max(0,cloud.jobFilter.offset-20)">上一页</button><button class="secondary" :disabled="cloud.jobFilter.offset+20>=cloud.jobPage.total||cloud.jobsLoading" @click="cloud.jobFilter.offset+=20">下一页</button></div>
    </section>
  </WorkspaceTabs>
  <div class="workspace-status"><span class="status-dot"/><span>本轮检查本地副本 {{cloud.snapshot.automatic?.scanned||0}} 条 · 新增上传 {{cloud.snapshot.automatic?.queued||0}} 项</span><small>最近 {{stamp(cloud.snapshot.automatic?.last_at)}}</small></div>
  <WorkspaceDrawer v-if="detail" title="云端任务详情" @close="detail=null"><dl class="workspace-details"><dt>文件</dt><dd>{{detail.file_path||detail.digest}}</dd><dt>目标</dt><dd>{{target(detail.profile)}} / {{detail.object_key}}</dd><dt>状态</dt><dd>{{label(detail)}}</dd><dt>下次自动重试</dt><dd>{{stamp(detail.retry_at)}}</dd><dt>具体原因</dt><dd>{{detail.error||detail.download_health?.error||'无异常'}}</dd><dt>本地副本</dt><dd>{{detail.local?'仍保留':'已释放或不存在'}}</dd></dl></WorkspaceDrawer>
</div>
</template>
