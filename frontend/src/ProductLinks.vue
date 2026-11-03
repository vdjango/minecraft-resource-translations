<script setup lang="ts">
import {productLinks} from './publicSeo';
</script>
<template><footer class="product-footer" aria-label="Buggcraft 产品服务"><strong>Buggcraft · BUGG启动器</strong><nav aria-label="产品服务"><a v-for="link in productLinks" :key="link.href" :href="link.href">{{link.label}}</a></nav></footer></template>
<style scoped>.product-footer{max-width:1400px;margin:2rem auto;padding:1.25rem 24px;border-top:1px solid var(--border,#3d3552);display:flex;gap:1rem;justify-content:space-between;flex-wrap:wrap;font-size:.875rem}.product-footer nav{display:flex;gap:1.25rem;flex-wrap:wrap}.product-footer a{color:inherit;text-decoration:none}.product-footer a:hover{text-decoration:underline}</style>
