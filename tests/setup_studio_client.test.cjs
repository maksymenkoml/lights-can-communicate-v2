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
async function harness({remote=saved(),conflict,postHandler,storage=new Map()}={}){
 const nodes=new Map(),timers=new Map(),posts=[];let timer=0,gets=0;
 const node=key=>{if(!nodes.has(key))nodes.set(key,{innerHTML:'',textContent:'',hidden:false,
  classList:{add(){},remove(){}},setAttribute(){},addEventListener(){},querySelector(){return null;}});return nodes.get(key);};
 const context=vm.createContext({structuredClone,document:{querySelector:node,addEventListener(){},activeElement:{tagName:'BODY'}},
  localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  setTimeout:(fn,delay)=>{timers.set(++timer,{fn,delay});return timer;},clearTimeout:id=>timers.delete(id),
  fetch:async(url,options)=>{
   assert.equal(url,'/api/layout');
   if(!options)return response(200,{layout:gets++&&conflict?conflict:remote,revision:gets>1?'r2':'r1'});
   const payload=JSON.parse(options.body);posts.push(payload);
   if(postHandler)return postHandler(payload,posts.length);
   if(conflict&&posts.length===1)return response(409,{error:'layout changed'});
   return response(200,{layout:payload,revision:'r3'});
  }});
 vm.runInContext(code,context);await settle();
 return {posts,storage,run:source=>vm.runInContext(source,context),async flush(){
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
