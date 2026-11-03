/** Browser routes served by the platform. Dynamic project pages keep their server-rendered entry. */
export const platformRoutes=[
  {path:'/',entry:'index',title:'Buggcraft · 译文库与镜像源'},
  {path:'/mirror',entry:'mirror',title:'镜像源 · Buggcraft'},
  {path:'/docs',entry:'docs',title:'开发文档 · Buggcraft'},
  {path:'/developers',entry:'developers',title:'开发者接入 · Buggcraft'},
  {path:'/projects/:provider/:projectId',entry:'project',title:'项目译文 · Buggcraft'},
  {path:'/admin',entry:'admin',title:'平台总览 · Buggcraft'},
  {path:'/admin/setup',entry:'admin',title:'设置管理员账户 · Buggcraft'},
  {path:'/admin/traffic',entry:'admin-traffic',title:'访问统计 · Buggcraft'},
  {path:'/admin/settings',entry:'admin-model',title:'翻译配置 · Buggcraft'},
  {path:'/admin/mail',entry:'admin-mail',title:'邮件通知 · Buggcraft'},
  {path:'/admin/clients',entry:'admin-clients',title:'客户端密钥 · Buggcraft'},
  {path:'/admin/tasks',entry:'admin-tasks',title:'翻译任务 · Buggcraft'},
  {path:'/admin/usage',entry:'admin-usage',title:'用量与费用 · Buggcraft'},
  {path:'/admin/nodes',entry:'admin-nodes',title:'服务节点 · Buggcraft'},
  {path:'/admin/feedback',entry:'admin-feedback',title:'译文反馈 · Buggcraft'},
  {path:'/admin/mirror',entry:'admin-mirror',title:'镜像概览 · Buggcraft'},
  {path:'/admin/mirror/resources',entry:'admin-mirror',title:'缓存资源 · Buggcraft'},
  {path:'/admin/mirror/tasks',entry:'admin-mirror',title:'同步任务 · Buggcraft'},
  {path:'/admin/mirror/transfers',entry:'admin-mirror',title:'云端复制与迁移 · Buggcraft'},
  {path:'/admin/mirror/temporary',entry:'admin-mirror',title:'客户端临时缓存 · Buggcraft'},
  {path:'/admin/mirror/sources',entry:'admin-mirror',title:'来源覆盖 · Buggcraft'},
  {path:'/admin/mirror/quality',entry:'admin-mirror',title:'来源质量 · Buggcraft'},
  {path:'/admin/mirror/settings',entry:'admin-mirror',title:'镜像设置 · Buggcraft'},
  ...['downloads','storage','cloud','distribution','sources','quality','room-cache','cloudflare'].map(tab=>({
    path:'/admin/mirror/settings/'+tab,entry:'admin-mirror',title:'镜像设置 · Buggcraft',
  })),
] as const;

export function platformRoute(path:string,mirrorHome=false){
  if(path==='/'&&mirrorHome)return {...platformRoutes[0],entry:'mirror'};
  return platformRoutes.find(route=>route.path===path)||
    (/^\/projects\/(?:modrinth|curseforge)\/[A-Za-z0-9_-]+$/.test(path)?platformRoutes.find(route=>route.entry==='project'):undefined);
}
