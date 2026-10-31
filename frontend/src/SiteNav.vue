<script setup lang="ts">
import {onMounted,ref} from 'vue';
import links from '../navigation.json';
const light=ref(document.documentElement.dataset.theme==='light');
const active=(href:string)=>href==='/'?location.pathname==='/'||location.pathname.startsWith('/projects/'):location.pathname===href||location.pathname.startsWith(href+'/');
function toggle(){light.value=!light.value;document.documentElement.dataset.theme=light.value?'light':'dark';try{localStorage.setItem('buggcraft.theme',light.value?'light':'dark');}catch{}}
onMounted(()=>{light.value=document.documentElement.dataset.theme==='light';});
</script>
<template>
<nav class="site-nav" aria-label="主导航">
  <a class="brand" href="/mirror"><span class="mark">B</span><span class="brand-type"><strong>BUGGCRAFT</strong><small>RESOURCE MIRROR & TRANSLATIONS</small></span></a>
  <div class="nav-links site-desktop-links"><a v-for="link in links" :key="link.href" :href="link.href" :class="{'nav-active':active(link.href)}" :aria-current="active(link.href)?'page':undefined">{{link.label}}</a></div>
  <div class="site-nav-tools"><button id="theme-toggle" class="theme-toggle" type="button" :aria-label="light?'切换到深色主题':'切换到浅色主题'" :aria-pressed="light" @click="toggle">{{light?'深色模式':'浅色模式'}}</button><details class="site-mobile-menu"><summary aria-label="打开导航菜单">菜单 <span aria-hidden="true">☰</span></summary><div><a v-for="link in links" :key="link.href" :href="link.href" :aria-current="active(link.href)?'page':undefined">{{link.label}}</a></div></details></div>
</nav>
</template>
