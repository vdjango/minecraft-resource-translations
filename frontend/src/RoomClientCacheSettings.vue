<template>
<section id="settings-room-cache" role="tabpanel" aria-labelledby="tab-room-cache" class="admin-section">
  <div class="section-heading"><div><div class="section-caption">ROOM FILE ACCELERATION</div><h2>房间临时下载加速</h2></div><span class="badge">先传信息 · 按需缓存</span></div>
  <div class="admin-card">
    <div class="section-heading"><div><h3>接入联机节点</h3><p class="muted small">生成邀请，复制到节点后台的“资源下载加速”，接入后立即生效，无需修改服务器文件或重启。</p></div><button id="room-node-refresh" type="button" class="secondary">刷新节点</button></div>
    <div class="mirror-settings-grid"><label>节点名称<input id="room-node-name" maxlength="80" placeholder="例如：宿迁联机节点"></label><div><button id="room-node-invite" type="button" class="primary">生成接入邀请</button></div></div>
    <div id="room-node-invitation" hidden><label>一次性邀请 · 10 分钟有效<textarea id="room-node-code" rows="3" readonly spellcheck="false"></textarea></label><button id="room-node-copy" type="button" class="secondary">复制邀请</button><p class="muted small">仅交给节点管理员；邀请只能使用一次，授权密钥自动保存，玩家无需配置。</p></div>
    <p id="room-node-feedback" role="status" class="muted small"></p>
    <div class="table-scroll"><table><thead><tr><th>节点</th><th>接入状态</th><th>管理</th></tr></thead><tbody id="room-node-list"></tbody></table></div>
  </div>
  <div class="admin-card">
    <p>正常可下载的文件直接使用公开来源。源站只对无法获取或持续过慢的文件请求房主补传，不上传整个整合包。下载公开，无需账号认证；请勿共享敏感或不希望公开的文件。</p>
    <p class="muted small">玩家无需管理缓存或关注清理时间。启动器只说明临时加速与通常 8 小时保留，不展示上传客户端、到期提醒或清理倒计时；到期后自动重新选择正常来源，仍有实际缺失时再按需缓存。</p>

    <div class="mirror-settings-grid">
      <label class="full"><span><input id="room-cache-enabled" type="checkbox"> 启用房间客户端文件兜底</span></label>
      <label>每次缓存需求保留时间（小时）<input id="room-cache-ttl-hours" type="number" min="1" max="32" required><small>默认 8 小时，最多 32 小时；跨房间共用一份文件，全部需求到期才清理，轮询不会续期。</small></label>
      <label>临时容量上限（GiB）<input id="room-cache-quota-gib" type="number" min="0.1" max="1024" step="0.1" required><small>仅限制房间客户端临时文件，不占用普通镜像缓存配额。</small></label>
      <label>单个文件上限（MiB）<input id="room-cache-max-file-mib" type="number" min="1" max="2048" required></label>
      <label>慢速阈值（KiB/s）<input id="room-cache-slow-kibps" type="number" min="1" max="102400" required><small>默认 100，连续两次慢速采样才触发。</small></label>
      <label class="full"><span><input id="room-cache-probe-enabled" type="checkbox"> 在后台限量采样公开来源</span></label>
      <label>同一来源采样间隔（小时）<input id="room-cache-probe-hours" type="number" min="1" max="168" required></label>
      <label>每日采样预算（MiB）<input id="room-cache-probe-daily-mib" type="number" min="1" max="1024" required></label>
    </div>
    <p class="muted small">此处保留时间只适用于房间客户端的模组、资源包与光影。游戏核心、Java、依赖库和加载器沿用各自策略。未知来源先直下，请求计划时不等待测速；已有下载不因后续测速变化而中断。</p>
  </div>
  <p><a href="/admin/mirror/temporary">查看临时文件、状态与空间占用 →</a></p>
</section>
</template>
