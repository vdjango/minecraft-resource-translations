<script setup lang="ts">
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import {computed} from 'vue';
import {useSourceQualityStore} from './stores/sourceQuality';
import {bytes,stamp} from './stores/mirrorWorkspace';
import WorkspaceDrawer from './WorkspaceDrawer.vue';
const quality=useSourceQualityStore();
const grades:Record<string,string>={excellent:'优',good:'良',poor:'需关注',observing:'观察中',stale:'待复核'};
const health:Record<string,string>={unknown:'待观测',healthy:'可用',slow:'持续慢',unavailable:'暂不可达'};
const enabled=computed(()=>quality.sources.filter(s=>s.enabled!==false));
const measured=computed(()=>enabled.value.filter(s=>(s.recent_speed_bps??s.speed_bps)>0));
const speed=(n?:number)=>n==null?'待采样':bytes(n)+'/s';
const mappedCount=(s:any)=>Number(s.mapped_files||0);
const coverage=(s:any)=>s.enabled===false?'来源已停用':!mappedCount(s)?'未匹配文件':s.id?.startsWith('cdn:')?mappedCount(s)+' 个候选地址':mappedCount(s)+' 个已匹配文件';
const provider=(s:any)=>({minecraft:'Minecraft 镜像',maven:'Maven 仓库',catalogue:'文件清单'}[s.kind as string]||'内容 CDN');
const bands=[0,64,128,256,512,1024,2048,4096];
const histogram=computed(()=>bands.map((edge,i)=>({label:i===7?'≥ 4096':i===0?'< 64':edge+'–'+bands[i+1],count:measured.value.filter(s=>{const n=(s.recent_speed_bps??s.speed_bps)/1024;return n>=edge&&(i===7||n<bands[i+1]);}).length})));
const healthGroups=computed(()=>[
  {label:'可用',count:enabled.value.filter(s=>s.health==='healthy').length,color:'#78c8ae'},
  {label:'持续慢',count:enabled.value.filter(s=>s.health==='slow').length,color:'#e1b675'},
  {label:'不可达',count:enabled.value.filter(s=>s.health==='unavailable').length,color:'#dc8d94'},
  {label:'待观测',count:enabled.value.filter(s=>!['healthy','slow','unavailable'].includes(s.health)).length,color:'#756b88'},
]);
const donut=computed(()=>{let start=0;return 'conic-gradient('+healthGroups.value.map(g=>{const end=start+g.count/Math.max(1,enabled.value.length)*100;const stop=g.color+' '+start+'% '+end+'%';start=end;return stop;}).join(',')+')';});
const chart=computed(()=>{const rows=[...(quality.history||[])].reverse().filter(r=>r.outcome==='ok'&&r.speed>0),max=Math.max(1,...rows.map(r=>r.speed));return rows.map((r,i)=>i*600/Math.max(1,rows.length-1)+','+(100-90*r.speed/max)).join(' ');});
</script>
<template>
<section class="workspace-card">
  <WorkspaceCardHeader title="来源质量" description="Minecraft 镜像、公共仓库与内容 CDN 共用采样与评分。"><RouterLink to="/admin/mirror/settings/downloads?view=quality">采样设置 →</RouterLink></WorkspaceCardHeader>
  <div class="quality-charts">
    <section class="quality-health"><h3>来源可用情况 <span>{{enabled.length}} 个启用</span></h3><div class="quality-health-body"><div class="quality-donut" :style="{background:enabled.length?donut:'#756b88'}" role="img" :aria-label="healthGroups.map(g=>g.label+' '+g.count+' 个').join('，')"><div><strong>{{healthGroups[0].count}}</strong><small>可用来源</small></div></div><ul><li v-for="g in healthGroups" :key="g.label"><i :style="{background:g.color}"/><span>{{g.label}}</span><strong>{{g.count}}</strong></li></ul></div></section>
    <section class="quality-speeds"><h3>下载速度分布 <span>KiB/s · {{measured.length}} 个有速度数据</span></h3><div v-for="band in histogram" :key="band.label" class="quality-speed-row"><span>{{band.label}}</span><div><i :style="{width:100*band.count/Math.max(1,...histogram.map(b=>b.count))+'%'}"/></div><strong>{{band.count}}</strong></div></section>
  </div>
  <div class="workspace-table"><table><thead><tr><th>来源</th><th>类型与覆盖</th><th>状态</th><th>近期速度</th><th>长期评分</th><th>下次采样</th><th></th></tr></thead><tbody>
    <tr v-for="source in quality.sources" :key="source.id">
      <td><strong>{{source.name}}</strong><small>{{source.host||source.url}}</small></td><td>{{provider(source)}}<small>{{coverage(source)}}</small></td>
      <td><span class="workspace-badge" :class="{warning:['slow','unavailable'].includes(source.health)}">{{source.enabled===false?'已停用':health[source.health]||'待观测'}}</span></td>
      <td class="workspace-nowrap">{{speed(source.recent_speed_bps??source.speed_bps)}}</td>
      <td><span class="quality-score">{{grades[source.grade]||'观察中'}} <b>{{source.score==null?'—':source.score+'/100'}}</b></span><span v-if="source.score!=null" class="quality-score-track"><i :style="{width:Math.max(0,Math.min(100,source.score))+'%'}"/></span><small>{{source.samples||0}} 次 · {{source.days||0}} 天</small></td>
      <td>{{source.enabled!==false?stamp(source.next_at):'—'}}</td><td><button v-if="source.samples>0" type="button" class="workspace-link" @click="quality.showHistory(source)">趋势</button><span v-else class="workspace-hint">待采样</span></td>
    </tr>
  </tbody></table></div>
  <div class="workspace-footnote"><span>今日采样 {{bytes(quality.observation.daily_bytes)}} / {{bytes(quality.observation.daily_limit)}}</span><span>速度为本站到来源的观测；未匹配文件的来源不测速。</span></div>
</section>
<WorkspaceDrawer v-if="quality.history!==null" :title="quality.historyName+' · 采样历史'" @close="quality.closeHistory">
  <div v-if="quality.historyLoading" aria-busy="true"><span v-for="n in 4" :key="n" class="workspace-skeleton"/></div>
  <template v-else>
    <svg v-if="chart" class="quality-line" viewBox="-5 -5 610 120" role="img" aria-label="成功采样的下载速度趋势"><path d="M0 100H600 M0 55H600 M0 10H600" stroke="currentColor" opacity=".15"/><polyline :points="chart" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>
    <div class="workspace-table"><table><thead><tr><th>时间</th><th>结果</th><th>速度</th></tr></thead><tbody><tr v-for="row in quality.history" :key="row.at"><td>{{stamp(row.at)}}</td><td>{{row.outcome==='ok'?'成功':row.outcome==='missing'?'未覆盖该文件':'连接失败'}} {{row.status||''}}</td><td>{{speed(row.outcome==='ok'?row.speed:undefined)}}</td></tr><tr v-if="!quality.history.length"><td colspan="3" class="workspace-empty">暂无采样记录</td></tr></tbody></table></div>
  </template>
</WorkspaceDrawer>
</template>
<style scoped>
.quality-charts{display:grid;grid-template-columns:minmax(220px,1fr) minmax(280px,1.3fr);gap:24px;padding:14px 0 20px;border-bottom:1px solid var(--workspace-border);margin-bottom:12px}.quality-charts h3{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 0 16px;font-size:12px}.quality-charts h3 span{font-weight:400;font-size:11px;color:var(--muted)}.quality-health-body{display:flex;align-items:center;gap:24px}.quality-donut{width:132px;height:132px;flex:none;border-radius:50%;padding:12px}.quality-donut>div{border-radius:50%;height:100%;background:var(--workspace-surface);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px}.quality-donut strong{font-size:26px;font-variant-numeric:tabular-nums}.quality-donut small{font-size:11px;color:var(--muted)}.quality-health ul{list-style:none;padding:0;margin:0;flex:1;max-width:210px;display:grid;gap:13px}.quality-health li{display:flex;align-items:center;gap:8px;font-size:12px}.quality-health li i{width:7px;height:7px;border-radius:50%;flex:none}.quality-health li strong{margin-left:auto}.quality-speed-row{display:grid;grid-template-columns:76px 1fr 24px;align-items:center;gap:10px;margin:7px 0;font-size:11px;color:var(--muted)}.quality-speed-row>div{height:7px;background:#9987ba18;border-radius:4px;overflow:hidden}.quality-speed-row i{display:block;height:100%;background:#b39adf;border-radius:4px}.quality-speed-row strong{text-align:right;font-size:11px;color:var(--text)}.quality-score{display:flex;gap:8px;font-size:12px}.quality-score b{font-weight:500;color:var(--muted);font-size:11px}.quality-score-track{display:block;height:3px;width:90px;background:#9987ba20;margin-top:5px;border-radius:3px}.quality-score-track i{display:block;height:100%;background:#b39adf;border-radius:3px}.quality-line{width:100%;height:170px;background:#8f75b510;color:#b49ae8;border-radius:8px;margin-bottom:18px}@media(max-width:800px){.quality-charts{grid-template-columns:1fr;gap:20px}.quality-health-body{gap:20px}.quality-health ul{max-width:none}.quality-charts h3{flex-wrap:wrap}}
</style>
