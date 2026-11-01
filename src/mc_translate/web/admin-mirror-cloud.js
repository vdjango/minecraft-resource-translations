// Cloud configuration shares the mirror settings form, while copies are observed on the task and resource pages.
const cloudKinds = [
  ['core', '游戏核心'], ['loaders', '加载器'], ['mods', '模组'],
  ['java', 'Java 运行时'], ['libraries', '依赖库'], ['assets', '原版资源'],
  ['room_mods', '客户端 · 模组'], ['room_resourcepacks', '客户端 · 资源包'],
  ['room_shaderpacks', '客户端 · 光影']
];
const cloudModes = [['local', '仅本地'], ['copy', '本地 + 七牛副本'], ['cloud', '七牛优先']];
const cloudStates = {queued:'排队中', uploading:'上传并校验', finished:'已完成', failed:'失败',
  waiting_capacity:'空间预算已满', waiting_configuration:'等待配置'};

function cloudField(label, key, value, type='text') {
  const wrapper = document.createElement('label');
  wrapper.textContent = label;
  const input = document.createElement('input');
  input.dataset.field = key;
  input.type = type;
  if(type === 'checkbox') input.checked = Boolean(value);
  else input.value = value == null ? '' : String(value);
  if(type === 'number') { input.min = '1'; input.max = '1000000'; }
  wrapper.append(input);
  return wrapper;
}

function cloudProfileCard(profile) {
  const card = document.createElement('article');
  card.className = 'cloud-profile-card';
  const head = document.createElement('div');
  head.className = 'section-heading';
  const name = sourceElement('strong', profile.id || '新空间');
  const remove = sourceElement('button', '移除空间');
  remove.type = 'button'; remove.className = 'secondary';
  remove.onclick = () => { card.remove(); refreshCloudProfileOptions(); };
  head.append(name, remove);
  const grid = document.createElement('div');
  grid.className = 'cloud-profile-fields';
  grid.append(
    cloudField('空间标识', 'id', profile.id),
    cloudField('Bucket 名称', 'bucket', profile.bucket),
    cloudField('HTTPS 下载域名', 'domain', profile.domain, 'url'),
    cloudField('统一前缀', 'prefix', profile.prefix || 'buggcraft-mirror'),
    cloudField('七牛上传域名', 'upload_host', profile.upload_host || 'https://up-z0.qiniup.com', 'url'),
    cloudField('本站托管预算 / GiB', 'quota_gib', profile.quota_gib || 10, 'number'),
    cloudField('每日上传预算 / GiB', 'daily_upload_gib', profile.daily_upload_gib || 1, 'number'),
    cloudField('私有空间（生成限时签名 URL）', 'private', profile.private, 'checkbox')
  );
  card.append(head, grid);
  grid.querySelector('[data-field="id"]').addEventListener('input', event => {
    name.textContent = event.target.value || '新空间';
    refreshCloudProfileOptions();
  });
  return card;
}

function readCloudProfiles() {
  return [...$('cloud-profiles').querySelectorAll('.cloud-profile-card')].map(card => {
    const values = {};
    for(const input of card.querySelectorAll('[data-field]')) {
      values[input.dataset.field] = input.type === 'checkbox' ? input.checked :
        input.type === 'number' ? Number(input.value) : input.value.trim();
    }
    return values;
  });
}

function readCloudRoutes() {
  return Object.fromEntries([...$('cloud-routes').querySelectorAll('tr[data-route]')].map(row =>
    [row.dataset.route, Object.fromEntries([...row.querySelectorAll('[data-field]')].map(input =>
      [input.dataset.field, input.value.trim()]))]));
}

function refreshCloudProfileOptions() {
  const names = readCloudProfiles().map(profile => profile.id).filter(Boolean);
  for(const select of $('cloud-routes').querySelectorAll('select[data-field="profile"]')) {
    const selected = select.value;
    select.replaceChildren(new Option('不指定', ''), ...names.map(id => new Option(id, id)));
    select.value = names.includes(selected) ? selected : '';
  }
}

function renderCloudRoutes(routes) {
  const rows = cloudKinds.map(([key, label]) => {
    const route = routes[key] || {mode:'local', profile:'', path:key};
    const row = document.createElement('tr'); row.dataset.route = key;
    const mode = document.createElement('select'); mode.dataset.field = 'mode';
    mode.replaceChildren(...cloudModes.map(([value, text]) => new Option(text, value)));
    mode.value = route.mode;
    const profile = document.createElement('select'); profile.dataset.field = 'profile';
    const path = document.createElement('input'); path.dataset.field = 'path'; path.value = route.path || key;
    path.maxLength = 80; path.placeholder = key;
    mode.onchange = () => { profile.disabled = mode.value === 'local'; };
    row.append(sourceElement('td', label));
    for(const input of [mode, profile, path]) { const cell = document.createElement('td'); cell.append(input); row.append(cell); }
    return row;
  });
  $('cloud-routes').replaceChildren(...rows);
  refreshCloudProfileOptions();
  for(const row of rows) {
    const route = routes[row.dataset.route] || {mode:'local', profile:'', path:row.dataset.route};
    row.querySelector('[data-field="profile"]').value = route.profile || '';
    row.querySelector('[data-field="profile"]').disabled = route.mode === 'local';
  }
  const migrate = $('cloud-migrate-route');
  migrate.replaceChildren(...cloudKinds.map(([key, label]) => new Option(label, key)));
}

function qiniuSettings() {
  const routes = readCloudRoutes();
  for(const route of Object.values(routes)) if(route.mode === 'local') route.profile = '';
  return {qiniu_profiles:readCloudProfiles(), qiniu_routes:routes,
    qiniu_paused:$('cloud-paused').checked,
    qiniu_signed_url_minutes:Number($('cloud-signed-minutes').value)};
}

function cloudActionButton(text, action, profile, enabled) {
  const button = sourceElement('button', text);
  button.type = 'button'; button.className = 'secondary'; button.disabled = !enabled;
  button.onclick = async () => {
    button.disabled = true;
    try {
      const result = await api('v1/admin/mirror', {action, profile});
      if(!result.ok) throw Error('验证未通过，请检查空间、域名和凭据。');
      message(action === 'cloud_test' ? '32 字节写入、下载及清理验证通过。' : '七牛空间查询通过。');
      await loadMirror();
    } catch(error) { showError(error); button.disabled = false; }
  };
  return button;
}

function renderCloud(cloud, settings, fillSettings) {
  if(fillSettings) {
    $('cloud-profiles').replaceChildren(...(settings.qiniu_profiles || []).map(cloudProfileCard));
    renderCloudRoutes(settings.qiniu_routes || {});
    $('cloud-paused').checked = settings.qiniu_paused !== false;
    $('cloud-signed-minutes').value = settings.qiniu_signed_url_minutes || 240;
  }
  const profiles = cloud.profiles || [];
  const copies = profiles.reduce((sum, profile) => sum + (profile.files || 0), 0);
  const bytes = profiles.reduce((sum, profile) => sum + (profile.bytes || 0), 0);
  $('cloud-overview').textContent = profiles.length ?
    profiles.length + ' 个空间 · ' + number(copies) + ' 个已核验副本 · ' + mirrorBytes(bytes) +
      (cloud.paused ? ' · 新上传暂停' : ' · 后台上传运行中') : '尚未配置七牛空间；资源继续走现有本地与上游策略。';
  $('cloud-task-state').textContent = profiles.length ?
    (cloud.paused ? '新上传已暂停' : '后台上传已启用') +
      (cloud.active ? ' · 正在处理任务 #' + cloud.active + '（' + mirrorBytes(cloud.progress) + '）' : ' · 当前无正在上传的文件') :
    '尚无云端空间；不会上传文件。';
  const status = $('cloud-config-state'); status.replaceChildren();
  if(!profiles.length) status.textContent = '尚未添加空间。保存配置后，可以进行轻量联通验证。';
  for(const profile of profiles) {
    const line = document.createElement('div'); line.className = 'cloud-profile-status';
    line.append(sourceElement('span', profile.id + ' · ' + profile.bucket + ' · ' +
      (profile.configured ? '密钥可用' : '未配置密钥') + ' · ' +
      (profile.health ? (profile.health.healthy ? '下载域名正常' : '下载域名异常，回退本地或上游') : '下载域名待探测') +
      ' · ' + number(profile.files) + ' 个副本 / ' + mirrorBytes(profile.bytes)));
    line.append(cloudActionButton('检查连接', 'cloud_probe', profile.id, profile.configured),
      cloudActionButton('32 字节读写测试', 'cloud_test', profile.id, profile.configured));
    status.append(line);
  }
  const jobs = (cloud.jobs || []).map(job => {
    const row = document.createElement('tr');
    const detail = sourceElement('td', job.route + ' · ' + job.digest.slice(0, 16));
    const state = sourceElement('td', cloudStates[job.state] || job.state);
    if(job.error) state.append(sourceElement('small', ' · ' + job.error));
    const action = sourceElement('td', qualityTime(job.finished || job.started || job.created));
    if(['failed', 'waiting_configuration', 'waiting_capacity'].includes(job.state)) {
      const retry = sourceElement('button', '重试'); retry.type = 'button'; retry.className = 'secondary';
      retry.onclick = async () => {
        retry.disabled = true;
        try { await api('v1/admin/mirror', {action:'cloud_retry', job_id:job.id}); await loadMirror(); }
        catch(error) { showError(error); retry.disabled = false; }
      };
      action.append(retry);
    }
    row.append(detail, sourceElement('td', job.profile), sourceElement('td', mirrorBytes(job.size)), state, action);
    return row;
  });
  $('cloud-jobs').replaceChildren(...(jobs.length ? jobs : [observationRow(['暂无云端复制任务', '—', '—', '—', '—'])]));
}

$('cloud-add-profile').onclick = () => {
  const existing = new Set(readCloudProfiles().map(profile => profile.id));
  let index = 1; while(existing.has('space' + index)) index++;
  $('cloud-profiles').append(cloudProfileCard({id:'space' + index, prefix:'buggcraft-mirror',
    upload_host:'https://up-z0.qiniup.com', quota_gib:10, daily_upload_gib:1}));
  refreshCloudProfileOptions();
};
$('cloud-migrate').onclick = async event => {
  const button = event.currentTarget; button.disabled = true;
  try {
    const result = await api('v1/admin/mirror', {action:'cloud_migrate',
      route:$('cloud-migrate-route').value, limit:Number($('cloud-migrate-limit').value)});
    message('已安排 ' + number(result.queued) + ' 个文件进入云端队列。');
    await loadMirror();
  } catch(error) { showError(error); } finally { button.disabled = false; }
};
