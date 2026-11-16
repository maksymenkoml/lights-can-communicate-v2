const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const code=fs.readFileSync(path.join(__dirname,'../experiments/setup-studio/static/app.js'),'utf8');
function saved(){return {version:3,gridStepCm:5,room:'Office',desk:{width:160,height:85},monitorInches:32,devices:[
 ['govee-left',20,60,65],['govee-right',180,60,65],['hue-monitor',100,110,100],
 ['play-left',70,40,100],['play-center',100,40,50],['play-right',130,40,100]
].map(([id,x,height,scale])=>({id,positionCm:{x,height},scale,orientation:'horizontal'}))};}
const response=(status,body)=>({status,ok:status>=200&&status<300,json:async()=>structuredClone(body)});
async function settle(){for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));}
const discovered=(overrides={})=>({id:'device-'+ 'a'.repeat(32),provider:'govee',name:'Third <lamp>',model:'H6076',shape:'floor',zones:7,supported:true,online:true,selectable:true,ambiguous:false,layout_id:null,height_cm:136,width_cm:null,...overrides});
async function harness({remote=saved(),conflict,postHandler,storage=new Map(),discovery={connected:true,devices:[discovered()],providers:{govee:'ready',hue:'ready'}}}={}){
 const nodes=new Map(),timers=new Map(),posts=[],events=new Map();let timer=0,gets=0;
 const body={tagName:'BODY'},documentModel={activeElement:body};
 const node=key=>{
  if(!nodes.has(key)){
   let html='',controls=[];
   const element={textContent:'',hidden:false,classList:{add(){},remove(){}},setAttribute(){},addEventListener(){},
    focus(){documentModel.activeElement=this;},contains:control=>controls.includes(control),
    querySelector(selector){const match=selector.match(/^\[data-(discovery|action)="([^"]+)"\](?::not\(:disabled\))?$/);return match?controls.find(control=>control.dataset[match[1]]===match[2]&&(!selector.includes(':not')||!control.disabled))||null:null;},
    showModal(){this.open=true;},close(){this.open=false;}};
   Object.defineProperty(element,'innerHTML',{get:()=>html,set:value=>{
    if(controls.includes(documentModel.activeElement))documentModel.activeElement=body;
    html=value;controls=[...value.matchAll(/<(input|button)\b([^>]*)>/g)].flatMap(([,tag,attrs])=>{
     const match=attrs.match(/data-(discovery|action)="([^"]+)"/);if(!match)return [];
     return [{tagName:tag.toUpperCase(),dataset:{[match[1]]:match[2]},disabled:/\bdisabled\b/.test(attrs),checked:/\bchecked\b/.test(attrs),focus(){if(!this.disabled)documentModel.activeElement=this;}}];
    });
   }});nodes.set(key,element);
  }
  return nodes.get(key);
 };
 documentModel.querySelector=node;documentModel.addEventListener=(name,fn)=>{if(!events.has(name))events.set(name,[]);events.get(name).push(fn);};
 const context=vm.createContext({structuredClone,document:documentModel,
  localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  setTimeout:(fn,delay)=>{timers.set(++timer,{fn,delay});return timer;},clearTimeout:id=>timers.delete(id),
  fetch:async(url,options)=>{
   if(url==='/api/discovery'){if(discovery instanceof Error)throw discovery;return response(200,await (typeof discovery==='function'?discovery():discovery));}
   assert.equal(url,'/api/layout');
   if(!options)return response(200,{layout:gets++&&conflict?conflict:remote,revision:gets>1?'r2':'r1'});
   const payload=JSON.parse(options.body);posts.push(payload);
   if(postHandler)return postHandler(payload,posts.length);
   if(conflict&&posts.length===1)return response(409,{error:'layout changed'});
   return response(200,{layout:payload,revision:'r3'});
  }});
 vm.runInContext(code,context);await settle();
 return {posts,storage,nodes,active:()=>documentModel.activeElement,run:source=>vm.runInContext(source,context),event:(name,target)=>{for(const fn of events.get(name)||[])fn({target});},async poll(){
  for(const [id,item]of [...timers])if(item.delay===3000){timers.delete(id);item.fn();}await settle();
 },async flush(){
  for(const [id,item]of [...timers])if(item.delay===300){timers.delete(id);item.fn();}await settle();
 }};
}
test('server layout is authoritative on ordinary reload',async()=>{
 const data=saved();data.devices[0].positionCm.x=50;
 const app=await harness({remote:data});await app.flush();
 assert.equal(app.run('layout.devices[0].pos.x'),25);assert.equal(app.posts.length,0);
});
test('409 merges a local brightness edit with remote geometry instead of overwriting it',async()=>{
 const remote=saved();remote.devices[0].positionCm.x=50;
 const app=await harness({conflict:remote});
 app.run('layout.devices[0].scale=40;dirty();');await app.flush();
 assert.equal(app.posts.length,2);
 assert.equal(app.posts[1].devices[0].scale,40);
 assert.equal(app.posts[1].devices[0].positionCm.x,50);
 assert.equal(app.run('layout.devices[0].pos.x'),25);
});
test('autosave serializes requests and coalesces edits made during an in-flight save',async()=>{
 let release;
 const app=await harness({postHandler:(body,count)=>count===1?new Promise(resolve=>release=()=>resolve(response(200,{layout:body,revision:'r2'}))):response(200,{layout:body,revision:'r3'})});
 app.run('layout.devices[0].scale=40;dirty();');await app.flush();
 app.run('layout.devices[0].scale=50;dirty();layout.devices[0].scale=60;dirty();');await app.flush();
 assert.equal(app.posts.length,1);release();await settle();
 assert.deepEqual(app.posts.map(x=>x.devices[0].scale),[40,60]);
 assert.equal(app.storage.has('light-studio-grid-v3-pending'),false);
});
test('offline draft reload merges against its persisted base',async()=>{
 const base=saved(),draft=saved(),remote=saved();draft.devices[0].scale=40;remote.devices[0].positionCm.x=50;
 const storage=new Map([['light-studio-grid-v3',JSON.stringify(draft)],
  ['light-studio-grid-v3-base',JSON.stringify(base)],['light-studio-grid-v3-pending','true']]);
 const app=await harness({remote,storage});await app.flush();
 assert.equal(app.posts[0].devices[0].scale,40);assert.equal(app.posts[0].devices[0].positionCm.x,50);
});
test('undo keeps remote geometry merged during a conflict',async()=>{
 const remote=saved();remote.devices[0].positionCm.x=50;
 const app=await harness({conflict:remote});
 app.run('remember();layout.devices[0].scale=40;dirty();');await app.flush();
 app.run('layout=history.pop();dirty();');await app.flush();
 assert.equal(app.posts.at(-1).devices[0].scale,65);
 assert.equal(app.posts.at(-1).devices[0].positionCm.x,50);
});
test('real placement metadata round-trips without changing desk or manual Play orientation',async()=>{
 const remote=saved();remote.devices[3].orientation='vertical';
 remote.devices.push({id:discovered().id,name:'Third <lamp>',model:'H6076',shape:'floor',zones:7,scale:80,orientation:'horizontal',positionCm:{x:100,height:60}});
 const app=await harness({remote});app.run('layout.devices[6].scale=55;dirty();');await app.flush();
 assert.deepEqual(app.posts[0].devices[6],{...remote.devices[6],scale:55});
 assert.deepEqual(app.posts[0].desk,{width:160,height:85});assert.equal(app.posts[0].devices[3].orientation,'vertical');
 app.run(`selected='${discovered().id}';actions.remove();`);await app.flush();assert.equal(app.posts.at(-1).devices.length,6);
});
test('discovery picker escapes labels and adds selected real lamps once with autosave',async()=>{
 const app=await harness();app.run('add()');await settle();
 assert.match(app.nodes.get('#modal-content').innerHTML,/Third &lt;lamp&gt;/);
 app.event('change',{dataset:{discovery:discovered().id},checked:true});
 app.run('addSelected();addSelected();');await app.flush();
 assert.equal(app.posts.at(-1).devices.length,7);
 assert.deepEqual(app.posts.at(-1).devices[6],{id:discovered().id,name:'Third <lamp>',model:'H6076',shape:'floor',zones:7,scale:80,orientation:'horizontal',positionCm:{x:100,height:60}});
 assert.match(app.nodes.get('#selection-panel').innerHTML,/не отримує кольори/);
});
test('picker refresh preserves choices and disables existing offline ambiguous and unsupported lamps',async()=>{
 let count=0;const app=await harness({discovery:()=>({connected:true,providers:{govee:'ready',hue:'ready'},devices:[discovered(),discovered({id:'device-'+'b'.repeat(32),layout_id:'govee-left'}),discovered({id:'device-'+'c'.repeat(32),online:false,selectable:false}),discovered({id:'device-'+'d'.repeat(32),ambiguous:true,selectable:false}),discovered({id:'device-'+'e'.repeat(32),supported:false,selectable:false}),...(count++?[discovered({id:'device-'+'f'.repeat(32),name:'Arrived'})]:[])]})});
 app.run('add()');await settle();app.event('change',{dataset:{discovery:discovered().id},checked:true});await app.poll();
 const html=app.nodes.get('#modal-content').innerHTML;
 assert.match(html,/checked/);assert.match(html,/Arrived/);assert.match(html,/Уже на макеті/);assert.match(html,/Не в мережі/);assert.match(html,/Неоднозначна/);assert.match(html,/Не підтримується/);
 app.run('addSelected()');await app.flush();assert.equal(app.posts.at(-1).devices.length,7);
});
test('discovery failure does not discard local layout edits',async()=>{
 const app=await harness({discovery:new Error('unavailable')});app.run('layout.devices[0].scale=45;dirty();add();');await settle();await app.flush();
 assert.equal(app.posts.at(-1).devices[0].scale,45);assert.match(app.nodes.get('#modal-content').innerHTML,/недоступний/);
});
test('reopening picker during an earlier request still refreshes the new session',async()=>{
 let release,count=0;
 const app=await harness({discovery:()=>++count===1?new Promise(resolve=>release=resolve):{connected:true,devices:[discovered({name:'Fresh lamp'})],providers:{govee:'ready',hue:'ready'}}});
 app.run('add();closeDiscovery();add();');await settle();
 assert.match(app.nodes.get('#modal-content').innerHTML,/Fresh lamp/);
 release({connected:true,devices:[discovered({name:'Stale lamp'})],providers:{govee:'ready',hue:'ready'}});await settle();
 assert.doesNotMatch(app.nodes.get('#modal-content').innerHTML,/Stale lamp/);
});
test('adding selected lamps respects capacity and field-level conflicts preserve real bindings',async()=>{
 const remote=saved();for(let i=0;i<5;i++)remote.devices.push({id:'virtual-'+i,name:'Drawing',shape:'bar',scale:80,orientation:'horizontal',positionCm:{x:100,height:40}});
 const second=discovered({id:'device-'+'b'.repeat(32),shape:'bar',model:'440400982841',zones:1});
 const conflict=structuredClone(remote);conflict.devices[0].positionCm.x=50;
 const app=await harness({remote,conflict,discovery:{connected:true,providers:{govee:'ready',hue:'ready'},devices:[discovered(),second]}});
 app.run('add()');await settle();for(const id of [discovered().id,second.id])app.event('change',{dataset:{discovery:id},checked:true});
 app.run('addSelected()');assert.equal(app.run('layout.devices.length'),11);
 app.event('change',{dataset:{discovery:second.id},checked:false});app.run('addSelected()');await app.flush();
 assert.equal(app.posts.at(-1).devices.length,12);assert.equal(app.posts.at(-1).devices[0].positionCm.x,50);
 assert.equal(app.posts.at(-1).devices.at(-1).model,'H6076');
});
test('picker distinguishes searching and unconfigured providers from unavailable or stale providers',async()=>{
 let state='discovering';
 const app=await harness({discovery:()=>({connected:true,devices:[],providers:{govee:state,hue:'not_configured'}})});
 app.run('add()');await settle();
 const status=()=>app.nodes.get('#modal-content').innerHTML.match(/role="status">([^<]*)</)[1];
 assert.match(status(),/Шукаємо/);assert.doesNotMatch(status(),/недоступна/);
 state='not_configured';await app.poll();assert.doesNotMatch(status(),/недоступна|Шукаємо/);
 for(state of ['unavailable','stale']){await app.poll();assert.match(status(),/недоступна/);}
});
test('picker polling preserves keyboard focus on the same checkbox and Add selected control',async()=>{
 const app=await harness();app.run('add()');await settle();const content=app.nodes.get('#modal-content');
 const checkbox=content.querySelector(`[data-discovery="${discovered().id}"]`);checkbox.focus();await app.poll();
 assert.notEqual(app.active(),checkbox);assert.equal(app.active().dataset?.discovery,discovered().id);
 const updated=app.active();updated.checked=true;app.event('change',updated);
 assert.equal(app.active().dataset?.discovery,discovered().id);
 const add=content.querySelector('[data-action="add-selected"]');assert.equal(add.disabled,false);add.focus();await app.poll();
 assert.notEqual(app.active(),add);assert.equal(app.active().dataset?.action,'add-selected');
});
test('picker polling never steals outside focus or restores a removed or disabled control',async()=>{
 let devices=[discovered()];const app=await harness({discovery:()=>({connected:true,devices,providers:{govee:'ready',hue:'ready'}})});
 app.run('add()');await settle();const content=app.nodes.get('#modal-content');
 const outside=app.nodes.get('#stage');outside.focus();await app.poll();assert.equal(app.active(),outside);
 content.querySelector(`[data-discovery="${discovered().id}"]`).focus();devices=[];await app.poll();assert.equal(app.active().tagName,'BODY');
 devices=[discovered()];await app.poll();const checkbox=content.querySelector(`[data-discovery="${discovered().id}"]`);checkbox.focus();devices=[discovered({online:false,selectable:false})];await app.poll();assert.equal(app.active().tagName,'BODY');
 devices=[discovered()];await app.poll();app.event('change',{dataset:{discovery:discovered().id},checked:true});content.querySelector('[data-action="add-selected"]').focus();devices=[];await app.poll();assert.equal(app.active().tagName,'BODY');
});
