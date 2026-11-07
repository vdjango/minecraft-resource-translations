export const adminSections = [
  {id:'overview',label:'总览',href:'/admin',items:[
    {href:'/admin',label:'总览'},
  ]},
  {id:'mirror',label:'镜像资源',href:'/admin/mirror/resources',items:[
    {href:'/admin/mirror/resources',label:'缓存资源'},
    {href:'/admin/mirror/sources',label:'资源索引'},
    {href:'/admin/mirror/tasks',label:'自动任务'},
  ]},
  {id:'translation',label:'翻译服务',href:'/admin/tasks',items:[
    {href:'/admin/tasks',label:'翻译任务'},
    {href:'/admin/feedback',label:'译文反馈'},
  ]},
  {id:'network',label:'节点与接入',href:'/admin/nodes',items:[
    {href:'/admin/nodes',label:'节点管理'},
    {href:'/admin/clients',label:'客户端密钥'},
  ]},
  {id:'settings',label:'系统设置',href:'/admin/mirror/settings',items:[
    {href:'/admin/mirror/settings',label:'镜像设置'},
    {href:'/admin/settings',label:'翻译配置'},
    {href:'/admin/mail',label:'邮件通知'},
  ]},
] as const;

export const mirrorSettingsTabs = [
  ['downloads','下载与来源'],
  ['cloud','存储与容量'],
  ['storage','同步与分流'],
  ['room-cache','客户端加速'],
  ['cloudflare','域名与 CDN'],
] as const;

export function adminSectionFor(path:string) {
  if(['/admin/mirror','/admin/traffic','/admin/usage'].includes(path))return 'overview';
  return adminSections.find(section=>section.items.some(item=>adminItemActive(path,item.href)))?.id??'overview';
}

export function adminItemActive(path:string,href:string) {
  return path===href||(href==='/admin/mirror/settings'&&path.startsWith(href+'/'));
}
