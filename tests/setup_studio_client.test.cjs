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
async function harness({remote=saved(),conflict,postHandler,storage=new Map(),pilot={version:1,connected:false,mode:'unknown',sync:{state:'unknown'},lamps:{}},discovery={connected:true,devices:[discovered()],providers:{govee:'ready',hue:'ready'}}}={}){
 const nodes=new Map(),timers=new Map(),posts=[],events=new Map(),pilotRequests=[];let timer=0,gets=0,now=0;
 const body={tagName:'BODY'},documentModel={activeElement:body,hidden:false};
 const matches=(element,selector)=>{
  if(selector.endsWith(':not(:disabled)'))return !element.disabled&&matches(element,selector.slice(0,-15));
  if(selector.startsWith('#'))return element.id===selector.slice(1);
  if(selector.startsWith('.'))return element.classList.contains(selector.slice(1));
  const attr=selector.match(/^\[([^=\]]+)(?:="([^"]+)")?\]$/);
  return !!attr&&element.hasAttribute(attr[1])&&(attr[2]===undefined||element.getAttribute(attr[1])===attr[2]);
 };
 const makeElement=(tag,attrs='')=>{
  const attributes=new Map([...attrs.matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(([,key,value])=>[key,value??''])),listeners=new Map();
  const element={tagName:tag.toUpperCase(),dataset:{},textContent:'',hidden:false,disabled:attributes.has('disabled'),checked:attributes.has('checked'),writes:0,
   getAttribute:key=>attributes.get(key)??null,hasAttribute:key=>attributes.has(key),
   setAttribute(key,value){attributes.set(key,String(value));if(key==='id')this.id=value;if(key.startsWith('data-'))this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(value);},
   classList:{contains:value=>(attributes.get('class')||'').split(/\s+/).includes(value),add(value){attributes.set('class',[...new Set((attributes.get('class')||'').split(/\s+/).concat(value))].join(' '));},remove(value){attributes.set('class',(attributes.get('class')||'').split(/\s+/).filter(x=>x!==value).join(' '));}},
   focus(){if(!this.disabled)documentModel.activeElement=this;},closest(selector){return selector.split(',').some(s=>matches(this,s))?this:null;},
   addEventListener(name,fn){listeners.set(name,fn);},dispatch(name,event){listeners.get(name)?.(event);},
   setPointerCapture(id){this.capture=id;},getScreenCTM:()=>({inverse:()=>({})}),
   showModal(){this.open=true;},close(){this.open=false;}};
  for(const [key,value]of attributes)element.setAttribute(key,value);
  return element;
 };
 const node=key=>{
  if(!nodes.has(key)){
   let html='',controls=[];
   const element=makeElement(key==='#stage'?'svg':'div',key.startsWith('#')?`id="${key.slice(1)}"`:'');
   element.contains=control=>controls.includes(control);
   element.querySelector=selector=>controls.find(control=>matches(control,selector))||null;
   element.querySelectorAll=selector=>controls.filter(control=>matches(control,selector));
   Object.defineProperty(element,'innerHTML',{get:()=>html,set:value=>{
    if(controls.includes(documentModel.activeElement))documentModel.activeElement=body;
    html=value;element.writes++;controls=[...value.matchAll(/<([a-z]+)\b([^>]*)>/g)].map(([,tag,attrs])=>makeElement(tag,attrs));
   }});nodes.set(key,element);
  }
  return nodes.get(key);
 };
 documentModel.querySelector=node;documentModel.addEventListener=(name,fn)=>{if(!events.has(name))events.set(name,[]);events.get(name).push(fn);};
 const context=vm.createContext({structuredClone,document:documentModel,
  localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  AbortController,performance:{now:()=>now},DOMPoint:class{constructor(x,y){this.x=x;this.y=y;}matrixTransform(){return this;}},
  setTimeout:(fn,delay)=>{timers.set(++timer,{fn,delay,at:now+delay});return timer;},clearTimeout:id=>timers.delete(id),
  fetch:async(url,options)=>{
   if(url==='/api/pilot'){pilotRequests.push(options);return response(200,await (typeof pilot==='function'?pilot(options):pilot));}
   if(url==='/api/discovery'){if(discovery instanceof Error)throw discovery;return response(200,await (typeof discovery==='function'?discovery():discovery));}
   assert.equal(url,'/api/layout');
   if(!options)return response(200,{layout:gets++&&conflict?conflict:remote,revision:gets>1?'r2':'r1'});
   const payload=JSON.parse(options.body);posts.push(payload);
   if(postHandler)return postHandler(payload,posts.length);
   if(conflict&&posts.length===1)return response(409,{error:'layout changed'});
   return response(200,{layout:payload,revision:'r3'});
  }});
 vm.runInContext(code,context);await settle();
 return {posts,storage,nodes,pilotRequests,active:()=>documentModel.activeElement,run:source=>vm.runInContext(source,context),event:(name,target,extra={})=>{for(const fn of events.get(name)||[])fn({target,...extra});},
  async visible(visible){documentModel.hidden=!visible;for(const fn of events.get('visibilitychange')||[])fn();await settle();},
  async wake(ms,delay){now+=ms;for(const [id,item]of [...timers])if(item.delay===delay&&item.at<=now){timers.delete(id);item.fn();}await settle();},
  async advance(ms){const end=now+ms;while(true){const next=[...timers].filter(([,item])=>item.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;const [id,item]=next;now=item.at;timers.delete(id);item.fn();await settle();}now=end;await settle();},async poll(){
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
test('Sync visibility is an opt-in persistent view preference without layout or control writes',async()=>{
 const storage=new Map(),app=await harness({storage});
 assert.equal(app.nodes.get('#stage').querySelector('.sync-functional'),null);
 const before=JSON.stringify(app.run('packLayout()'));
 app.event('change',{id:'show-sync-bounds',checked:true});
 assert.ok(app.nodes.get('#stage').querySelector('.sync-functional'));
 assert.equal(JSON.stringify(app.run('packLayout()')),before);
 assert.equal(app.run('editGeneration'),0);assert.equal(app.run('history.length'),0);
 await app.flush();assert.equal(app.posts.length,0);
 const reloaded=await harness({storage});
 assert.ok(reloaded.nodes.get('#stage').querySelector('.sync-functional'));
 reloaded.event('change',{id:'show-sync-bounds',checked:false});
 assert.equal(reloaded.nodes.get('#stage').querySelector('.sync-functional'),null);
});
test('functional fade follows desk edges while desk and monitor center guides remain independent',async()=>{
 const app=await harness();
 assert.equal(app.run('syncFadeY(500)'),500);
 assert.equal(app.run('syncFadeY(100)'),500);
 assert.equal(app.run('syncFadeY(25)'),575);
 assert.equal(app.run('syncFadeY(975)'),575);
 const center=app.nodes.get('#stage').querySelector('.table-center-axis').getAttribute('d');
 app.run('screen().pos.x+=10;renderStage()');
 assert.equal(app.nodes.get('#stage').querySelector('.table-center-axis').getAttribute('d'),center);
 assert.match(app.nodes.get('#stage').querySelector('.monitor-axis').getAttribute('d'),/^M600 /);
 assert.equal(app.run('syncFadeY(25)'),575);
 app.run('layout.desk.width=100;layout.desk.height=95;renderStage()');
 assert.equal(app.run('syncFadeY(100)'),600);
 assert.equal(app.posts.length,0);
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

const liveSnapshot=(overrides={})=>({version:1,connected:true,mode:'sync',sync:{state:'active'},lamps:{
 'govee-left':{online:true,on:true,brightness:65,colors:[[166,0,0],[0,120,0],[0,0,90],[12,34,56],[70,80,90],[1,2,3],[250,240,230]],source:'sync-output',stale:false},
 'play-center':{online:true,on:true,brightness:50,colors:[[81,29,7]],source:'sync-output',stale:false},
 'hue-monitor':{online:true,on:true,brightness:100,colors:[[1,0,0],[2,0,0],[3,0,0],[4,0,0],[5,0,0],[6,0,0],[7,0,0],[8,0,0],[9,0,0]],source:'sync-output',stale:false}
},...overrides});
const zones=(app,id)=>app.nodes.get('#stage').querySelectorAll('[data-preview-lamp]').filter(node=>node.dataset.previewLamp===id&&node.tagName==='PATH'&&!node.hasAttribute('data-preview-glow'));
const zone=(app,id,index=0)=>zones(app,id).find(node=>node.dataset.zone===String(index));
const glows=(app,id)=>app.nodes.get('#stage').querySelectorAll('[data-preview-glow]').filter(node=>node.dataset.previewLamp===id);
test('a Govee raised onto an 85 cm desk saves and reloads its 145 cm anchor without moving existing fixtures',async()=>{
 const app=await harness(),stage=app.nodes.get('#stage');
 stage.dispatch('pointerdown',{target:stage.querySelector('[data-lamp="govee-left"]'),pointerId:7,clientX:100,clientY:550});
 stage.dispatch('pointermove',{clientX:100,clientY:125});stage.dispatch('pointerup',{});await app.flush();
 const result=app.posts.at(-1);
 assert.deepEqual(result.devices[0].positionCm,{x:20,height:145});
 assert.deepEqual(result.devices.slice(1),saved().devices.slice(1));
 assert.match(app.nodes.get('#selection-panel').innerHTML,/85 см від підлоги/);
 const reloaded=await harness({remote:result});reloaded.run('layout.devices[0].scale=40;dirty()');await reloaded.flush();
 assert.deepEqual(reloaded.posts.at(-1).devices[0].positionCm,{x:20,height:145});
});
test('negative stage coordinates round-trip raised enrolled real and virtual floors up to the 300 cm anchor bound',async()=>{
 const remote=saved();remote.devices[0].positionCm.height=300;
 remote.devices.push({id:discovered().id,name:'Raised real',model:'H6076',shape:'floor',zones:7,scale:80,positionCm:{x:100,height:200}},
  {id:'virtual-raised',name:'Raised drawing',shape:'floor',scale:80,positionCm:{x:150,height:180}});
 const app=await harness({remote});app.run('layout.devices[0].scale=40;dirty()');await app.flush();
 for(const [index,height]of [[0,300],[6,200],[7,180]])assert.deepEqual(app.posts.at(-1).devices[index].positionCm,{x:remote.devices[index].positionCm.x,height});
 app.run('layout.devices[0].pos.y=-500;autoSnap(layout.devices[0]);dirty()');await app.flush();assert.equal(app.posts.at(-1).devices[0].positionCm.height,300);
 app.run('layout.devices[0].pos.y=500;autoSnap(layout.devices[0]);dirty()');await app.flush();assert.equal(app.posts.at(-1).devices[0].positionCm.height,60);
});
test('stage bounds include the whole active raised floor but ignore an excluded high distant floor',async()=>{
 const baseline=await harness(),remote=saved();remote.devices[0].positionCm={x:-200,height:300};remote.devices[0].enabled=false;
 const hidden=await harness({remote});assert.equal(hidden.nodes.get('#stage').getAttribute('viewBox'),baseline.nodes.get('#stage').getAttribute('viewBox'));
 remote.devices[0].enabled=true;const active=await harness({remote});
 const [left,top]=active.nodes.get('#stage').getAttribute('viewBox').split(' ').map(Number);
 assert.ok(top<=-1100);assert.ok(left<=-1100);
});
test('raising a floor freezes the viewport during dragging and expands it after release',async()=>{
 const app=await harness(),stage=app.nodes.get('#stage'),original=stage.getAttribute('viewBox');
 stage.dispatch('pointerdown',{target:stage.querySelector('[data-lamp="govee-left"]'),pointerId:7,clientX:100,clientY:550});
 stage.dispatch('pointermove',{clientX:100,clientY:-150});assert.equal(stage.getAttribute('viewBox'),original);
 stage.dispatch('pointermove',{clientX:100,clientY:-650});assert.equal(stage.getAttribute('viewBox'),original);
 stage.dispatch('pointerup',{});assert.ok(Number(stage.getAttribute('viewBox').split(' ')[1])<=-1100);
});
test('each enrolled role can leave the composition without deleting its saved inventory row',async()=>{
 const app=await harness({pilot:liveSnapshot()});
 for(const row of saved().devices){
  app.run(`choose('${row.id}')`);
  assert.ok(app.nodes.get('#selection-panel').querySelector('[data-action="remove"]'));
  app.run('actions.remove()');await app.flush();
  assert.deepEqual(app.posts.at(-1).devices.find(d=>d.id===row.id),{...row,enabled:false});
  assert.equal(app.nodes.get('#stage').querySelector(`[data-lamp="${row.id}"]`),null);
  assert.equal(zones(app,row.id).length,0);assert.equal(glows(app,row.id).length,0);
 }
 assert.equal(app.posts.at(-1).devices.length,6);
 assert.ok(app.nodes.get('#stage').querySelector('[data-monitor]'));
 assert.equal(app.nodes.get('#selection-panel').hidden,true);
});
test('disabled fixtures survive reload while the monitor remains independently editable',async()=>{
 const remote=saved();remote.devices[2].enabled=false;remote.devices[3].enabled=false;
 remote.devices.push({id:discovered().id,name:'Saved placement',model:'H6076',shape:'floor',zones:7,scale:45,enabled:false,positionCm:{x:150,height:60}});
 const app=await harness({remote,pilot:liveSnapshot()});
 assert.equal(app.nodes.get('#stage').querySelector('[data-lamp="hue-monitor"]'),null);
 assert.equal(app.nodes.get('#stage').querySelector(`[data-lamp="${discovered().id}"]`),null);
 assert.equal(glows(app,'hue-monitor').length,0);
 const monitor=app.nodes.get('#stage').querySelector('[data-monitor]');
 app.nodes.get('#stage').dispatch('click',{target:monitor});
 app.event('change',{id:'monitor-offset',value:'25',valueAsNumber:25});await app.flush();
 assert.deepEqual(app.posts.at(-1).devices[2],{...remote.devices[2],positionCm:{x:125,height:110}});
 assert.equal(app.posts.at(-1).devices[3].enabled,false);assert.equal(app.posts.at(-1).devices[6].enabled,false);
 assert.ok(app.nodes.get('#stage').querySelector('[data-monitor]'));
 assert.equal(app.posts.at(-1).devices[0].enabled,undefined);
});
test('an inactive discovered role rejoins at capacity with its identity geometry and calibration intact',async()=>{
 const remote=saved();Object.assign(remote.devices[3],{enabled:false,scale:35,orientation:'vertical',positionCm:{x:55,height:70}});
 for(let i=0;i<6;i++)remote.devices.push({id:'virtual-'+i,name:'Drawing',shape:'bar',scale:80,positionCm:{x:100,height:40}});
 const item=discovered({layout_id:'play-left',selectable:false,shape:'bar',zones:1,model:'Hue Play'});
 const app=await harness({remote,discovery:{connected:true,devices:[item],providers:{hue:'ready'}}});
 app.run('add()');await settle();
 const checkbox=app.nodes.get('#modal-content').querySelector(`[data-discovery="${item.id}"]`);assert.equal(checkbox.disabled,false);
 app.event('change',{dataset:{discovery:item.id},checked:true});
 assert.equal(app.nodes.get('#modal-content').querySelector('[data-action="add-selected"]').disabled,false);
 app.run('addSelected()');await app.flush();
 assert.equal(app.posts.at(-1).devices.length,12);
 assert.deepEqual(app.posts.at(-1).devices[3],{id:'play-left',scale:35,orientation:'vertical',positionCm:{x:55,height:70}});
 assert.equal(app.posts.at(-1).devices.some(d=>d.id===item.id),false);
 assert.equal(app.run('selected'),'play-left');assert.ok(app.nodes.get('#stage').querySelector('[data-lamp="play-left"]'));
});
test('inactive role rejoin keeps discovery safety checks and new placements still need free inventory capacity',async()=>{
 const remote=saved();remote.devices[0].enabled=false;
 for(let i=0;i<6;i++)remote.devices.push({id:'virtual-'+i,name:'Drawing',shape:'bar',scale:80,positionCm:{x:100,height:40}});
 let item=discovered({layout_id:'govee-left',selectable:false});
 const app=await harness({remote,discovery:()=>({connected:true,devices:[item,discovered({id:'device-'+'b'.repeat(32)})],providers:{govee:'ready'}})});
 app.run('add()');await settle();
 for(const changes of [{ambiguous:true},{supported:false},{online:false}]){
  item=discovered({layout_id:'govee-left',selectable:false,...changes});await app.poll();
  assert.equal(app.nodes.get('#modal-content').querySelector(`[data-discovery="${item.id}"]`).disabled,true);
 }
 item=discovered({layout_id:'govee-left',selectable:false});await app.poll();
 for(const id of [item.id,'device-'+'b'.repeat(32)])app.event('change',{dataset:{discovery:id},checked:true});
 assert.equal(app.nodes.get('#modal-content').querySelector('[data-action="add-selected"]').disabled,true);
 app.run('addSelected()');assert.equal(app.posts.length,0);assert.equal(app.run('layout.devices[0].enabled'),false);
});
test('composition removal merges independent remote geometry and undo preserves that geometry',async()=>{
 const remote=saved();remote.devices[0].positionCm.x=50;remote.devices[2].positionCm.x=125;
 const app=await harness({conflict:remote});app.run("choose('govee-left');actions.remove()");await app.flush();
 assert.deepEqual(app.posts.at(-1).devices[0],{...remote.devices[0],enabled:false});
 assert.deepEqual(app.posts.at(-1).devices[2],remote.devices[2]);
 app.run('layout=history.pop();dirty();render()');await app.flush();
 assert.deepEqual(app.posts.at(-1).devices[0],remote.devices[0]);
 assert.ok(app.nodes.get('#stage').querySelector('[data-lamp="govee-left"]'));
});
test('offline membership drafts merge remote calibration and default true does not override remote removal',async()=>{
 const base=saved(),draft=saved(),remote=saved();draft.devices[3].enabled=false;remote.devices[3].scale=35;remote.devices[3].orientation='vertical';
 remote.devices[2].positionCm.x=125;
 const storage=new Map([['light-studio-grid-v3',JSON.stringify(draft)],['light-studio-grid-v3-base',JSON.stringify(base)],['light-studio-grid-v3-pending','true']]);
 const app=await harness({remote,storage});await app.flush();
 assert.deepEqual(app.posts.at(-1).devices[3],{...remote.devices[3],enabled:false});assert.deepEqual(app.posts.at(-1).devices[2],remote.devices[2]);
 const explicit=saved();explicit.devices[0].enabled=true;
 const removed=saved();removed.devices[0].enabled=false;
 const next=await harness({remote:explicit,conflict:removed});next.run('layout.devices[0].scale=40;dirty()');await next.flush();
 assert.equal(next.posts.at(-1).devices[0].enabled,false);assert.equal(next.posts.at(-1).devices[0].scale,40);
});
test('remote removal ends a fixture drag and clears its selection without affecting the monitor',async()=>{
 const remote=saved();remote.devices[4].enabled=false;
 const app=await harness({conflict:remote}),stage=app.nodes.get('#stage');
 stage.dispatch('pointerdown',{target:stage.querySelector('[data-lamp="play-center"]'),pointerId:7,clientX:500,clientY:500});
 stage.dispatch('pointermove',{clientX:550,clientY:500});
 app.run('dirty()');await app.flush();
 assert.equal(app.run('dragging'),null);assert.equal(app.run('selected'),null);
 assert.equal(app.nodes.get('#selection-panel').hidden,true);
 assert.doesNotThrow(()=>stage.dispatch('pointermove',{clientX:575,clientY:500}));
 assert.ok(stage.querySelector('[data-monitor]'));
});
test('wall glow follows calibrated zones behind furniture without becoming an interactive lamp',async()=>{
 const app=await harness({pilot:liveSnapshot()}),stage=app.nodes.get('#stage');
 assert.equal(glows(app,'govee-left').length,7);
 assert.deepEqual(glows(app,'govee-left').map(node=>node.getAttribute('stroke')),zones(app,'govee-left').map(node=>node.getAttribute('stroke')));
 assert.equal(glows(app,'play-center')[0].getAttribute('stroke'),'rgb(81,29,7)');
 assert.equal(glows(app,'hue-monitor').filter(node=>node.tagName==='PATH').length,9);
 assert.ok(stage.innerHTML.indexOf('class="wall-glow-layer"')<stage.innerHTML.indexOf('class="desk-target"'));
 assert.equal(stage.querySelectorAll('[data-lamp]').length,6);
 assert.equal(app.posts.length,0);
});
test('wall glow clears on Off or unavailable output and returns with fresh scene data without layout writes',async()=>{
 let snapshot=liveSnapshot();const app=await harness({pilot:()=>snapshot}),stage=app.nodes.get('#stage'),writes=stage.writes;
 const glow=glows(app,'govee-left')[0];assert.ok(glow);
 for(const changes of [{on:false},{stale:true},{online:false},{source:'unknown'},{colors:[]}]){
  snapshot=liveSnapshot();Object.assign(snapshot.lamps['govee-left'],changes);await app.advance(200);
  assert.equal(glow.getAttribute('opacity'),'0');
 }
 snapshot=liveSnapshot({mode:'scene',sync:{state:'off'}});await app.advance(200);
 assert.equal(glow.getAttribute('opacity'),'1');
 await app.visible(false);assert.equal(glow.getAttribute('opacity'),'0');
 assert.equal(stage.writes,writes);assert.equal(app.posts.length,0);
});
test('hidden black Sync segments have no background emission while visible zones keep their colors',async()=>{
 const snapshot=liveSnapshot();snapshot.lamps['govee-left'].colors.splice(0,4,...Array.from({length:4},()=>[0,0,0]));
 const app=await harness({pilot:snapshot});
 assert.deepEqual(glows(app,'govee-left').map(node=>node.getAttribute('opacity')),['0','0','0','0','1','1','1']);
 assert.equal(zone(app,'govee-left',0).getAttribute('stroke'),'rgb(0,0,0)');
 assert.equal(app.posts.length,0);
});
test('live output paints calibrated RGB once, seven bottom-to-top Govee zones and all nine U-path segments',async()=>{
 const app=await harness({pilot:liveSnapshot()});
 assert.equal(zone(app,'govee-left')?.getAttribute('stroke'),'rgb(166,0,0)');
 assert.equal(zone(app,'govee-left',6).getAttribute('stroke'),'rgb(250,240,230)');
 assert.ok(Number(zone(app,'govee-left').getAttribute('d').split(' ')[1].split('v')[0])>Number(zone(app,'govee-left',6).getAttribute('d').split(' ')[1].split('v')[0]));
 assert.equal(zone(app,'play-center').getAttribute('stroke'),'rgb(81,29,7)');
 const segments=zones(app,'hue-monitor').filter(node=>node.tagName==='PATH');
 assert.equal(segments.length,9);assert.deepEqual(segments.map(node=>node.getAttribute('stroke')),['rgb(1,0,0)','rgb(2,0,0)','rgb(3,0,0)','rgb(4,0,0)','rgb(5,0,0)','rgb(6,0,0)','rgb(7,0,0)','rgb(8,0,0)','rgb(9,0,0)']);
 assert.equal(app.nodes.get('#sync-status').textContent,'Sync: активний');
});
test('telemetry updates attributes without replacing the stage, focused sidebar, drag capture, history or saved layout',async()=>{
 let snapshot=liveSnapshot();const app=await harness({pilot:()=>snapshot});
 const stage=app.nodes.get('#stage'),panel=app.nodes.get('#selection-panel');
 stage.dispatch('pointerdown',{target:stage.querySelector('[data-lamp="play-center"]'),pointerId:7,clientX:500,clientY:500});
 stage.dispatch('pointermove',{clientX:550,clientY:500});
 const input=panel.querySelector('#brightness');input.focus();
 const stageWrites=stage.writes,panelWrites=panel.writes,storage=[...app.storage],history=app.run('history.length'),paint=zone(app,'play-center');
 snapshot=liveSnapshot();snapshot.lamps['play-center'].colors=[[17,33,99]];await app.advance(250);
 assert.equal(paint.getAttribute('stroke'),'rgb(17,33,99)');assert.equal(zone(app,'play-center'),paint);
 assert.equal(stage.writes,stageWrites);assert.equal(panel.writes,panelWrites);assert.equal(app.active(),input);assert.equal(stage.capture,7);assert.equal(app.run('dragging.id'),'play-center');
 assert.equal(app.run('history.length'),history);assert.equal(app.run('editGeneration'),0);assert.equal(app.posts.length,0);assert.deepEqual([...app.storage],storage);
});
test('off, stale, unavailable and paused or foreign modes mask colors while scene Sync-off keeps scene RGB',async()=>{
 let snapshot=liveSnapshot();const app=await harness({pilot:()=>snapshot});
 snapshot.lamps['govee-left'].on=false;await app.advance(250);
 assert.equal(zone(app,'govee-left').getAttribute('opacity'),'0');
 snapshot=liveSnapshot();snapshot.lamps['govee-left'].stale=true;await app.advance(250);
 assert.equal(zone(app,'govee-left').getAttribute('data-output'),'neutral');
 snapshot=liveSnapshot();snapshot.lamps['govee-left'].online=false;await app.advance(250);
 assert.equal(zone(app,'govee-left').getAttribute('data-output'),'neutral');
 for(const [mode,state,label]of [['waiting','waiting','очікування'],['foreign','foreign','зовнішній'],['unknown','unknown','невідомо'],['paused','paused','пауза']]){
  snapshot=liveSnapshot({mode,sync:{state}});
  if(['waiting','unknown'].includes(mode))snapshot.lamps['play-center']={...snapshot.lamps['play-center'],stale:true,colors:[]};
  await app.advance(250);
  assert.equal(zone(app,'play-center').getAttribute('data-output'),'neutral');assert.equal(app.nodes.get('#sync-status').textContent,`Sync: ${label}`);
 }
 snapshot=liveSnapshot({mode:'scene',sync:{state:'off'}});await app.advance(250);
 assert.equal(zone(app,'play-center').getAttribute('stroke'),'rgb(81,29,7)');assert.equal(app.nodes.get('#sync-status').textContent,'Sync: неактивний');
 snapshot=liveSnapshot({connected:false});await app.advance(250);
 assert.equal(zone(app,'play-center').getAttribute('data-output'),'neutral');assert.equal(app.nodes.get('#sync-status').textContent,'Sync: невідомо');
});
test('waiting or unknown source-control status keeps verified fresh scene output visible with the correct Sync badge',async()=>{
 let snapshot=liveSnapshot({mode:'waiting',sync:{state:'waiting'}});
 for(const lamp of Object.values(snapshot.lamps))lamp.source='scene-output';
 const app=await harness({pilot:()=>snapshot});
 assert.equal(zone(app,'govee-left').getAttribute('stroke'),'rgb(166,0,0)');
 assert.equal(zone(app,'play-center').getAttribute('stroke'),'rgb(81,29,7)');assert.equal(app.nodes.get('#sync-status').textContent,'Sync: очікування');
 snapshot.mode='unknown';snapshot.sync.state='unknown';await app.advance(200);
 assert.equal(zone(app,'play-center').getAttribute('data-output'),'active');assert.equal(zone(app,'play-center').getAttribute('stroke'),'rgb(81,29,7)');assert.equal(app.nodes.get('#sync-status').textContent,'Sync: невідомо');
 snapshot.lamps['play-center'].stale=true;await app.advance(200);
 assert.equal(zone(app,'play-center').getAttribute('data-output'),'neutral');
 snapshot.lamps['play-center'].stale=false;snapshot.lamps['play-center'].colors=[];await app.advance(200);
 assert.equal(zone(app,'play-center').getAttribute('data-output'),'neutral');assert.equal(app.posts.length,0);
});
test('unbound and new placements stay neutral even when a payload includes their RGB',async()=>{
 const remote=saved();remote.devices.push({id:discovered().id,name:'New lamp',shape:'floor',model:'H6076',zones:7,scale:80,positionCm:{x:100,height:60}},{id:'virtual-test',name:'Drawing',shape:'bar',scale:80,positionCm:{x:100,height:40}});
 const snapshot=liveSnapshot();snapshot.lamps[discovered().id]=snapshot.lamps['govee-left'];snapshot.lamps['virtual-test']=snapshot.lamps['play-center'];
 const app=await harness({remote,pilot:snapshot});
 for(const id of [discovered().id,'virtual-test','play-left'])assert.equal(zone(app,id).getAttribute('data-output'),'neutral');
});
test('poll deadline and independent freshness watchdog clear hung output and fence late responses',async()=>{
 let count=0,release;const app=await harness({pilot:()=>++count===1?liveSnapshot():new Promise(resolve=>release=resolve)});
 await app.advance(400);assert.equal(app.pilotRequests.length,2);
 await app.advance(600);assert.equal(app.pilotRequests.length,2);assert.equal(app.pilotRequests[1].signal.aborted,false);
 await app.advance(200);assert.equal(app.pilotRequests[1].signal.aborted,true);
 assert.equal(zone(app,'play-center').getAttribute('data-output'),'neutral');assert.equal(app.nodes.get('#sync-status').textContent,'Sync: невідомо');
 release(liveSnapshot());await settle();assert.equal(zone(app,'play-center').getAttribute('data-output'),'neutral');
});
test('local freshness expires independently even while an in-flight fetch never completes',async()=>{
 let count=0;const app=await harness({pilot:()=>++count===1?liveSnapshot():new Promise(()=>{})});
 await app.advance(200);await app.wake(1300,1500);
 assert.equal(app.pilotRequests[1].signal.aborted,false);
 assert.equal(zone(app,'play-center').getAttribute('data-output'),'neutral');assert.equal(app.nodes.get('#sync-status').textContent,'Sync: невідомо');
 assert.equal(app.posts.length,0);
});
test('visibility suspends polling, aborts the old request and refreshes immediately without accepting its late response',async()=>{
 let count=0,release;const fresh=liveSnapshot();fresh.lamps['play-center'].colors=[[7,8,9]];
 const app=await harness({pilot:()=>++count===1?liveSnapshot():count===2?new Promise(resolve=>release=resolve):fresh});
 await app.advance(200);await app.visible(false);assert.equal(app.pilotRequests[1].signal.aborted,true);
 const calls=app.pilotRequests.length;await app.advance(2000);assert.equal(app.pilotRequests.length,calls);
 await app.visible(true);assert.equal(zone(app,'play-center').getAttribute('stroke'),'rgb(7,8,9)');
 release(liveSnapshot());await settle();assert.equal(zone(app,'play-center').getAttribute('stroke'),'rgb(7,8,9)');
});
test('sleep pause remains visible with disconnected adapter and never retains the previous colors',async()=>{
 let snapshot=liveSnapshot();const app=await harness({pilot:()=>snapshot});
 snapshot={version:1,connected:false,mode:'paused',sync:{state:'paused'},lamps:{}};await app.advance(200);
 assert.equal(app.nodes.get('#sync-status').textContent,'Sync: пауза');assert.equal(zone(app,'play-center').getAttribute('data-output'),'neutral');
});
test('scene colors repeat a single calibrated sample and invalid RGB or failed refresh clears previous output',async()=>{
 let snapshot=liveSnapshot({mode:'scene',sync:{state:'off'}});snapshot.lamps['govee-left'].colors=[[166,0,0]];
 const app=await harness({pilot:()=>{if(snapshot instanceof Error)throw snapshot;return snapshot;}});
 assert.deepEqual(zones(app,'govee-left').map(node=>node.getAttribute('stroke')),Array(7).fill('rgb(166,0,0)'));
 for(const colors of [[[256,0,0]],[[-1,0,0]],[[1.5,0,0]],[[1,2]],[[1,2,'3']],[]]){
  snapshot.lamps['govee-left'].colors=colors;await app.advance(200);assert.equal(zone(app,'govee-left').getAttribute('data-output'),'neutral');
 }
 snapshot=new Error('offline');await app.advance(200);
 assert.equal(zone(app,'play-center').getAttribute('data-output'),'neutral');assert.equal(app.nodes.get('#sync-status').textContent,'Sync: невідомо');
 assert.equal(app.posts.length,0);
});
test('unrecognized output sources cannot glow even with valid fresh RGB',async()=>{
 let snapshot=liveSnapshot();const app=await harness({pilot:()=>snapshot});
 assert.equal(zone(app,'play-center').getAttribute('data-output'),'active');
 for(const source of ['unknown','native-output',undefined]){
  snapshot.lamps['play-center'].source=source;await app.advance(200);
  assert.equal(zone(app,'play-center').getAttribute('data-output'),'neutral');
 }
 snapshot.lamps['play-center'].source='scene-output';await app.advance(200);
 assert.equal(zone(app,'play-center').getAttribute('stroke'),'rgb(81,29,7)');assert.equal(zone(app,'play-center').getAttribute('data-output'),'active');assert.equal(app.posts.length,0);
});
test('known online power-off hides light without color history while unknown power stays neutral',async()=>{
 let snapshot=liveSnapshot();snapshot.lamps['play-center']={online:true,on:false,brightness:0,colors:[],source:'unknown',stale:true};
 const app=await harness({pilot:()=>snapshot});
 assert.equal(zone(app,'play-center').getAttribute('data-output'),'off');assert.equal(zone(app,'play-center').getAttribute('opacity'),'0');
 snapshot.lamps['play-center'].on=null;await app.advance(200);
 assert.equal(zone(app,'play-center').getAttribute('data-output'),'neutral');assert.equal(zone(app,'play-center').getAttribute('opacity'),'.35');
});

const customZone=()=>({version:1,points:[{u:-1.625,h:0},{u:1.625,h:0},{u:1.625,h:-65},{u:1,h:-15},{u:-1,h:-15},{u:-1.625,h:-65}]});
const keyEvent=(target,key)=>({target,key,preventDefault(){this.prevented=true;}});
test('floor clamps loaded and rotated physical fixtures without moving the table',async()=>{
 const remote=saved();remote.devices[0].positionCm.height=-10;remote.devices[3].positionCm.height=-10;remote.devices[4].positionCm.height=-10;remote.devices[4].orientation='vertical';
 remote.devices.push({id:'virtual-strip',shape:'strip',name:'Strip',scale:80,positionCm:{x:100,height:-5}});
 const app=await harness({remote});
 assert.equal(app.run('heightCm(layout.devices[0])'),60);assert.equal(app.run('heightCm(layout.devices[3])'),5);assert.equal(app.run('heightCm(layout.devices[4])'),15);assert.equal(app.run('heightCm(layout.devices[6])'),5);
 const stage=app.nodes.get('#stage'),before=JSON.stringify(app.run('packLayout()'));
 stage.dispatch('pointerdown',{target:stage.querySelector('[data-desk]'),pointerId:7,clientX:500,clientY:450});stage.dispatch('pointermove',{clientX:700,clientY:800});stage.dispatch('pointerup',{});
 stage.dispatch('keydown',keyEvent(stage.querySelector('[data-desk]'),'ArrowDown'));assert.equal(JSON.stringify(app.run('packLayout()')),before);
 app.run("selected='play-left'");app.event('change',{name:'orientation',value:'vertical'});assert.equal(app.run('heightCm(layout.devices[3])'),15);
 assert.match(stage.innerHTML,/cy="294.25" rx="21" ry="5"/);assert.ok(stage.querySelector('.floor-line'));assert.match(stage.innerHTML,/Підлога · 0 см/);
});
test('all physical orientations stay above floor after mouse and keyboard movement',async()=>{
 const app=await harness(),stage=app.nodes.get('#stage');
 for(const [id,minimum]of [['govee-left',60],['play-left',5],['play-center',15],['hue-monitor',105]]){
  if(id==='play-center')app.run("layout.devices[4].orientation='vertical';render()");
  const node=stage.querySelector(`[data-lamp="${id}"]`);
  stage.dispatch('pointerdown',{target:node,pointerId:8,clientX:0,clientY:0});stage.dispatch('pointermove',{clientX:0,clientY:5000});stage.dispatch('pointerup',{});
  assert.ok(app.run(`heightCm(layout.devices.find(d=>d.id==='${id}'))`)>=minimum);
  stage.dispatch('keydown',keyEvent(stage.querySelector(`[data-lamp="${id}"]`),'ArrowDown'));
  assert.ok(app.run(`heightCm(layout.devices.find(d=>d.id==='${id}'))`)>=minimum);
 }
});
test('zone validation rejects malformed crossing and nonmonotone geometry and interpolates world sections',async()=>{
 const app=await harness();assert.equal(app.run(`validSyncZone(${JSON.stringify(customZone())})`),true);
 for(const mutate of [z=>z.points.pop(),z=>z.points[0].u=true,z=>z.points[0].u=21,z=>z.points[0].h=401,z=>z.extra=true,z=>z.points[2].extra=1,z=>z.points[3].u=z.points[4].u,z=>z.points[4].h=2,z=>z.version=2]){
  const config=customZone();mutate(config);assert.equal(app.run(`validSyncZone(${JSON.stringify(config)})`),false);
 }
 const points=[{x:-30,height:85},{x:230,height:85},{x:230,height:20},{x:180,height:70},{x:20,height:70},{x:-30,height:20}];
 app.run(`layout.syncZone=${JSON.stringify(customZone())}`);
 assert.deepEqual(JSON.parse(app.run('JSON.stringify(syncZonePoints())')),points);
 assert.deepEqual(JSON.parse(app.run('JSON.stringify(syncZoneBounds(syncZonePoints(),-5))')),[45,85]);assert.equal(app.run('syncZoneBounds(syncZonePoints(),-31)'),null);
 assert.deepEqual(JSON.parse(app.run('JSON.stringify(worldToSyncZone(syncZonePoints()))')),customZone());
 app.run('layout.desk.width=240;layout.desk.height=100');assert.deepEqual(JSON.parse(app.run('JSON.stringify(syncZonePoints()[0])')),{x:-95,height:100});
});
test('default zone covers enabled fixture and monitor extent without attaching to hidden placements',async()=>{
 const app=await harness();app.run('layout.devices[0].pos.x=-100;layout.devices[1].enabled=false;layout.devices[1].pos.x=200');
 const points=JSON.parse(app.run('JSON.stringify(syncZonePoints())'));
 assert.equal(points[0].x,-220);assert.equal(points[1].x,230);assert.equal(points[5].height,-170);assert.equal(points[4].height,70);
 assert.equal(app.run('packLayout().syncZone'),null);
});
test('Sync shows smooth fading bands behind objects and a closed foreground contour with six accessible corners',async()=>{
 const app=await harness();app.event('change',{id:'show-sync-bounds',checked:true});const stage=app.nodes.get('#stage');
 const bands=stage.querySelectorAll('.sync-functional-band');assert.equal(bands.length,20);
 assert.ok(Number(bands[0].getAttribute('fill-opacity'))>Number(bands.at(-1).getAttribute('fill-opacity')));
 assert.equal(new Set(bands.map(n=>n.getAttribute('d'))).size,20);
 assert.match(stage.querySelector('.sync-functional-limit').getAttribute('d'),/Z$/);
 assert.equal(stage.querySelectorAll('[data-sync-corner]').length,6);
 for(const corner of stage.querySelectorAll('[data-sync-corner]')){assert.equal(corner.getAttribute('tabindex'),'0');assert.ok(corner.getAttribute('aria-label'));}
 assert.ok(stage.innerHTML.indexOf('class="sync-functional"')<stage.innerHTML.indexOf('class="desk-target"'));
 assert.ok(stage.innerHTML.indexOf('class="sync-outline"')>stage.innerHTML.lastIndexOf('data-lamp='));
});
test('viewport and foreground guides include functional below-floor geometry and freeze for corner drag',async()=>{
 const remote=saved();remote.syncZone=customZone();remote.syncZone.points[2].h=-300;remote.syncZone.points[5].h=-300;
 const app=await harness({remote});app.event('change',{id:'show-sync-bounds',checked:true});const stage=app.nodes.get('#stage');
 const [left,top,width,height]=stage.getAttribute('viewBox').split(' ').map(Number),bottom=top+height;
 assert.ok(bottom>1925);assert.equal(stage.querySelector('.table-center-axis').getAttribute('d'),`M500 ${top}V${bottom}`);
 assert.ok(stage.innerHTML.indexOf('class="reference-guides"')>stage.innerHTML.lastIndexOf('data-lamp='));
 assert.match(stage.querySelector('.table-baseline').getAttribute('d'),new RegExp(`M${left} .*H${left+width}`));
 const old=stage.getAttribute('viewBox');stage.dispatch('pointerdown',{target:stage.querySelector('[data-sync-corner="5"]'),pointerId:3,clientX:-150,clientY:1925});stage.dispatch('pointermove',{clientX:-200,clientY:2200});
 assert.equal(stage.getAttribute('viewBox'),old);stage.dispatch('pointerup',{});assert.notEqual(stage.getAttribute('viewBox'),old);
});
test('corner drag saves only on finish and Escape restores the gesture without history or autosave',async()=>{
 const app=await harness();app.event('change',{id:'show-sync-bounds',checked:true});const stage=app.nodes.get('#stage'),before=JSON.stringify(app.run('packLayout()'));
 stage.dispatch('pointerdown',{target:stage.querySelector('[data-sync-corner="0"]'),pointerId:3,clientX:-150,clientY:425});stage.dispatch('pointermove',{clientX:-175,clientY:400});await app.flush();assert.equal(app.posts.length,0);
 app.event('keydown',stage,{key:'Escape'});assert.equal(JSON.stringify(app.run('packLayout()')),before);assert.equal(app.run('history.length'),0);assert.equal(app.run('editGeneration'),0);
 stage.dispatch('pointerdown',{target:stage.querySelector('[data-sync-corner="0"]'),pointerId:4,clientX:-150,clientY:425});stage.dispatch('pointermove',{clientX:-175,clientY:400});stage.dispatch('pointerup',{});await app.flush();
 assert.equal(app.posts.length,1);assert.equal(app.posts[0].syncZone.points[0].h,5);assert.equal(app.active().dataset.syncCorner,'0');assert.deepEqual(app.posts[0].devices,saved().devices);
});
test('every corner has five-centimeter keyboard edits with focus undo reset and invalid-shape rejection',async()=>{
 const app=await harness();app.event('change',{id:'show-sync-bounds',checked:true});const stage=app.nodes.get('#stage');
 for(let i=0;i<6;i++){
  const before=JSON.parse(app.run('JSON.stringify(syncZonePoints())'));stage.dispatch('keydown',keyEvent(stage.querySelector(`[data-sync-corner="${i}"]`),i<2?'ArrowUp':'ArrowDown'));
  assert.equal(app.run(`syncZonePoints()[${i}].height`),before[i].height+(i<2?5:-5));assert.equal(app.active().dataset.syncCorner,String(i));
 }
 await app.flush();assert.equal(app.posts.length,1);assert.ok(app.nodes.get('#reset-sync-zone').hidden===false);
 const before=JSON.stringify(app.run('layout.syncZone')),generation=app.run('editGeneration');
 app.run('moveSyncCorner(3, {x: syncZonePoints()[4].x, height: 70})');assert.equal(JSON.stringify(app.run('layout.syncZone')),before);assert.equal(app.run('editGeneration'),generation);
 app.run("actions['reset-sync-zone']()");await app.flush();assert.equal(app.posts.at(-1).syncZone,null);assert.equal(app.nodes.get('#reset-sync-zone').hidden,true);
 app.run('layout=history.pop();dirty();render()');await app.flush();assert.deepEqual(app.posts.at(-1).syncZone,JSON.parse(before));
});
test('custom zone survives reload conflicts and undo without changing poses or mutating input data',async()=>{
 const remote=saved();remote.syncZone=customZone();const input=JSON.stringify(remote),app=await harness({remote});
 assert.equal(JSON.stringify(remote),input);app.run('layout.devices[0].scale=40;dirty()');await app.flush();assert.deepEqual(app.posts[0].syncZone,remote.syncZone);
 const conflict=structuredClone(remote);conflict.devices[0].positionCm.x=50;const merged=await harness({remote,conflict});merged.event('change',{id:'show-sync-bounds',checked:true});
 merged.nodes.get('#stage').dispatch('keydown',keyEvent(merged.nodes.get('#stage').querySelector('[data-sync-corner="0"]'),'ArrowUp'));await merged.flush();
 assert.equal(merged.posts.at(-1).syncZone.points[0].h,5);assert.equal(merged.posts.at(-1).devices[0].positionCm.x,50);
 merged.run('layout=history.pop();dirty();render()');await merged.flush();assert.deepEqual(merged.posts.at(-1).syncZone,remote.syncZone);assert.equal(merged.posts.at(-1).devices[0].positionCm.x,50);
});

test('all six corners support mouse gestures without writing until release',async()=>{
 const app=await harness();app.event('change',{id:'show-sync-bounds',checked:true});const stage=app.nodes.get('#stage');
 for(let index=0;index<6;index++){
  const before=JSON.parse(app.run('JSON.stringify(syncZonePoints())')),p=before[index],x=p.x*5,y=850-p.height*5;
  const generations=app.run('editGeneration');
  stage.dispatch('pointerdown',{target:stage.querySelector(`[data-sync-corner="${index}"]`),pointerId:index+10,clientX:x,clientY:y});
  stage.dispatch('pointermove',{clientX:x,clientY:y+(index<2?-25:25)});
  assert.equal(app.run('editGeneration'),generations);assert.equal(app.run(`syncZonePoints()[${index}].height`),p.height+(index<2?5:-5));
  stage.dispatch('pointerup',{});assert.equal(app.run('editGeneration'),generations+1);assert.equal(app.active().dataset.syncCorner,String(index));
 }
 await app.flush();assert.equal(app.posts.length,1);assert.deepEqual(app.posts[0].devices,saved().devices);
});
test('invalid corner gestures leave the automatic shape history and autosave untouched',async()=>{
 const app=await harness();app.event('change',{id:'show-sync-bounds',checked:true});const stage=app.nodes.get('#stage'),before=JSON.stringify(app.run('packLayout()'));
 stage.dispatch('pointerdown',{target:stage.querySelector('[data-sync-corner="3"]'),pointerId:12,clientX:900,clientY:500});
 stage.dispatch('pointermove',{clientX:0,clientY:500});stage.dispatch('pointerup',{});await app.flush();
 assert.equal(JSON.stringify(app.run('packLayout()')),before);assert.equal(app.run('history.length'),0);assert.equal(app.run('editGeneration'),0);assert.equal(app.posts.length,0);
});
test('simple x-monotone polygons allow concave lower chains and pinched outer sections',async()=>{
 const app=await harness();
 const pinched={version:1,points:[{u:-1,h:0},{u:1,h:0},{u:2,h:-40},{u:.5,h:-5},{u:-.5,h:-5},{u:-2,h:-40}]};
 assert.equal(app.run(`validSyncZone(${JSON.stringify(pinched)})`),true);
 app.run(`layout.syncZone=${JSON.stringify(pinched)}`);
 assert.deepEqual(JSON.parse(app.run('JSON.stringify(syncZoneBounds(syncZonePoints(),-60))')),[45,45]);
 assert.deepEqual(JSON.parse(app.run('JSON.stringify(syncZoneBounds(syncZonePoints(),100))')),[80,85]);
 const crossing=customZone();crossing.points[0].u=0;crossing.points[5]={u:-2,h:-100};crossing.points[4]={u:-1,h:-1};
 assert.equal(app.run(`validSyncZone(${JSON.stringify(crossing)})`),false);
 const nonmonotone=structuredClone(pinched);nonmonotone.points[0].u=1.5;
 assert.equal(app.run(`validSyncZone(${JSON.stringify(nonmonotone)})`),false);
});
test('desk resizing follows saved zone coordinates while fixture geometry and identities stay fixed',async()=>{
 const remote=saved();remote.syncZone=customZone();const app=await harness({remote});
 app.event('change',{id:'desk-width',value:'200',valueAsNumber:200});app.event('change',{id:'desk-height',value:'100',valueAsNumber:100});await app.flush();
 const result=app.posts.at(-1);assert.deepEqual(result.syncZone,remote.syncZone);assert.deepEqual(result.devices.filter(d=>d.id!=='hue-monitor'),remote.devices.filter(d=>d.id!=='hue-monitor'));
 assert.equal(result.devices[2].positionCm.height,125);assert.deepEqual(JSON.parse(app.run('JSON.stringify(syncZonePoints()[0])')),{x:-62.5,height:100});
});
test('zone merges preserve remote-only zone edits and explicit local reset across conflicts',async()=>{
 const remote=saved(),conflict=saved();conflict.syncZone=customZone();
 const app=await harness({remote,conflict});app.run('layout.devices[0].scale=40;dirty()');await app.flush();assert.deepEqual(app.posts.at(-1).syncZone,conflict.syncZone);
 const reset=await harness({remote:conflict,conflict:{...conflict,room:'Remote'}});reset.run("actions['reset-sync-zone']()");await reset.flush();
 assert.equal(reset.posts.at(-1).syncZone,null);assert.equal(reset.posts.at(-1).room,'Remote');
 const loaded=await harness({remote:reset.posts.at(-1)});assert.equal(loaded.run('layout.syncZone'),null);assert.equal(loaded.nodes.get('#reset-sync-zone').hidden,true);
});
test('an older autosave conflict never persists an unfinished corner gesture',async()=>{
 const remote=saved();remote.syncZone=customZone();const conflict=structuredClone(remote);conflict.devices[0].positionCm.x=50;
 const app=await harness({remote,conflict});app.run('layout.devices[0].scale=40;dirty()');app.event('change',{id:'show-sync-bounds',checked:true});const stage=app.nodes.get('#stage');
 stage.dispatch('pointerdown',{target:stage.querySelector('[data-sync-corner="0"]'),pointerId:3,clientX:-150,clientY:425});stage.dispatch('pointermove',{clientX:-150,clientY:400});
 await app.flush();assert.equal(app.posts.length,2);assert.deepEqual(app.posts.at(-1).syncZone,remote.syncZone);assert.equal(app.run('layout.syncZone.points[0].h'),5);
 stage.dispatch('pointerup',{});await app.flush();assert.equal(app.posts.at(-1).syncZone.points[0].h,5);assert.equal(app.posts.at(-1).devices[0].positionCm.x,50);
});

// Match the API's canonical responses: null resets, omission preserves a custom zone.
function canonicalLayoutPost(stored,payload){
 const canonical=structuredClone(payload);
 if(!Object.hasOwn(payload,'syncZone')&&stored.syncZone)canonical.syncZone=structuredClone(stored.syncZone);
 if(canonical.syncZone===null)delete canonical.syncZone;
 return canonical;
}
test('undoing the first custom edit explicitly clears the server zone when canonical Auto omits its field',async()=>{
 let stored=saved();
 const app=await harness({remote:stored,postHandler:payload=>{stored=canonicalLayoutPost(stored,payload);return response(200,{layout:stored,revision:'canonical'});}});
 app.run(`remember();layout.syncZone=${JSON.stringify(customZone())};dirty()`);await app.flush();assert.deepEqual(stored.syncZone,customZone());
 app.event('keydown',app.nodes.get('#stage'),{key:'z',ctrlKey:true,preventDefault(){}});await app.flush();
 assert.equal(app.posts.at(-1).syncZone,null);assert.equal(Object.hasOwn(stored,'syncZone'),false);assert.equal(app.run('validSyncZone(layout.syncZone)'),false);
 assert.deepEqual(stored.devices,saved().devices);
});
test('a successful canonical Auto reset does not erase a later remote zone during a brightness conflict',async()=>{
 const remote=saved();remote.syncZone=customZone();const conflict=saved();conflict.syncZone=customZone();conflict.syncZone.points[0].h=5;
 let stored=structuredClone(remote);
 const app=await harness({remote,conflict,postHandler:(payload,count)=>{
  if(count===2){stored=structuredClone(conflict);return response(409,{error:'layout changed'});}
  stored=canonicalLayoutPost(stored,payload);return response(200,{layout:stored,revision:'canonical-'+count});
 }});
 app.run("actions['reset-sync-zone']()");await app.flush();assert.equal(Object.hasOwn(stored,'syncZone'),false);assert.equal(app.run('Object.hasOwn(serverBaseline,"syncZone")'),false);
 app.run('layout.devices[0].scale=40;dirty()');await app.flush();
 assert.equal(app.posts.length,3);assert.deepEqual(stored.syncZone,conflict.syncZone);assert.deepEqual(app.posts.at(-1).syncZone,conflict.syncZone);
 assert.deepEqual(stored.devices[0],{...remote.devices[0],scale:40});assert.deepEqual(stored.devices.slice(1),remote.devices.slice(1));
});
