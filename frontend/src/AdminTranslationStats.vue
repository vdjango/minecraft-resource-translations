<script setup lang="ts">
import {computed,ref,watch} from 'vue';
import {useAdminStatisticsStore} from './stores/adminStatistics';
import {stamp} from './stores/mirrorWorkspace';
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import WorkspaceDrawer from './WorkspaceDrawer.vue';
const store=useAdminStatisticsStore(),detail=ref<any>(null),summary=computed(()=>store.usage?.summary||{});
const money=(n:number)=>n==null?'未知':'$'+Number(n).toFixed(6);
const names:Record<string,string>={succeeded:'成功',failed:'失败',pending:'调用中',unknown:'结果未确认'};
watch(()=>[store.filters.period,store.filters.status],()=>void store.loadUsage(),{immediate:true});
</script>
<template>
<p v-if="store.errors.usage" class="workspace-alert" role="alert">{{store.errors.usage}} <button type="button" class="workspace-link" @click="store.loadUsage()">重试</button></p>
<div v-if="!store.usage&&store.busy.usage" class="workspace-card"><span v-for="n in 5" :key="n" class="workspace-skeleton"/></div>
<template v-if="store.usage">
<div class="workspace-metrics"><article><span>{{store.filters.period==='day'?'今日':'本月'}}模型调用</span><strong>{{summary.calls||0}}</strong><small>成功 {{summary.succeeded||0}} · 失败 {{summary.failed||0}}</small></article><article><span>Token 总量</span><strong>{{(summary.tokens||0).toLocaleString()}}</strong><small>含未确认调用的预留量</small></article><article><span>已知费用 · USD</span><strong>{{money(summary.cost)}}</strong><small>{{summary.cost_unknown||0}} 次调用费用未知</small></article><article><span>调用中 / 未确认</span><strong>{{summary.unconfirmed||0}}</strong><small>统计周期 {{store.usage.key}}</small></article></div>
<p v-if="store.overview?.pause" class="workspace-alert">模型生成已暂停：{{store.overview.pause.reason==='monthly_budget'?'本月额度已用尽':'今日额度已用尽'}}，{{stamp(store.overview.pause.resume_at)}} 自动恢复。已生成译文可继续查询。</p>
<section class="workspace-card"><WorkspaceCardHeader title="模型调用明细"><RouterLink to="/admin/settings">费用与预算设置 →</RouterLink></WorkspaceCardHeader><div class="workspace-toolbar"><label>统计周期<select v-model="store.filters.period"><option value="day">今天</option><option value="month">本月</option></select></label><label>明细状态<select v-model="store.filters.status"><option value="">全部状态</option><option v-for="(label,key) in names" :key="key" :value="key">{{label}}</option></select></label><button type="button" class="secondary" :disabled="store.busy.usage" @click="store.loadUsage()">刷新列表</button></div><div class="workspace-table" :aria-busy="store.busy.usage"><table><thead><tr><th>模型</th><th>状态</th><th>Token</th><th>费用 · USD</th><th>时间</th><th></th></tr></thead><tbody><tr v-for="item in store.usage.calls||[]" :key="item.id"><td>{{item.model}}</td><td>{{names[item.status]||item.status}}</td><td>{{(item.tokens||0).toLocaleString()}}</td><td>{{money(item.cost)}}</td><td>{{stamp(item.created)}}</td><td><button type="button" class="workspace-link" @click="detail=item">详情</button></td></tr><tr v-if="!store.usage.calls?.length"><td colspan="6" class="workspace-empty">本周期没有符合条件的模型调用</td></tr></tbody></table></div><div v-if="store.usage.next_cursor" class="workspace-pagination"><button type="button" class="secondary" :disabled="store.busy.usage" @click="store.loadUsage(true)">加载更多</button></div><div class="workspace-footnote">顶部汇总覆盖所选周期全部调用；状态筛选只影响明细。费用按调用时单价估算，以服务商账单为准。</div></section>
</template>
<WorkspaceDrawer v-if="detail" title="模型调用详情" @close="detail=null"><dl class="workspace-details"><dt>模型</dt><dd>{{detail.model}}</dd><dt>输入 Token</dt><dd>{{detail.input_tokens??'未回报'}}</dd><dt>其中缓存输入</dt><dd>{{detail.cached_tokens??'未回报'}}</dd><dt>输出 Token</dt><dd>{{detail.output_tokens??'未回报'}}</dd><dt>缓存写入 Token</dt><dd>{{detail.write_tokens??'未回报'}}</dd><dt>计费依据</dt><dd>{{detail.cost_basis||'未回报'}}</dd><dt>费用 · USD</dt><dd>{{money(detail.cost)}}</dd></dl></WorkspaceDrawer>
</template>
