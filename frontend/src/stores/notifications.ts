import {defineStore} from 'pinia';
import {ref} from 'vue';

export const useNotificationsStore=defineStore('notifications',()=>{
  const items=ref<{id:number;message:string;error:boolean}[]>([]);
  let sequence=0;
  function dismiss(id:number){items.value=items.value.filter(item=>item.id!==id);}
  function notify(message:string,error=false){
    if(!message)return;
    const duplicate=items.value.find(item=>item.message===message&&item.error===error);
    if(duplicate)dismiss(duplicate.id);
    const id=++sequence;
    items.value=[...items.value.slice(-2),{id,message,error}];
    window.setTimeout(()=>dismiss(id),error?10000:5500);
  }
  return {items,notify,dismiss};
});
