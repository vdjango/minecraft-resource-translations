<script setup lang="ts">
import {computed,watch} from 'vue';
import {useAdminStatisticsStore} from './stores/adminStatistics';
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
const store=useAdminStatisticsStore(),data=computed(()=>store.traffic),today=computed(()=>data.value?.today||{});
const percent=(n:number)=>n==null?'—':(n*100).toFixed(1)+'%';
const names:Record<string,string>={resolve:'查询译文',ensure:'申请翻译',catalogue:'资源搜索',artifact:'译文正文',manifest:'译文状态',source:'原文',refresh:'检查更新',job:'任务进度',events:'事件订阅',history:'更新历史',other:'其他请求'};
const daily=computed(()=>{if(!data.value)return [];const end=Date.parse(data.value.day+'T00:00:00+08:00');return Array.from({length:store.filters.days},(_,i)=>{const day=new Date(end-(store.filters.days-i-1)*86400000).toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'});return {label:day.slice(5),requests:data.value.daily?.find((p:any)=>p.day===day)?.requests||0};});});
const maximum=computed(()=>Math.max(1,...daily.value.map(p=>p.requests)));
watch(()=>store.filters.days,()=>void store.loadTraffic(),{immediate:true});
</script>
<template>
<p class="workspace-footnote">仅统计公共翻译 API；镜像文件下载和 Cloudflare 边缘流量不在此统计。</p>
<p v-if="store.errors.traffic" class="workspace-alert" role="alert">{{store.errors.traffic}} <button type="button" class="workspace-link" @click="store.loadTraffic">重试</button></p>
<div v-if="!data&&store.busy.traffic" class="workspace-card"><span v-for="n in 5" :key="n" class="workspace-skeleton"/></div>
<template v-if="data">
  <div class="workspace-metrics"><article><span>今日翻译 API 请求</span><strong>{{today.requests||0}}</strong><small>北京时间 {{data.day}}</small></article><article><span>今日接入应用</span><strong>{{today.active_integrations||0}}</strong><small>按应用密钥去重</small></article><article><span>译文字段命中率</span><strong>{{percent(today.field_hit_ratio)}}</strong><small>{{today.ready_fields||0}} / {{today.fields||0}} 个字段</small></article><article><span>请求错误</span><strong>{{today.errors||0}}</strong><small>其中限流 {{today.throttled||0}} 次</small></article></div>
  <section class="workspace-card"><WorkspaceCardHeader title="请求趋势"><select v-model="store.filters.days" aria-label="统计天数"><option :value="1">今天</option><option :value="7">近 7 天</option><option :value="30">近 30 天</option></select><button type="button" class="workspace-link" :disabled="store.busy.traffic" @click="store.loadTraffic">刷新</button></WorkspaceCardHeader><div class="stats-bars" role="img" aria-label="每日翻译 API 请求次数"><div v-for="point in daily" :key="point.label" :title="point.label+' · '+point.requests+' 次请求'"><span :style="{height:Math.max(2,point.requests/maximum*130)+'px'}"/><small>{{point.label}}</small></div></div></section>
  <section class="workspace-card"><WorkspaceCardHeader title="接口访问"/><div class="workspace-table"><table><thead><tr><th>接口</th><th>今日请求</th><th>错误</th><th>响应时间 p95</th></tr></thead><tbody><tr v-for="item in data.endpoints||[]" :key="item.endpoint"><td>{{names[item.endpoint]||item.endpoint}}</td><td>{{item.requests}}</td><td>{{item.errors}}</td><td>{{!item.latency?.samples?'暂无样本':item.latency.p95_le_ms==null?'> 5000 ms':'≤ '+item.latency.p95_le_ms+' ms'}}</td></tr><tr v-if="!data.endpoints?.length"><td colspan="4" class="workspace-empty">暂无接口访问</td></tr></tbody></table></div><details class="workspace-help"><summary>统计范围与口径</summary><p>数据每 {{data.aggregation?.interval_seconds||5}} 秒聚合。命中率按查询字段统计；p95 是服务端响应时间的分桶上界，不包含网络往返。管理页面、镜像文件、客户端本地和 CDN 缓存命中不计入。</p><p>今日启动器实例 {{today.launcher_instances||0}}，其中匿名上报 {{today.reported_instances||0}}、估算 {{today.estimated_instances||0}}。不代表独立玩家数。</p></details></section>
</template>
</template>
<style scoped>.stats-bars{height:175px;display:flex;align-items:end;gap:8px;overflow-x:auto}.stats-bars>div{flex:1;min-width:20px;display:flex;flex-direction:column;align-items:center;gap:8px}.stats-bars span{width:100%;max-width:60px;background:var(--accent,#8b6bdf);border-radius:4px 4px 0 0}.stats-bars small{font-size:10px;white-space:nowrap}</style>
