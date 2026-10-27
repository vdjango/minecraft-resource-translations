<script setup lang="ts">
import { computed, reactive } from 'vue';
export interface Project {
  resource:{provider:string;project_id:string};title:string;summary:string;localized_summary?:string;kind:string;translation_state:string;
  update?:{code?:string;notice?:string}; metadata?:{icon_url?:string;authors?:{name:string}[];tags?:string[];loaders?:string[];updated_at?:string;game_versions?:string[];downloads?:number;dependency_count?:number};
  badge?:string;
}
const props=defineProps<{projects:Project[];language:string;loading:boolean;open:(resource:Project['resource'])=>void}>();
const labels:Record<string,string>={mod:'模组',resourcepack:'资源包',shader:'光影',modpack:'整合包'};
const tags:Record<string,string>={optimization:'性能优化',adventure:'冒险',decoration:'装饰',equipment:'装备',food:'食物',library:'前置库',magic:'魔法',mobs:'生物',storage:'存储',technology:'科技',utility:'实用工具',worldgen:'世界生成',realistic:'写实',fantasy:'奇幻','vanilla-like':'原版风格',animated:'动画',themed:'主题',challenging:'挑战',combat:'战斗','kitchen-sink':'综合玩法',quests:'任务',multiplayer:'多人',lightweight:'轻量',performance:'性能','API and Library':'API 与前置库'};
const key=(p:Project)=>p.resource.provider+':'+p.resource.project_id;
const loadedIcons=reactive<Record<string,boolean>>({});
const safeIcon=(url?:string)=>{try{return url&&new URL(url).protocol==='https:'?url:undefined;}catch{return undefined;}};
const date=(value?:string)=>value&&!Number.isNaN(new Date(value).getTime())?new Date(value).toLocaleDateString('zh-CN'):'暂未提供';
const versions=(values?:string[])=>{const v=[...(values||[])].sort(new Intl.Collator('en',{numeric:true}).compare).reverse();return v.length?v.slice(0,3).join(' / ')+(v.length>3?` 等 ${v.length} 个版本`:''):'版本信息待补全';};
const rows=computed(()=>props.projects.map(p=>({p,m:p.metadata||{},key:key(p)})));
const information=(p:Project)=>{const m=p.metadata||{};return [['更新时间',date(m.updated_at)],['支持版本',versions(m.game_versions)],['下载量',new Intl.NumberFormat('zh-CN',{notation:'compact',maximumFractionDigits:1}).format(Number(m.downloads)||0)],['依赖关系',m.dependency_count==null?'按版本查看':`最新版本 ${m.dependency_count} 项`]];};
</script>
<template>
  <template v-if="loading && !projects.length"><div v-for="i in 3" :key="i" class="loading-placeholder" aria-hidden="true"></div></template>
  <article v-for="{p,m,key:identity} in rows" :key="identity" class="card resource-card">
    <div class="project-icon" style="position:relative"><span v-if="!loadedIcons[m.icon_url||'']">{{ (p.title||'B').slice(0,1) }}</span><img v-if="safeIcon(m.icon_url)" :style="loadedIcons[m.icon_url||'']?undefined:{position:'absolute',inset:'0',opacity:'0'}" :src="safeIcon(m.icon_url)" alt="" loading="lazy" referrerpolicy="no-referrer" @load="loadedIcons[m.icon_url||'']=true" @error="loadedIcons[m.icon_url||'']=false"></div>
    <div class="resource-main"><a class="resource-title" :href="'/projects/'+p.resource.provider+'/'+p.resource.project_id" @click.prevent="open(p.resource)">{{ p.title }}</a><div class="resource-author">作者 · {{ (m.authors||[]).map(a=>a.name).join('、')||'作者信息待补全' }}</div><p class="resource-summary" :data-project="identity">{{ p.localized_summary||p.summary }}</p><div class="tag-list"><span v-for="(tag,i) in [labels[p.kind],...(m.tags||[]).slice(0,4),...(m.loaders||[])]" :key="i" class="tag">{{ tags[tag]||tag }}</span></div><div class="resource-bottom"><span>{{ p.resource.provider==='modrinth'?'Modrinth':'CurseForge' }}</span><span class="translation-badge">{{ p.badge || (p.update?.code==='not_required'?'无需翻译，保留原文':p.translation_state==='ready'?language+' · 简介与正文已就绪':p.update?.notice||'自动准备'+language+'译文') }}</span></div></div>
    <div class="resource-info"><div v-for="[label,value] in information(p)" :key="label"><span>{{ label }}</span><span>{{ value }}</span></div></div>
  </article>
</template>
