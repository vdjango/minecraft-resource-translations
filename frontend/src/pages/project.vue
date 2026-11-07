<script setup lang="ts">
defineProps<{seed:Record<string,string>}>();
</script>

<template>

<main id="project-page" :data-detail="seed.detail"><p><a href="/">← 返回译文库</a></p><header><div class="eyebrow">MINECRAFT RESOURCE TRANSLATIONS</div><h1>{{ seed.title }}</h1><p id="project-summary" class="project-summary">{{ seed.summary }}</p><div class="tag-list" v-html="seed.tags"></div></header><div class="detail-actions"><label>内容语言 <select id="project-language" v-html="seed.options"></select></label><a :href="seed.sourceUrl" target="_blank" rel="noopener noreferrer">访问原项目 ↗</a><button class="secondary" id="project-retry">重新读取译文</button><span id="project-status" role="status">{{ seed.status }}</span></div><div class="detail-layout"><article class="detail-content"><h2>项目介绍</h2><div id="project-body" class="prose" v-html="seed.body"></div><details><summary>对照原文</summary><div class="prose" v-html="seed.original"></div></details><div v-html="seed.releases"></div></article><aside class="project-sidebar"><h2>项目信息</h2><dl v-html="seed.info"></dl></aside></div></main><footer><span>Buggcraft Translate</span><span>AI 译文可能有误，请以作者原文为准。项目版权归原作者所有。</span></footer>
</template>
