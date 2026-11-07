// Vue / Pinia owns cloud settings, observations and actions.
function qiniuSettings(){return cloudStore.payload();}
function renderCloud(cloud,settings,fillSettings,observation){cloudStore.hydrate(cloud,settings,fillSettings,observation);}
