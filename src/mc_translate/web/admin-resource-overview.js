"use strict";
async function loadResourceOverview(){
  const data=await api('v1/admin/mirror');
  const gib=value=>(Number(value||0)/1024**3).toFixed(2)+' GiB';
  $('resource-overview-state').textContent=data.running?'资源下载在线 · 翻译与下载使用同一管理工作台':'资源下载未启动 · 可在平台设置中查看部署与下载配置';
  metrics($('resource-overview-metrics'),[
    ['本地文件',number(data.cached_files),'已完成并校验的缓存'],
    ['缓存空间',gib(data.cached_bytes),'上限 '+data.settings.quota_gib+' GiB'],
    ['资源节点',number((data.nodes||[]).filter(n=>n.enabled&&n.healthy).length),'健康且启用的协作节点'],
    ['公开来源',number((data.settings.sources||[]).filter(s=>s.enabled).length),'文件或目录映射规则']
  ]);
  $('resource-overview-budget').textContent=data.egress?'本月（UTC '+data.egress.period+'）已接受下载 '+gib(data.egress.bytes)+' · '+(data.accepting?'本机可继续提供下载':'优先将下载转移到可用来源'):'资源下载上线后显示出口预算状态。';
}
