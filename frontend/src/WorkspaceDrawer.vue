<script setup lang="ts">
import {onBeforeUnmount,onMounted,ref} from 'vue';
defineProps<{title:string}>();const emit=defineEmits<{close:[]}>();
const panel=ref<HTMLElement|null>(null);let previous:HTMLElement|null=null;
const key=(event:KeyboardEvent)=>{if(event.key==='Escape')emit('close');if(event.key==='Tab'){const nodes=panel.value?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),a[href],summary');if(!nodes?.length)return;const first=nodes[0],last=nodes[nodes.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}}};
onMounted(()=>{previous=document.activeElement as HTMLElement;panel.value?.focus();window.addEventListener('keydown',key);});onBeforeUnmount(()=>{window.removeEventListener('keydown',key);previous?.focus();});
</script>
<template><Teleport to="body"><div class="workspace-drawer-backdrop" @click.self="emit('close')"><section ref="panel" class="workspace-drawer" role="dialog" aria-modal="true" :aria-label="title" tabindex="-1"><header class="workspace-drawer-header"><h2>{{title}}</h2><button type="button" class="secondary" aria-label="关闭详情" @click="emit('close')">✕</button></header><div class="workspace-drawer-content"><slot/></div><footer v-if="$slots.footer" class="workspace-drawer-footer"><slot name="footer"/></footer></section></div></Teleport></template>
