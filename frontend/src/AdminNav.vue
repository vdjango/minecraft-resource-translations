<script setup lang="ts">
import {computed} from 'vue';
import {adminItemActive,adminSectionFor,adminSections} from './adminNavigation';
import {usePlatformNavigation} from './usePlatformNavigation';
defineProps<{authenticated:boolean}>();
const {path,navigate}=usePlatformNavigation();
const section=computed(()=>adminSections.find(item=>item.id===adminSectionFor(path.value))!);
</script>
<template>
  <aside v-if="authenticated" class="admin-sidebar" :aria-label="section.label+'导航'">
    <span class="eyebrow">{{section.label}}</span>
    <template v-for="item in section.items" :key="item.href">
      <a :href="item.href" :class="{active:adminItemActive(path,item.href),'active-parent':item.href==='/admin/mirror/settings'&&path.startsWith(item.href+'/')}" :aria-current="adminItemActive(path,item.href)?'page':undefined" @click="navigate($event,item.href)">{{item.label}}</a>
    </template>
    <div class="sidebar-bottom">Buggcraft 管理后台</div>
  </aside>
</template>
