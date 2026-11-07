<script setup lang="ts">
import {computed} from 'vue';
import {useRoute,useRouter} from 'vue-router';
const props=defineProps<{items:readonly (readonly string[])[];param?:string}>();
const route=useRoute(),router=useRouter();
const key=computed(()=>props.param||'view');
const active=computed(()=>props.items.some(item=>item[0]===route.query[key.value])?String(route.query[key.value]):props.items[0]?.[0]);
function select(value:string){void router.replace({path:route.path,query:{...route.query,[key.value]:value}});}
</script>
<template><div class="workspace-tabs" role="tablist"><button v-for="[value,label] in items" :key="value" type="button" role="tab" :aria-selected="active===value" :class="{active:active===value}" @click="select(value)">{{label}}</button></div><slot :active="active"/></template>
