<script setup lang="ts">
import AdminOverview from '../AdminOverview.vue';
defineProps<{seed:Record<string,string>;views:any}>();
</script>
<template><AdminOverview :seed="seed" :views="views"/></template>
