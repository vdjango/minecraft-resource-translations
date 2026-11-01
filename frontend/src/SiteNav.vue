<script setup lang="ts">
import {onMounted,ref} from 'vue';
import links from '../navigation.json';
const light=ref(document.documentElement.dataset.theme==='light');
const path=window.location.pathname;
const admin=path==='/admin'||path.startsWith('/admin/');
const active=(href:string)=>href==='/'?location.pathname==='/'||location.pathname.startsWith('/projects/'):location.pathname===href||location.pathname.startsWith(href+'/');
function toggle(){light.value=!light.value;document.documentElement.dataset.theme=light.value?'light':'dark';try{localStorage.setItem('buggcraft.theme',light.value?'light':'dark');}catch{}}
onMounted(()=>{light.value=document.documentElement.dataset.theme==='light';});
</script>
<template>
<nav class="site-nav" aria-label="主导航">
  <a class="brand" :href="admin?'/admin':'/mirror'"><span class="mark">B</span><span class="brand-type"><strong>BUGGCRAFT</strong><small>{{admin?'管理工作台':'RESOURCE MIRROR & TRANSLATIONS'}}</small></span></a>
  <div v-if="admin" class="nav-links site-desktop-links admin-top-links"><a href="/admin" :class="{'nav-active':path==='/admin'}">统计总览</a><a href="/admin/mirror" :class="{'nav-active':path.startsWith('/admin/mirror')}">镜像资源</a><a href="/admin/tasks" :class="{'nav-active':['/admin/tasks','/admin/usage','/admin/feedback'].includes(path)}">译文与用户</a><a href="/admin/nodes" :class="{'nav-active':path==='/admin/nodes'}">节点网络</a><a href="/admin/mirror/settings" :class="{'nav-active':path.startsWith('/admin/mirror/settings')}">设置</a></div>
  <div v-else class="nav-links site-desktop-links"><a v-for="link in links" :key="link.href" :href="link.href" :class="{'nav-active':active(link.href)}" :aria-current="active(link.href)?'page':undefined">{{link.label}}</a></div>
  <div class="site-nav-tools"><a v-if="admin" class="admin-public-link" href="/">访问网站 ↗</a><button id="theme-toggle" class="theme-toggle" type="button" :aria-label="light?'切换到深色主题':'切换到浅色主题'" :aria-pressed="light" @click="toggle">{{light?'深色模式':'浅色模式'}}</button><details class="site-mobile-menu"><summary aria-label="打开导航菜单">菜单 <span aria-hidden="true">☰</span></summary><div><a v-if="admin" href="/admin">统计总览</a><a v-if="admin" href="/admin/mirror">镜像资源</a><a v-if="admin" href="/admin/tasks">译文与用户</a><a v-if="admin" href="/admin/nodes">节点网络</a><a v-if="admin" href="/admin/mirror/settings">设置</a><a v-if="admin" href="/">访问网站</a><a v-for="link in admin?[]:links" :key="link.href" :href="link.href" :aria-current="active(link.href)?'page':undefined">{{link.label}}</a></div></details></div>
</nav>
</template>
