<script setup lang="ts">
import WorkspaceCardHeader from './WorkspaceCardHeader.vue';
import {useSourceQualityStore} from './stores/sourceQuality';
const quality=useSourceQualityStore(),s=quality.settings;
</script>
<template>
<section class="workspace-card">
  <WorkspaceCardHeader title="统一选源与轻量采样" description="普通下载、整合包与房间同步共用质量数据。采样在后台分散运行。"><RouterLink to="/admin/mirror/sources?view=quality">来源表现 →</RouterLink></WorkspaceCardHeader>
  <div class="workspace-form-grid">
    <label class="workspace-check"><input v-model="s.quality_enabled" type="checkbox">启用后台质量采样</label>
    <label class="workspace-check"><input v-model="s.quality_adaptive_cache" type="checkbox">根据质量安排缓存优先级</label>
    <label>慢速阈值 / KiB/s<input v-model.number="s.quality_slow_kibps" type="number" min="1" max="102400"></label>
    <label>每日采样预算 / MiB<input v-model.number="s.quality_daily_mib" type="number" min="1" max="1024"></label>
  </div>
  <details class="workspace-help"><summary>采样周期与长期评分</summary><div class="workspace-form-grid">
    <label>活跃 CurseForge / 分钟<input v-model.number="s.quality_content_minutes" type="number" min="5" max="120"></label>
    <label>备用 CurseForge / 分钟<input v-model.number="s.quality_backup_minutes" type="number" min="5" max="120"></label>
    <label>Modrinth / 分钟<input v-model.number="s.quality_modrinth_minutes" type="number" min="5" max="120"></label>
    <label>其他仓库 / 小时<input v-model.number="s.quality_interval_hours" type="number" min="1" max="168"></label>
    <label>快速参考 / KiB/s<input v-model.number="s.quality_fast_kibps" type="number" min="64" max="102400"></label>
  </div><p class="workspace-subtle">采样时间随机浮动，失败退避。单次 404 不等于整站不可用；至少跨 3 天、8 次观测后形成长期评分。</p></details>
</section>
</template>
