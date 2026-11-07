<script setup lang="ts">
defineProps<{title:string;description?:string}>();
</script>

<template>
  <div class="workspace-card-header">
    <div class="workspace-card-heading"><h2>{{title}}</h2><p v-if="description">{{description}}</p></div>
    <div v-if="$slots.default" class="workspace-card-actions"><slot/></div>
  </div>
</template>
