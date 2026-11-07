<script setup lang="ts">
import {useNotificationsStore} from './stores/notifications';
const notices=useNotificationsStore();
</script>
<template>
  <div class="toast-stack" aria-label="操作通知">
    <TransitionGroup name="toast">
      <div v-for="notice in notices.items" :key="notice.id" class="toast-notice" :class="{'toast-error':notice.error}" :role="notice.error?'alert':'status'">
        <span class="toast-icon" aria-hidden="true">{{notice.error?'!':'✓'}}</span>
        <span>{{notice.message}}</span>
        <button type="button" aria-label="关闭通知" @click="notices.dismiss(notice.id)">×</button>
      </div>
    </TransitionGroup>
  </div>
</template>
<style>
.toast-stack{position:fixed;z-index:2000;top:24px;right:24px;width:min(440px,calc(100vw - 32px));display:grid;gap:10px;pointer-events:none}.toast-notice{pointer-events:auto;display:flex;align-items:center;gap:12px;padding:16px 18px;border:1px solid #608b80;border-radius:12px;color:#edfff8;background:#182e2a;box-shadow:0 12px 40px #0006;font-size:14px;line-height:1.6}.toast-notice.toast-error{background:#39232d;border-color:#a85d70;color:#ffe8ec}.toast-icon{font-weight:800;font-size:18px}.toast-notice>span:nth-child(2){flex:1;overflow-wrap:anywhere}.toast-notice button{border:0;background:transparent;color:inherit;padding:0 4px;min-height:26px;font-size:22px}.toast-enter-active,.toast-leave-active{transition:opacity .18s,transform .18s}.toast-enter-from,.toast-leave-to{opacity:0;transform:translateY(-8px)}
</style>
