<template>
<section id="settings-room-cache" role="tabpanel" aria-labelledby="tab-room-cache" class="admin-section">
  <div class="section-heading"><div><div class="section-caption">ROOM FILE ACCELERATION</div><h2>房间临时下载加速</h2></div><span class="badge">先传信息 · 按需缓存</span></div>
  <div class="admin-card">
    <p>正常可下载的文件直接使用公开来源。源站只对无法获取或持续过慢的文件请求房主补传，不上传整个整合包。下载公开，无需账号认证；请勿共享敏感或不希望公开的文件。</p>
    <p class="muted small">玩家无需管理缓存或关注清理时间。启动器只说明临时加速与通常 8 小时保留，不展示上传客户端、到期提醒或清理倒计时；到期后自动重新选择正常来源，仍有实际缺失时再按需缓存。</p>
    <p id="room-cache-state" role="status" class="muted small"></p>
    <div class="mirror-settings-grid">
      <label class="full"><span><input id="room-cache-enabled" type="checkbox"> 启用房间客户端文件兜底</span></label>
      <label>每次缓存需求保留时间（小时）<input id="room-cache-ttl-hours" type="number" min="1" max="32" required><small>默认 8 小时，最多 32 小时；跨房间共用一份文件，全部需求到期才清理，轮询不会续期。</small></label>
      <label>临时容量上限（MiB）<input id="room-cache-quota-mib" type="number" min="1" max="1048576" required></label>
      <label>单个文件上限（MiB）<input id="room-cache-max-file-mib" type="number" min="1" max="2048" required></label>
      <label>慢速阈值（KiB/s）<input id="room-cache-slow-kibps" type="number" min="1" max="102400" required><small>默认 100，连续两次慢速采样才触发。</small></label>
      <label class="full"><span><input id="room-cache-probe-enabled" type="checkbox"> 在后台限量采样公开来源</span></label>
      <label>同一来源采样间隔（小时）<input id="room-cache-probe-hours" type="number" min="1" max="168" required></label>
      <label>每日采样预算（MiB）<input id="room-cache-probe-daily-mib" type="number" min="1" max="1024" required></label>
    </div>
    <p class="muted small">此处保留时间只适用于房间客户端的模组、资源包与光影。游戏核心、Java、依赖库和加载器沿用各自策略。未知来源先直下，请求计划时不等待测速；已有下载不因后续测速变化而中断。</p>
  </div>
  <div id="room-cache-metrics" class="metrics"></div>
  <div class="admin-card"><h3>最近文件决策</h3><div class="table-scroll"><table><thead><tr><th>文件</th><th>大小</th><th>处理原因</th><th>更新时间</th></tr></thead><tbody id="room-cache-files"></tbody></table></div><p class="muted small">只接收节点授权且清单匹配的文件；关闭共享后不再签发新授权，已签授权最多保留 5 分钟。</p></div>
  <div class="admin-card"><h3>公开来源观测</h3><div class="table-scroll"><table><thead><tr><th>来源</th><th>采样速度</th><th>连续慢速 / 样本</th><th>采样时间</th></tr></thead><tbody id="room-cache-sources"></tbody></table></div><p class="muted small">源站网络观测不代表所有玩家速度。Modrinth、MCIM 不参与自动采样和房间直下候选。</p></div>
</section>
</template>
