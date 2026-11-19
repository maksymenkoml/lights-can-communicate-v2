'use strict';
/* Light Studio: the page is the room.
 *
 * Layout edits autosave to /api/layout with If-Match revisions; a 409 refetches
 * and merges field by field. A browser draft keeps offline edits for retry.
 * /api/live telemetry only repaints existing SVG attributes: it never rebuilds
 * the layout and never saves. /api/discovery is polled only while the Lamps
 * panel is open. */

const $=selector=>document.querySelector(selector);
const clone=value=>structuredClone(value);
const esc=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clamp=(v,a=0,b=1)=>Math.max(a,Math.min(b,v));
const store={
 get(key){try{return localStorage.getItem(key)}catch{return null}},
 set(key,value){try{localStorage.setItem(key,value);return true}catch{return false}},
 remove(key){try{localStorage.removeItem(key)}catch{}}
};
function readJson(key){try{return JSON.parse(store.get(key))}catch{return null}}

// ---------------------------------------------------------------- model --
const KEY='light-studio-grid-v3',VIEW_KEY='light-studio-view-v1',DARK_KEY='light-studio-dark-scene-v1';
const SAVE_DELAY_MS=300,RETRY_MS=3000,DISCOVERY_POLL_MS=3000;
// About 20 updates a second: Sync frames arrive at 15-25 Hz and a loopback answer takes a few ms.
const LIVE_POLL_MS=50,LIVE_DEADLINE_MS=1000,LIVE_FRESH_MS=1500;
const STEP=5,MAX_ROWS=12,DESK_CENTER=100,FLOOR_ANCHOR=60;
const MONITOR='hue-monitor';
// Adapter roles of the six enrolled lamps (desk_layout.ROLES).
const ROLES={'govee-left':'left','govee-right':'right','hue-monitor':'strip','play-left':'hue_left','play-center':'hue_center','play-right':'hue_right'};
// Seed coordinates: x is cm from the desk centre; h is the bottom of a floor
// lamp and the centre of other lamps. Layout x has the desk centre at 100 cm and a
// floor lamp's height is its bottom + 60 cm (desk_layout.minimum_anchor_height).
const SEED=[
 {id:'govee-left',name:'Govee Left',model:'H6076',shape:'floor',zones:7,x:-80,h:0,scale:65},
 {id:'govee-right',name:'Govee Right',model:'H6076',shape:'floor',zones:7,x:80,h:0,scale:65},
 {id:'hue-monitor',name:'Monitor Strip',model:'Hue LCX006',shape:'strip',zones:9,x:0,h:110,scale:100},
 {id:'play-left',name:'Hue Play Left',model:'Hue Play',shape:'bar',zones:1,x:-30,h:40,scale:100},
 {id:'play-center',name:'Hue Play Center',model:'Hue Play',shape:'bar',zones:1,x:0,h:40,scale:50},
 {id:'play-right',name:'Hue Play Right',model:'Hue Play',shape:'bar',zones:1,x:30,h:40,scale:100}
];
const SHAPES=['floor','strip','bar'];
const realDevice=id=>/^device-[a-f0-9]{32}$/.test(id||'');
const virtualDevice=id=>/^virtual-[a-z0-9]{1,32}$/.test(id||'');
const isRole=id=>Object.hasOwn(ROLES,id);
// Hue lights take names of up to 32 characters; Govee and layout-only names up to 35.
const nameLimit=id=>isRole(id)&&!id.startsWith('govee-')?32:35;
// The drawing form: the monitor strip wraps the monitor; other strips draw as bars.
const formOf=(id,shape)=>id===MONITOR?'strip':shape==='floor'?'floor':'bar';
// During Sync a lamp takes screen colour, keeps its own light, or (role lamps only) is switched off.
const SYNC_ROLES=['sync','own','off'];
function seedLamp(s){return {...clone(s),form:formOf(s.id,s.shape),vertical:false,enabled:true,syncRole:'sync',unbound:false}}
function defaults(){
 const environment={width:160,height:85,inches:32,zones:{sync:null,scene:null},fade:{sync:true,scene:false}};
 const lamps=SEED.map(seedLamp);lamps.forEach(l=>clampFixture(l,environment));
 return {room:'Office',environment,lamps};
}
function monitorWidthCm(inches){return inches*2.54*16/Math.hypot(16,9)}
function minimumHeight(l,env=environment){if(l.form==='floor')return 0;if(l.id===MONITOR)return Math.ceil((env.inches*2.54*9/Math.hypot(16,9)/2+4.8)/5)*5;return l.vertical?15:5}
// Layout anchors must stay within x -200..400 cm and height 0..300 cm (desk_layout);
// a floor lamp's anchor is its bottom + 60 cm.
const X_LIMIT=300;
function maximumHeight(l){return l.form==='floor'?300-FLOOR_ANCHOR:300}
function clampFixture(l,env=environment){l.x=clamp(Math.round(l.x/5)*5,-X_LIMIT,X_LIMIT);l.h=clamp(Math.round(l.h/5)*5,minimumHeight(l,env),maximumHeight(l))}
const finite=(v,lo,hi,fallback)=>typeof v==='number'&&Number.isFinite(v)?clamp(v,lo,hi):fallback;
// A Govee lamp's colour calibration: slider positions, zero is neutral and not stored.
const CALIBRATION={warmth:[-100,100],tint:[-100,100],lowLight:[0,100]};
function calibrationOf(v){const c={};for(const [k,[lo,hi]] of Object.entries(CALIBRATION))c[k]=Math.round(finite(v?.[k],lo,hi,0));return c}
const calibrated=c=>!!c&&Object.keys(CALIBRATION).some(k=>c[k]);
function fromLayout(data){
 if(!data||typeof data!=='object'||![3,4,5,6].includes(data.version)||!Array.isArray(data.devices)||!data.desk||typeof data.desk!=='object')return null;
 const environment={width:finite(data.desk.width,80,240,160),height:finite(data.desk.height,50,120,85),inches:finite(data.monitorInches,19,55,32),
  zones:{sync:null,scene:null},fade:{sync:data.syncFalloff!==false,scene:data.sceneFalloff===true}};
 for(const [c,key] of [['sync','syncZone'],['scene','sceneZone']])if(validSyncZone(data[key]))environment.zones[c]=data[key].points.map(p=>({u:p.u,h:p.h}));
 const rows=new Map(data.devices.filter(r=>r&&typeof r.id==='string'&&r.positionCm&&typeof r.positionCm==='object').map(r=>[r.id,r]));
 if(SEED.some(s=>!rows.has(s.id)))return null;
 const place=(lamp,row)=>{
  lamp.x=finite(row.positionCm.x,-400,600,DESK_CENTER)-DESK_CENTER;
  const height=finite(row.positionCm.height,-400,600,0);
  lamp.h=lamp.form==='floor'?height-FLOOR_ANCHOR:height;
  lamp.scale=Math.round(finite(row.scale,0,100,lamp.scale));
  if(isRole(row.id)&&typeof row.name==='string'&&row.name.trim()){lamp.name=row.name.trim().slice(0,nameLimit(row.id));lamp.named=true}
  lamp.sceneScale=typeof row.sceneScale==='number'?Math.round(finite(row.sceneScale,0,100,lamp.scale)):null;
  lamp.calibration=calibrationOf(row.calibration);
  lamp.vertical=row.orientation==='vertical';lamp.enabled=row.enabled!==false;lamp.syncRole=SYNC_ROLES.includes(row.syncRole)&&(row.syncRole!=='off'||isRole(row.id))?row.syncRole:'sync';
  clampFixture(lamp,environment);return lamp;
 };
 const lamps=SEED.map(s=>place(seedLamp(s),rows.get(s.id)));
 for(const row of data.devices){
  if(lamps.length>=MAX_ROWS)break;
  const real=realDevice(row?.id),virtual=virtualDevice(row?.id);
  if(!real&&!virtual||!rows.has(row.id)||lamps.some(l=>l.id===row.id))continue;
  const shape=SHAPES.includes(row.shape)?row.shape:'bar';
  const zones=real&&Number.isInteger(row.zones)?clamp(row.zones,1,32):shape==='floor'?7:shape==='strip'?9:1;
  lamps.push(place({id:row.id,name:String(row.name??'New lamp').slice(0,real?80:35),model:real?String(row.model||'').slice(0,64):'Layout only',shape,form:formOf(row.id,shape),zones,unbound:true,scale:80},row));
 }
 return {room:typeof data.room==='string'&&data.room?data.room.slice(0,40):'Office',environment,lamps};
}
function pack(m={room,environment,lamps}){
 const env=m.environment;
 const zone=z=>z?{version:1,points:z.map(p=>({u:p.u,h:p.h}))}:null;
 return {version:6,gridStepCm:STEP,room:m.room,desk:{width:env.width,height:env.height},monitorInches:env.inches,
  syncZone:zone(env.zones.sync),sceneZone:zone(env.zones.scene),syncFalloff:env.fade.sync,sceneFalloff:env.fade.scene,
  devices:m.lamps.map(l=>({id:l.id,scale:l.scale,...(l.sceneScale!=null?{sceneScale:l.sceneScale}:{}),positionCm:{x:l.x+DESK_CENTER,height:l.form==='floor'?l.h+FLOOR_ANCHOR:l.h},orientation:l.vertical?'vertical':'horizontal',
   ...(l.enabled===false?{enabled:false}:{}),...(/^govee-/.test(l.id)&&calibrated(l.calibration)?{calibration:{...l.calibration}}:{}),...(syncRole(l)!=='sync'?{syncRole:syncRole(l)}:{}),...(isRole(l.id)&&l.named?{name:l.name}:{}),
   ...(realDevice(l.id)?{shape:l.shape,name:l.name,model:l.model,zones:l.zones}:virtualDevice(l.id)?{shape:l.shape,name:l.name}:{})}))};
}

let room,environment,lamps;
({room,environment,lamps}=fromLayout(readJson(KEY))||defaults());
let selected=null,mode=store.get(VIEW_KEY)==='sync'?'sync':'placement',past=[],future=[],drag=null,toastTimer;
const strip=()=>lamps.find(l=>l.id===MONITOR)||{x:0,h:110};
const activeLamps=()=>lamps.filter(l=>l.enabled!==false);
const syncRole=l=>SYNC_ROLES.includes(l.syncRole)?l.syncRole:'sync';
function movableFixture(){return selected==='monitor'?strip():lamps.find(l=>l.id===selected&&l.enabled!==false)}
function selectionFocus(){return selected==='monitor'?'[data-select="monitor"]':'[data-lamp="'+selected+'"]'}
function snapshot(){return JSON.stringify({lamps,environment})}
function remember(){past.push(snapshot());future=[];if(past.length>40)past.shift()}
function recordChange(before){if(snapshot()===before)return;past.push(before);if(past.length>40)past.shift();future=[]}
function restore(s){({lamps,environment}=JSON.parse(s));if(selected&&!lamps.some(l=>l.id===selected&&l.enabled!==false)&&!['desk','monitor','zone'].includes(selected))selected=null;render()}
function undo(){if(!past.length)return;future.push(snapshot());restore(past.pop());tell('Undone')}
function redo(){if(!future.length)return;past.push(snapshot());restore(future.pop())}
function tell(text){$('#toast').textContent=text;$('#toast').classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.remove('show'),1800)}

// ------------------------------------------------------------ live data --
let liveSnapshot=null,liveAt=-Infinity,liveRequest=null,liveGeneration=0,liveTimer,liveWatchdog,lastStatusKey='';
function liveFresh(){return !!liveSnapshot&&!document.hidden&&performance.now()-liveAt<LIVE_FRESH_MS}
const validRgb=c=>Array.isArray(c)&&c.length===3&&c.every(v=>Number.isInteger(v)&&v>=0&&v<=255);
function rgbHex(rgb){return '#'+rgb.map(v=>Math.round(clamp(v,0,255)).toString(16).padStart(2,'0')).join('')}
// Zone colours to draw (Govee bottom-to-top, strip nine anchors, Play one), or null when unlit.
// The Sync view without running Sync previews Sync on every Sync lamp from the demo picture.
function liveColors(l){return syncPreview()?demoColors(l):emittedColors(l)}
// What the lamp emits now, as the adapter reports it.
function emittedColors(l){
 if(l.unbound||l.enabled===false||!isRole(l.id)||!liveFresh()||lampBlocker(l))return null;
 const row=liveSnapshot.lamps&&typeof liveSnapshot.lamps==='object'?liveSnapshot.lamps[l.id]:null;
 if(!row||typeof row!=='object'||row.online!==true||row.on!==true||row.stale!==false||row.included===false)return null;
 // The view without Sync shows the lamps' own light only, even while Sync runs.
 if(row.source!=='scene-output'&&!(row.source==='sync-output'&&mode==='sync'))return null;
 if(!Array.isArray(row.colors)||!row.colors.length||!row.colors.every(validRgb))return null;
 const colors=Array.from({length:l.zones},(_,i)=>row.colors[row.colors.length===1?0:Math.min(i,row.colors.length-1)]);
 return colors.some(c=>c.some(v=>v>0))?colors:null;
}
const NEUTRAL_BEAM='#b5c7e6',NEUTRAL_RAY='#8e9cab';
// Light adds to the wall: a colour's hue is drawn at full strength and its
// brightness becomes opacity, so a dim zone glows faintly instead of painting it dark.
function light(c){const m=Math.max(c[0],c[1],c[2]);return m>0?{rgb:c.map(v=>v*255/m),level:Math.sqrt(m/255)}:{rgb:[0,0,0],level:0}}
const opacityText=v=>String(+v.toFixed(3));
function lampLevel(colors){return colors?Math.max(0,...colors.map(c=>light(c).level)):0}
// Average the emitted colours first, so a nearly-off zone barely shifts the hue, then take the hue.
function beamColor(colors){
 const lit=(colors||[]).filter(c=>c.some(v=>v>0));
 return lit.length?rgbHex(light([0,1,2].map(i=>lit.reduce((a,c)=>a+c[i],0)/lit.length)).rgb):NEUTRAL_BEAM;
}
// Each lens zone keeps its hue; its opacity is its brightness relative to the lamp's brightest zone.
function zoneStop(c,level){const z=light(c);return {color:rgbHex(z.rgb),opacity:level>0?opacityText(clamp(z.level/level)):'0'}}
function lampDark(l){return !liveColors(l)}
// With the demo picture every Sync lamp shows where it samples; with a live picture only lit lamps do.
function liveState(){
 if(!liveFresh())return 'unknown';
 const state=liveSnapshot.sync&&typeof liveSnapshot.sync==='object'?liveSnapshot.sync.state:undefined;
 if(liveSnapshot.connected!==true&&state!=='paused')return 'unknown';
 return ['active','waiting','foreign','off','paused'].includes(state)?state:'unknown';
}
const LAMP_ROLE=ROLES;
const BLOCKER_TEXT={owner_off:'off in HomeKit',color_fenced:'color still locked',native_off:'off',native_stale:'not responding',native_foreign:'another app in control'};
const BLOCKER_HINT={owner_off:'HomeKit requests Off, so Sync keeps it off',color_fenced:'The lamp is turning on; color is allowed once power is confirmed',native_off:'The lamp reports that it is off',native_stale:'No recent response from the lamp',native_foreign:'Another app, such as Hue Sync, is streaming to Hue'};
const blockerText=b=>BLOCKER_TEXT[b.code]||'not ready';
const blockerHint=b=>BLOCKER_HINT[b.code]||'The adapter reports a problem with this lamp';
const capital=t=>t[0].toUpperCase()+t.slice(1);
function lampBlocker(l){
 const role=LAMP_ROLE[l.id];
 if(!role||l.unbound||!liveFresh())return null;
 const list=liveSnapshot.sync&&Array.isArray(liveSnapshot.sync.blockers)?liveSnapshot.sync.blockers:[];
 return list.find(b=>b&&typeof b==='object'&&b.role===role&&typeof b.code==='string')||null;
}
// Waiting reasons with a clearer label; any other reason keeps "Waiting for frames".
const WAIT_LABEL={native_unavailable:'Hue not responding',starting:'Starting Sync'};
function globalHealth(){
 const state=liveState(),reason=liveFresh()&&liveSnapshot.sync?liveSnapshot.sync.reason:undefined;
 if(state==='active')return {level:'ok',short:'running',label:'Syncing',detail:'Lamps take color from the screen'};
 if(state==='off'||state==='paused')return {level:'idle',short:'not syncing',label:'Not syncing',detail:'Lamps keep their own light'};
 if(state==='foreign'||state==='waiting'&&reason==='native_foreign')return {level:'bad',short:'busy',label:'Hue busy',detail:'Close the other Hue app'};
 if(state==='waiting'){const label=typeof reason==='string'&&Object.hasOwn(WAIT_LABEL,reason)?WAIT_LABEL[reason]:'Waiting for frames';return {level:'wait',short:label.toLowerCase(),label,detail:'No image from the monitor'}}
 return {level:'unknown',short:'status unknown',label:'Sync status unknown',detail:'No reason reported yet'};
}
const levelColor={idle:'var(--unknown)',ok:'var(--ok)',wait:'var(--wait)',bad:'var(--bad)',unknown:'var(--unknown)'};
function blockerLevel(b){return b.code==='color_fenced'?'wait':'bad'}
function problemLamps(){return lamps.filter(l=>!l.unbound&&l.enabled!==false&&lampBlocker(l))}
function syncStatus(){
 if(!activeLamps().some(l=>!l.unbound))return {level:'idle',label:'No lamps',detail:'Nothing to sync'};
 const g=globalHealth(),p=problemLamps();
 if(g.level!=='ok')return {level:g.level,label:g.label,detail:g.detail,lamp:p[0]?.id};
 if(p.length){const worst=p.some(l=>blockerLevel(lampBlocker(l))==='bad')?'bad':'wait';return {level:worst,label:p.length===1?p[0].name+' '+blockerText(lampBlocker(p[0])):p.length+' lamps not lit',detail:p.length===1?'Click to show the lamp':p.map(l=>l.name).join(', '),lamp:p[0].id}}
 return {level:'ok',label:'Syncing',detail:'Lamps take color from the screen'};
}
function paintStatus(){
 const el=$('#sync-status');if(!el)return;const st=syncStatus(),label='Sync status: '+st.label;
 if(el.dataset.level!==st.level)el.dataset.level=st.level;
 el.style.setProperty('--s-color',levelColor[st.level]||'var(--unknown)');
 const text=el.querySelector('span');if(text.textContent!==st.label)text.textContent=st.label;
 if(el.getAttribute('aria-label')!==label)el.setAttribute('aria-label',label);el.style.cursor=st.lamp?'pointer':'default';
}

// ---------------------------------------------------- sync projection model --
const stripPoints=[[0,0],[0,.5],[0,1],[.25,1],[.5,1],[.75,1],[1,1],[1,.5],[1,0]];
// Mirrors sync_projection.screen_point.
function syncPoint(x,h){
 const m=strip(),w=monitorWidthCm(environment.inches),height=w*9/16;
 const u=clamp((x-m.x)/w+.5),v=clamp((h-m.h)/height+.5),edges=[u,1-u,v,1-v];
 const edge=edges.indexOf(Math.min(...edges));return [[0,v],[1,v],[u,0],[u,1]][edge];
}
// Mirrors sync_zone.default_zone in desk-centred coordinates.
function autoZonePoints(){
 const H=environment.height,half=environment.width/2,mx=strip().x,mh=environment.inches*2.54*8/Math.hypot(16,9),xs=activeLamps().map(l=>l.x);
 const left=Math.floor(Math.min(-half-50,mx-mh-20,...xs.map(x=>x-20))/5)*5,right=Math.ceil(Math.max(half+50,mx+mh+20,...xs.map(x=>x+20))/5)*5;
 return [{x:left,h:H},{x:right,h:H},{x:right,h:H-15-Math.max(0,right-half)},{x:half,h:H-15},{x:-half,h:H-15},{x:left,h:H-15-Math.max(0,-half-left)}];
}
// Two configurations: the Sync view edits the Sync one, the room view the one without Sync.
const view=()=>mode==='sync'?'sync':'scene';
const corr=(l,c=view())=>c==='sync'?l.scale:l.sceneScale??l.scale;
function setCorr(l,v){if(view()==='sync')l.scale=v;else l.sceneScale=v}
function zonePoints(c=view()){const z=environment.zones[c];return z?z.map(p=>({x:p.u*environment.width/2,h:environment.height+p.h})):autoZonePoints()}
function zoneConfig(points){return {version:1,points:points.map(p=>({u:p.x/(environment.width/2),h:p.h-environment.height}))}}
function zoneIntersections(P,x){
 const hs=[];
 P.forEach((a,i)=>{const b=P[(i+1)%P.length];if(x<Math.min(a.x,b.x)||x>Math.max(a.x,b.x))return;
  if(a.x===b.x)hs.push(a.h,b.h);else if(x===a.x)hs.push(a.h);else if(x===b.x)hs.push(b.h);else hs.push(a.h+(x-a.x)/(b.x-a.x)*(b.h-a.h))});
 return [...new Set(hs)].sort((a,b)=>a-b);
}
// The Sync fade switched off leaves every zone at full brightness.
function syncBounds(x){if(!environment.fade.sync)return null;const hs=zoneIntersections(zonePoints('sync'),x);return hs.length?[hs[0],hs[hs.length-1]]:null}
// The same validation as sync_zone.validate_zone, so a dragged corner is never refused by the server.
function validSyncZone(config){
 const exact=(value,keys)=>value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).length===keys.length&&keys.every(key=>Object.hasOwn(value,key));
 if(!exact(config,['version','points'])||config.version!==1||!Array.isArray(config.points)||config.points.length!==6)return false;
 if(!config.points.every(p=>exact(p,['u','h'])&&typeof p.u==='number'&&Number.isFinite(p.u)&&Math.abs(p.u)<=20&&typeof p.h==='number'&&Number.isFinite(p.h)&&Math.abs(p.h)<=400))return false;
 const p=config.points;
 if(new Set(p.map(point=>JSON.stringify([point.u,point.h]))).size!==6)return false;
 if(!(p[0].u<p[1].u&&p[5].u<p[4].u&&p[4].u<p[3].u&&p[3].u<p[2].u))return false;
 const slope=(p[1].h-p[0].h)/(p[1].u-p[0].u),top=x=>p[0].h+slope*(x-p[0].u);
 if(p.slice(2).some(point=>top(point.u)-point.h<1))return false;
 const cross=(a,b,c)=>(b.u-a.u)*(c.h-a.h)-(b.h-a.h)*(c.u-a.u);
 const on=(a,b,c)=>cross(a,b,c)===0&&c.u>=Math.min(a.u,b.u)&&c.u<=Math.max(a.u,b.u)&&c.h>=Math.min(a.h,b.h)&&c.h<=Math.max(a.h,b.h);
 for(let i=0;i<6;i++)for(let j=i+1;j<6;j++){
  if(j===i+1||(i===0&&j===5))continue;
  const a=p[i],b=p[(i+1)%6],c=p[j],d=p[(j+1)%6];
  const opposite=(a,b)=>(a<0&&b>0)||(b<0&&a>0);
  if((opposite(cross(a,b,c),cross(a,b,d))&&opposite(cross(c,d,a),cross(c,d,b)))||on(a,b,c)||on(a,b,d)||on(c,d,a)||on(c,d,b))return false;
 }
 if(p.reduce((area,a,i)=>{const b=p[(i+1)%6];return area+(a.u*b.h-b.u*a.h);},0)>=0)return false;
 const xs=[...new Set(p.map(point=>point.u))].sort((a,b)=>a-b);
 const sections=[...xs,...xs.slice(1).map((x,i)=>(xs[i]+x)/2)];
 const world=p.map(point=>({x:point.u,h:point.h}));
 return sections.every(x=>zoneIntersections(world,x).length<=2);
}
function syncGain(x,h){const b=syncBounds(x);if(!b)return 1;const [low,high]=b;if(high<=low)return h>=high?1:0;const t=clamp((h-low)/(high-low));return t*t*(3-2*t)}
// Mirrors sync_projection._segment_visibility.
function syncSegment(low,high,upper,fade){
 if(fade<=1e-9){const start=Math.max(low,upper);return high>start?[(high-start)/(high-low),(start+high)/2]:[0,null]}
 const lower=upper-fade;
 if(high<=lower)return [0,null];if(low>=upper)return [1,(low+high)/2];
 function integral(h){if(h<=lower)return [0,0];if(h>=upper)return [fade/2+h-upper,lower*fade/2+.35*fade*fade+(h*h-upper*upper)/2];const t=(h-lower)/fade;return [fade*(t**3-.5*t**4),lower*fade*(t**3-.5*t**4)+fade*fade*(.75*t**4-.4*t**5)]}
 const [a0,m0]=integral(low),[a1,m1]=integral(high),area=a1-a0;
 return area<=0?[0,null]:[clamp(area/(high-low)),clamp((m1-m0)/area,low,high)];
}
function syncProjection(l){
 if(l.unbound||l.enabled===false||syncRole(l)!=='sync')return [];
 if(l.form==='floor'){const b=syncBounds(l.x);return Array.from({length:7},(_,i)=>{const lo=l.h+6+i*128/7,hi=l.h+6+(i+1)*128/7,[gain,center]=b?syncSegment(lo,hi,b[1],b[1]-b[0]):[1,(lo+hi)/2];return {point:center===null?null:syncPoint(l.x,center),gain}})}
 if(l.id===MONITOR){const width=monitorWidthCm(environment.inches),height=width*9/16;return stripPoints.map(([u,v])=>({point:[u,v],gain:syncGain(l.x+(u-.5)*width,l.h+(v-.5)*height)}))}
 return [{point:syncPoint(l.x,l.h),gain:syncGain(l.x,l.h)}];
}
function syncSummary(l){const rows=syncProjection(l);return {rows,active:rows.filter(r=>r.gain>1e-8).length}}
function zoneColor(l,i){
 const colors=liveColors(l),c=colors?.[Math.min(i,colors.length-1)];return c&&c.some(v=>v>0)?rgbHex(light(c).rgb):NEUTRAL_RAY;
}

// ------------------------------------------------------------- drawing --
const glyph={floor:'<svg viewBox="0 0 22 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M11 3v17M6 22h10"/><path d="M11 3v13" stroke-width="3" stroke-linecap="round"/></svg>',bar:'<svg viewBox="0 0 22 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="9" width="18" height="6" rx="3"/></svg>',strip:'<svg viewBox="0 0 22 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 20V5h16v15"/></svg>',desk:'<svg viewBox="0 0 22 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M1 9h20M4 9v12M18 9v12"/></svg>',monitor:'<svg viewBox="0 0 22 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1" y="4" width="20" height="14" rx="2"/><path d="M11 18v4M6 22h10"/></svg>'};
const closeIcon='<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4"><path d="m5 5 10 10M15 5 5 15"/></svg>';
const trashIcon='<svg viewBox="-7 -7 14 14" aria-hidden="true"><path d="M-4.4 -3H4.4M-2.2 -3V-4.6H2.2V-3M-3.3 -3l.6 7.4H2.7l.6-7.4"/></svg>';
const plusIcon='<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M10 4v12M4 10h12"/></svg>';
const chipIcons={
 fade:'<path d="M-4.6 -1.5H4.6M-3.6 1.5H3.6M-2.2 4.4H2.2"/><path d="M-4.6 -4.4H4.6"/>',
 auto:'<path d="M-4 -1.5A4.2 4.2 0 1 1 -2.6 3.2M-4 -1.5V-5M-4 -1.5H-0.6"/>',
 rotate:'<path d="M-3.4 -2.6A4.2 4.2 0 1 1 -3.8 1.9M-3.4 -2.6V-5.8M-3.4 -2.6H-0.2"/>',
 more:'<circle class="dot" cx="-4" cy="0" r="1.1"/><circle class="dot" cx="0" cy="0" r="1.1"/><circle class="dot" cx="4" cy="0" r="1.1"/>',
 blink:'<path d="M2 0A2 2 0 1 1 -2 0A2 2 0 1 1 2 0M0 -5.6V-4M0 4V5.6M-5.6 0H-4M4 0H5.6M-4 -4l1.1 1.1M2.9 2.9 4 4M4 -4 2.9 -2.9M-2.9 2.9-4 4"/>',
 reset:'<path d="M-4 -1.5A4.2 4.2 0 1 1 -2.6 3.2M-4 -1.5V-5M-4 -1.5H-0.6"/>',
 remove:'<path d="M-4.4 -3H4.4M-2.2 -3V-4.6H2.2V-3M-3.3 -3l.6 7.4H2.7l.6-7.4"/>'
};
function iconSvg(id){return '<svg viewBox="-7 -7 14 14" aria-hidden="true">'+chipIcons[id]+'</svg>'}
function knob(kind,x,y,cursor,label){return '<g class="handle'+(drag?.handle===kind?' active':'')+'" data-handle="'+kind+'" style="cursor:'+cursor+'" data-tooltip="'+label+'" aria-label="'+label+'"><circle cx="'+x+'" cy="'+y+'" r="14" fill="transparent"/><circle class="knob" cx="'+x+'" cy="'+y+'" r="5.5"/></g>'}
function signed(v){return (v>0?'+':v<0?'−':'')+Math.abs(v)+' cm'}
function lampFrame(l){
 const x=500+l.x*3.3,y=740-l.h*3.3-(l.form==='floor'?224:0);
 if(l.form==='floor')return {x,y,left:x-25,right:x+25,top:y-239,bottom:y+238,ref:740-l.h*3.3};
 if(l.form==='strip'){const w=monitorWidthCm(environment.inches)*3.3/2,h=w*9/16;return {x,y,left:x-w-24,right:x+w+24,top:y-h-24,bottom:y+h+6,ref:y}}
 return l.vertical?{x,y,left:x-23,right:x+23,top:y-55,bottom:y+55,ref:y}:{x,y,left:x-55,right:x+55,top:y-23,bottom:y+23,ref:y};
}
function correctionTrack(l){const f=lampFrame(l);return l.form==='floor'?{tx:f.right+14,top:f.top+22,len:100}:{tx:f.right+14,top:f.top+18,len:56}}
function chipSvg(centerX,bottom,title,status,actions){
 const tw=title.length*6.6+18,sw=status?status.length*5.9+8:0,bw=22,w=tw+sw+actions.length*bw+(actions.length?2:0),x=centerX-w/2;
 let out='<g class="obj-chip" transform="translate('+x+' '+(bottom-22)+')"><rect width="'+w+'" height="22" rx="11"/><text x="10" y="14.8">'+esc(title)+(status?'<tspan class="chip-status" dx="7">'+esc(status)+'</tspan>':'')+'</text>';
 actions.forEach((a,i)=>{const cx=tw+sw+i*bw+bw/2-2;out+='<g class="chip-btn'+(a.id==='more'&&menuOpen?' open':'')+'" data-chip-action="'+a.id+'" data-tooltip="'+a.label+'" aria-label="'+a.label+'"><circle cx="'+cx+'" cy="11" r="9.5"/><g transform="translate('+cx+' 11)">'+chipIcons[a.id]+'</g></g>'});
 return out+'</g>';
}
function editAttrs(edit,value){return edit?' data-edit="'+edit+'" data-value="'+value+'" data-tooltip="Type an exact value"':''}
function tagSvg(x,y,text,edit,value,anchor='middle'){
 const w=text.length*6+8,x0=anchor==='end'?x-w:anchor==='start'?x:x-w/2;
 return '<g class="tag'+(edit?' editable':'')+'"'+editAttrs(edit,value)+'><rect class="tag-hit" x="'+(x0-3)+'" y="'+(y-10)+'" width="'+(w+6)+'" height="20" rx="6"/><text class="tag-text" x="'+(anchor==='end'?x-4:anchor==='start'?x+4:x)+'" y="'+(y+3.5)+'" text-anchor="'+anchor+'">'+esc(text)+'</text></g>';
}
function vDim(x,y1,y2){return Math.abs(y2-y1)<2?'':'<path class="dim" d="M'+(x-4)+' '+y1+'h8M'+(x-4)+' '+y2+'h8M'+x+' '+y1+'V'+y2+'"/>'}
function hDim(x1,x2,y){return Math.abs(x2-x1)<2?'':'<path class="dim" d="M'+x1+' '+(y-4)+'v8M'+x2+' '+(y-4)+'v8M'+x1+' '+y+'H'+x2+'"/>'}
function frameOf({cx,cy,mw,mh,w,left,top}){
 if(selected==='desk')return {left,right:left+w,top:top-8,bottom:top+18,x:500};
 if(selected==='monitor'){const mx=cx-mw/2,my=cy-mh/2;return {left:mx-24,right:mx+mw+24,top:my-24,bottom:my+mh+6,x:cx}}
 const l=lamps.find(l=>l.id===selected&&l.enabled!==false);return l?lampFrame(l):null;
}
function deskHeightX(left,top){
 let x=left-14;const frames=activeLamps().map(lampFrame).filter(f=>f.bottom>top&&f.top<740);
 for(let i=0;i<frames.length+1;i++){const hit=frames.find(f=>x>=f.left-8&&x<=f.right+8);if(!hit)break;x=hit.left-16}
 return x;
}
function monitorHalfCm(){return environment.inches*2.54*9/Math.hypot(16,9)/2}
function monitorGap(){return Math.round(strip().h-monitorHalfCm()-environment.height)}
function chipStatus(l){
 if(l.unbound)return 'not connected';
 const b=lampBlocker(l);if(b)return blockerText(b);
 if(mode!=='sync')return '';
 if(syncRole(l)==='own')return 'own light';
 if(syncRole(l)==='off')return 'off in Sync';
 const g=globalHealth();if(g.level!=='ok')return g.short;
 return syncSummary(l).active?'':'outside Sync';
}
function handlesSvg(geo){
 if(!selected)return '';
 if(selected==='zone')return '<g class="handles">'+zoneHandlesSvg()+'</g>';
 const {cx,cy,mw,mh,w,left,top}=geo,f=frameOf(geo);if(!f)return '';
 const s=strip(),l=lamps.find(l=>l.id===selected&&l.enabled!==false);
 const m=[];let knobs='',extra='',chip='',diag=null;
 if(selected==='desk'){
  m.push({axis:'h',x1:left,x2:left+w,y:top+44,text:environment.width+' cm',edit:'width',value:environment.width});
  const hx=deskHeightX(left,top);m.push({axis:'v',x:hx,y1:740,y2:top,text:environment.height+' cm',edit:'height',value:environment.height});
  knobs+=knob('desk-w',left,top+8,'ew-resize','Desk width')+knob('desk-w',left+w,top+8,'ew-resize','Desk width')+knob('desk-h',hx,top,'ns-resize','Tabletop height');
  chip=chipSvg(500,top-12,'Desk','',[]);
 }else if(selected==='monitor'){
  const mx=cx-mw/2,my=cy-mh/2,gap=monitorGap();
  m.push({axis:'v',x:f.left-12,y1:top,y2:my+mh,text:gap+' cm',edit:'gap',value:gap,leader:mx-4});
  m.push({axis:'h',x1:500,x2:cx,y:758,text:signed(s.x),edit:'x',value:s.x});
  knobs+=knob('mon-size',mx+mw,my+mh,'nwse-resize','Monitor diagonal');
  diag={x:mx+mw+12,y:my+mh,text:environment.inches+'″',edit:'inches',value:environment.inches};
  chip=chipSvg(cx,my-30,'Monitor','',[]);
 }else if(l){
  if(l.id!==MONITOR){
   m.push({axis:'v',x:f.left-12,y1:740,y2:f.ref,text:l.h+' cm',edit:'h',value:l.h,leader:f.left});
   m.push({axis:'h',x1:cx,x2:f.x,y:758,text:signed(l.x-s.x),edit:'x',value:l.x-s.x});
  }
  const t=correctionTrack(l),c=corr(l),ky=t.top+t.len*(1-c/100);
  knobs+='<line class="rail" x1="'+t.tx+'" y1="'+t.top+'" x2="'+t.tx+'" y2="'+(t.top+t.len)+'"/><line class="rail-fill" x1="'+t.tx+'" y1="'+ky+'" x2="'+t.tx+'" y2="'+(t.top+t.len)+'"/>'+knob('scale',t.tx,ky,'ns-resize','Brightness correction');
  extra+=tagSvg(t.tx,t.top+t.len+16,c+'%','corr',c);
  const actions=(l.shape==='bar'?[{id:'rotate',label:'Rotate 90°'}]:[]).concat([{id:'more',label:'Lamp settings'}]);
  chip=chipSvg(f.x,f.top-6,l.name,chipStatus(l),actions);
 }
 let lines='',labels='';
 for(const it of m){if(it.axis==='v'){lines+=vDim(it.x,it.y1,it.y2);if(it.leader!==undefined)lines+='<path class="dim leader" d="M'+it.x+' '+it.y2+'H'+it.leader+'"/>'}else lines+=hDim(it.x1,it.x2,it.y)}
 for(const it of m)labels+=it.axis==='v'?tagSvg(it.x-4,Math.abs(it.y2-it.y1)<2?it.y1-9:(it.y1+it.y2)/2,it.text,it.edit,it.value,'end'):tagSvg((it.x1+it.x2)/2,it.y+13,it.text,it.edit,it.value);
 if(diag)labels+=tagSvg(diag.x,diag.y,diag.text,diag.edit,diag.value,'start');
 return '<g class="handles">'+lines+labels+extra+knobs+chip+'</g>';
}
function zoneHandlesSvg(){
 const c=view(),P=zonePoints(),sx=x=>500+x*3.3,sy=h=>740-h*3.3,auto=!environment.zones[c],on=environment.fade[c];
 let out='';
 if(on)P.forEach((p,i)=>out+=knob('zp-'+i,sx(p.x),sy(p.h),'move','Zone corner · drag'));
 const actions=[{id:'fade',label:on?'Turn the fade off':'Turn the fade on'}].concat(on&&!auto?[{id:'auto',label:'Restore the automatic zone'}]:[]);
 return out+chipSvg((sx(P[0].x)+sx(-environment.width/2))/2,sy(P[0].h)-12,'Fade zone',on?auto?'auto':'':'off',actions);
}
function toggleFade(){const c=view();remember();environment.fade[c]=!environment.fade[c];render();tell(environment.fade[c]?'Fade on':'Fade off')}
function dragZone(kind,p){
 const x=Math.round((p.x-500)/3.3/5)*5,h=Math.round((740-p.y)/3.3/5)*5;
 const i=+kind.slice(3),P=zonePoints().map(q=>({...q}));P[i]={x,h};
 const config=zoneConfig(P);if(validSyncZone(config))environment.zones[view()]=config.points;
}
function resetZone(){const c=view();if(!environment.zones[c])return;remember();environment.zones[c]=null;renderScene();renderHistory();tell('The zone is automatic again')}
// A lamp that is drawn but not connected shows a broken link where a problem shows "!".
const UNLINKED_ICON='<path d="M-1.3 1.3 -2.4 2.4A1.8 1.8 0 0 1 -4.9 -0.1L-3.8 -1.2M1.3 -1.3 2.4 -2.4A1.8 1.8 0 0 1 4.9 0.1L3.8 1.2M-0.6 -3.4V-4.6M3.4 0.6H4.6"/>';
function healthMarkSvg(l){
 if(selected===l.id||l.enabled===false)return '';
 const f=lampFrame(l),cx=f.right-f.x-3,cy=f.top-f.y+3;
 if(l.unbound)return '<g class="health-badge unlinked" data-link-badge data-tooltip="Not connected"><circle cx="'+cx+'" cy="'+cy+'" r="7.5"/><g transform="translate('+cx+' '+cy+')">'+UNLINKED_ICON+'</g></g>';
 const b=lampBlocker(l);
 return '<g class="health-badge" data-badge visibility="'+(b?'visible':'hidden')+'" style="--h-color:'+(b?levelColor[blockerLevel(b)]:'var(--bad)')+'" data-tooltip="'+(b?esc(capital(blockerText(b))):'')+'"><circle cx="'+(f.right-f.x-3)+'" cy="'+(f.top-f.y+3)+'" r="7.5"/><text x="'+(f.right-f.x-3)+'" y="'+(f.top-f.y+6.6)+'">!</text></g>';
}
// The monitor strip glows left to right like the screen picture: left side, three top zones, right side.
function stripStops(colors){
 const cs=colors||Array.from({length:9},()=>[0,0,0]),avg=(...c)=>[0,1,2].map(i=>c.reduce((a,x)=>a+x[i],0)/c.length);
 return [avg(cs[0],cs[1],cs[2]),cs[3],cs[4],cs[5],avg(cs[6],cs[7],cs[8])];
}
function lensStops(l,colors,beam){
 if(l.form==='strip'){const level=lampLevel(colors);return stripStops(colors).map((c,i)=>{const z=zoneStop(c,level);return '<stop offset="'+i*25+'%" stop-color="'+z.color+'" stop-opacity="'+z.opacity+'"/>'}).join('')}
 if(l.zones>1){const cs=(colors||Array.from({length:l.zones},()=>[0,0,0])).slice().reverse(),n=cs.length,level=lampLevel(colors);return cs.map((c,i)=>{const z=zoneStop(c,level),a='" stop-color="'+z.color+'" stop-opacity="'+z.opacity+'"/>';return '<stop offset="'+(i/n*100)+'%'+a+'<stop offset="'+((i+1)/n*100)+'%'+a}).join('')}
 return '<stop stop-color="#fff8ed"/><stop offset=".48" stop-color="'+beam+'"/><stop offset="1" stop-color="'+beam+'" stop-opacity=".65"/>';
}
function lensAxis(l){
 if(l.form!=='strip')return 'x1="0" y1="0" x2="0" y2="1"';
 const w=monitorWidthCm(environment.inches)*3.3/2+11;return 'gradientUnits="userSpaceOnUse" x1="'+(-w)+'" y1="0" x2="'+w+'" y2="0"';
}
function lampSvg(l){
 const x=500+l.x*3.3,y=740-l.h*3.3-(l.form==='floor'?224:0);
 const colors=liveColors(l),beam=beamColor(colors),power=opacityText(lampLevel(colors));
 let box='',body='',wash='',off='',lens='',hit='';
 const glowOpacity=.21;
 if(l.form==='floor'){
  box='x="-25" y="-239" width="50" height="477"';
  body='<ellipse class="contact-shadow" cx="5" cy="225" rx="28" ry="5"/><ellipse fill="url(#body-metal)" stroke="#526075" stroke-width="1" cx="0" cy="220" rx="20" ry="5"/><path d="M0 218v-17" stroke="#49566c" stroke-width="5"/><rect x="-7.5" y="-225" width="15" height="432" rx="7.5" fill="url(#body-metal)" stroke="#718097" stroke-width=".8"/><path class="object-edge" d="M-5 -215V196"/>';
  wash='<path class="render-wash" opacity="'+glowOpacity+'" d="M-3 208 Q-115 -2 -92 -250 Q0 -306 92 -250 Q115 -2 3 208Z"/><ellipse class="render-wash" opacity="'+glowOpacity+'" cx="0" cy="-70" rx="43" ry="180"/>';
  off='<rect class="lens-off" x="-3.5" y="-220" width="7" height="420" rx="3.5"/>';
  lens='<path class="render-bloom" stroke-width="35" opacity=".3" d="M0 198V-218"/><rect x="-3.5" y="-220" width="7" height="420" rx="3.5" fill="url(#lens-'+l.id+')"/>';
 }else if(l.form==='strip'){
  const w=monitorWidthCm(environment.inches)*3.3/2,h=w*9/16;
  box='x="'+(-w-24)+'" y="'+(-h-24)+'" width="'+(2*w+48)+'" height="'+(2*h+48)+'"';
  const path='M'+(-w-11)+' '+h+'V'+(-h-11)+'H'+(w+11)+'V'+h;
  hit='<path class="hit" d="'+path+'" style="fill:none;stroke:transparent;stroke-width:25;pointer-events:stroke"/>';
  body='<path d="'+path+'" fill="none" stroke="#58667a" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/>';
  wash='<path class="render-bloom" stroke-width="94" opacity="'+(glowOpacity*1.6)+'" d="'+path+'" style="stroke:url(#lens-'+l.id+')"/>';
  off='<path class="lens-off" stroke-width="5" d="'+path+'"/>';
  lens='<path class="render-emission" stroke-width="5" d="'+path+'" style="stroke:url(#lens-'+l.id+')"/><path d="'+path+'" fill="none" stroke="#fff3df" stroke-opacity=".6" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>';
 }else{
  box=l.vertical?'x="-23" y="-55" width="46" height="110"':'x="-55" y="-23" width="110" height="46"';
  const rotation=l.vertical?-90:0;
  body='<g transform="rotate('+rotation+')"><ellipse class="contact-shadow" cx="3" cy="13" rx="43" ry="5"/><rect x="-41" y="-12" width="82" height="24" rx="10" fill="url(#body-metal)" stroke="#64738a" stroke-width="1"/><path class="object-edge" d="M-30 -9h60"/></g>';
  wash='<g transform="rotate('+rotation+')"><path class="render-wash" opacity="'+(glowOpacity*1.4)+'" d="M-32 -2 -105 -105 Q0 -163 105 -105 L32 -2Z"/></g>';
  off='<g transform="rotate('+rotation+')"><rect class="lens-off" x="-35" y="-7" width="70" height="13" rx="6.5"/></g>';
  lens='<g transform="rotate('+rotation+')"><rect x="-35" y="-7" width="70" height="13" rx="6.5" fill="url(#lens-'+l.id+')"/><path class="render-bloom" stroke-width="29" opacity=".25" d="M-30 -2H30"/></g>';
 }
 const mark=healthMarkSvg(l);
 return '<g class="lamp '+(selected===l.id?'chosen':'')+'" data-lamp="'+l.id+'" transform="translate('+x+' '+y+')" role="button" tabindex="0" aria-label="'+esc(l.name)+'"><g data-beam style="--beam:'+beam+'" pointer-events="none"><defs><linearGradient id="lens-'+l.id+'" '+lensAxis(l)+'>'+lensStops(l,colors,beam)+'</linearGradient></defs><g data-sync-power opacity="'+power+'">'+wash+'</g>'+body+off+'<g data-sync-power opacity="'+power+'">'+lens+'</g></g>'+(hit||'<rect class="hit" '+box+' rx="8"/>')+'<rect class="outline" '+box+' rx="8" pointer-events="none"/>'+mark+'</g>';
}
const BEZEL=5;
function environmentSvg({cx,cy,mw,mh,w,left,top}){
 const right=left+w,bottom=cy+mh/2,mx=cx-mw/2,my=cy-mh/2;
 const defs='<defs><linearGradient id="wood-face" x1="0" y1="0" x2="0" y2="1"><stop stop-color="var(--wood-top)"/><stop offset="1" stop-color="var(--wood-front)"/></linearGradient><linearGradient id="glass-face" x1="0" y1="0" x2="1" y2="1"><stop stop-color="var(--glass-top)"/><stop offset=".5" stop-color="var(--glass-bottom)"/><stop offset="1" stop-color="var(--glass-top)"/></linearGradient><filter id="furniture-shadow" x="-100%" y="-200%" width="300%" height="500%"><feGaussianBlur stdDeviation="5"/></filter></defs>';
 let desk='<ellipse cx="500" cy="741" rx="'+(w/2-3)+'" ry="5" fill="#243247" opacity=".08" filter="url(#furniture-shadow)"/>';
 for(const x of [left+15,right-27])desk+='<rect x="'+x+'" y="'+(top+13)+'" width="12" height="'+(740-top-15)+'" rx="2" fill="var(--furniture-metal)"/><path d="M'+(x+1)+' '+(top+22)+'V736" stroke="#ffffff" stroke-opacity=".18"/><rect x="'+(x-9)+'" y="736" width="30" height="4" rx="2" fill="var(--furniture-metal)"/>';
 desk+='<rect x="'+(left+17)+'" y="'+(top+23)+'" width="'+(w-34)+'" height="5" rx="2" fill="var(--furniture-metal)" opacity=".55"/><rect x="'+left+'" y="'+top+'" width="'+w+'" height="16" rx="3" fill="url(#wood-face)"/><path d="M'+(left+3)+' '+(top+1)+'H'+(right-3)+'" stroke="var(--wood-edge)" stroke-width="2"/><path d="M'+(left+8)+' '+(top+8)+'H'+(right-8)+'M'+(left+17)+' '+(top+11)+'H'+(right-28)+'" stroke="#70451f" stroke-opacity=".12" stroke-width=".6"/>';
 // The diagonal measures the picture, so the bezel is drawn outside it and Sync samples sit on the picture's edge.
 const B=BEZEL;
 const monitor='<rect x="'+(mx-B)+'" y="'+(my-B)+'" width="'+(mw+2*B)+'" height="'+(mh+2*B)+'" rx="7" fill="#445367" stroke="#77879b" stroke-width="1"/><rect x="'+mx+'" y="'+my+'" width="'+mw+'" height="'+mh+'" rx="3" fill="url(#glass-face)"/><path d="M'+(mx+3)+' '+(my+1)+'H'+(mx+mw-3)+'" stroke="#eaf5ff" stroke-opacity=".4"/><path d="M'+cx+' '+(bottom+B)+'v'+Math.max(0,top-bottom-B-9)+'" stroke="var(--furniture-metal)" stroke-width="5"/><path d="M'+cx+' '+(top-10)+'l-31 7h62Z" fill="var(--furniture-metal)"/><circle cx="'+cx+'" cy="'+(my+mh+B/2)+'" r=".9" fill="#c8d7e9" opacity=".5"/>';
 const deskHit='<rect x="'+(left-5)+'" y="'+(top-12)+'" width="'+(w+10)+'" height="45" fill="transparent"/>';
 return defs+'<g class="desk-group '+(selected==='desk'?'chosen':'')+'" data-select="desk" tabindex="0" role="button" aria-label="Desk">'+desk+'<path class="furniture-select" d="M'+left+' '+(top-6)+'H'+right+'"/>'+deskHit+'</g><g class="monitor-group '+(selected==='monitor'?'chosen':'')+(selected===MONITOR?' paired':'')+'" data-select="monitor" tabindex="0" role="button" aria-label="Monitor">'+monitor+'<rect class="furniture-select" x="'+(mx-3-BEZEL)+'" y="'+(my-3-BEZEL)+'" width="'+(mw+6+2*BEZEL)+'" height="'+(mh+6+2*BEZEL)+'" rx="9"/></g>';
}
// The monitor picture in the Sync view: a soft gradient of the strip's emitted colours
// while Sync runs; otherwise a fixed radial demo picture that the lamps preview.
function liveScreenStops(){
 const colors=liveState()==='active'?emittedColors(strip()):null;
 return colors&&colors.length>=9?stripStops(colors).map(rgbHex):null;
}
const syncPreview=()=>mode==='sync'&&liveState()!=='active';
const screenDemo=()=>mode==='sync'&&!liveScreenStops();
const screenStops=()=>liveScreenStops()||Array(5).fill(SCREEN_DARK);
const SCREEN_DARK='#1d222b',DEMO_WEDGES=90;
// The demo picture is a colour wheel around the screen centre, so every point of the
// perimeter, and so every lamp zone, gets its own hue. u runs left to right, v bottom to top.
function demoHue(u,v){const a=Math.atan2((v-.5)*2,(u-.5)*2)*180/Math.PI;return (a+360)%360}
function hueRgb(hue){
 const s=.62,l=.55,c=(1-Math.abs(2*l-1))*s,x=c*(1-Math.abs(hue/60%2-1)),m=l-c/2;
 const [r,g,b]=hue<60?[c,x,0]:hue<120?[x,c,0]:hue<180?[0,c,x]:hue<240?[0,x,c]:hue<300?[x,0,c]:[c,0,x];
 return [r,g,b].map(v=>(v+m)*255);
}
const demoColor=(u,v)=>rgbHex(hueRgb(demoHue(u,v)));
// What Sync would send each zone from the demo picture: its colour where the zone samples,
// dimmed by the zone's falloff gain and the lamp's correction.
function demoColors(l){
 if(!isRole(l.id))return null;
 const rows=syncProjection(l);if(!rows.length)return null;
 const rgb=hex=>[1,3,5].map(k=>parseInt(hex.slice(k,k+2),16)),k=l.scale/100;
 const colors=rows.map(row=>row.point&&row.gain>0?rgb(demoColor(row.point[0],row.point[1])).map(v=>Math.round(v*row.gain*k)):[0,0,0]);
 return colors.some(c=>c.some(v=>v>0))?colors:null;
}
// The demo watermark is .2 of the screen height with .3em tracking; tracking trails the last letter,
// so the anchor moves right by half of it (.03 of the height) to centre the letters.
// Wedges in the screen's normalised space, so a wedge's hue is the hue demoColor gives its points.
function demoWheelSvg(cx,cy,mw,mh){
 let out='';
 for(let i=0;i<DEMO_WEDGES;i++){
  const a0=i*360/DEMO_WEDGES,a1=(i+1)*360/DEMO_WEDGES,p=a=>{const t=a*Math.PI/180;return (cx+Math.cos(t)*mw)+' '+(cy-Math.sin(t)*mh)};
  const color=rgbHex(hueRgb((a0+a1)/2));
  out+='<path d="M'+cx+' '+cy+'L'+p(a0)+'L'+p(a1)+'Z" fill="'+color+'" stroke="'+color+'" stroke-width=".8"/>';
 }
 return out;
}
function syncScreenSvg({cx,cy,mw,mh}){
 const stops=screenStops(),offsets=[0,25,50,75,100],x=cx-mw/2,y=cy-mh/2,w=mw,h=mh,demo=screenDemo();
 return '<g id="sync-screen" pointer-events="none"><defs><clipPath id="screen-clip"><rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" rx="3"/></clipPath><linearGradient id="screen-colors" x1="0" y1="0" x2="1" y2="0">'+offsets.map((o,i)=>'<stop offset="'+o+'%" stop-color="'+stops[i]+'"/>').join('')+'</linearGradient><linearGradient id="screen-shade" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0f1c2a" stop-opacity="0"/><stop offset="1" stop-color="#0f1c2a" stop-opacity=".45"/></linearGradient></defs><rect class="screen-off" x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" rx="3"/><rect id="screen-image" x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" rx="3" fill="url(#screen-colors)" opacity="'+(demo?0:1)+'"/><g id="screen-demo-image" clip-path="url(#screen-clip)" opacity="'+(demo?1:0)+'">'+demoWheelSvg(cx,cy,mw,mh)+'</g><rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" rx="3" fill="url(#screen-shade)"/><text class="screen-demo" x="'+(cx+h*.03)+'" y="'+cy+'" font-size="'+(+(h*.2).toFixed(1))+'" text-anchor="middle" dominant-baseline="central" opacity="'+(demo?1:0)+'" data-screen-demo>Demo</text></g>';
}
function syncZones(l,{cx,cy,mw,mh}){
 const x=500+l.x*3.3;
 return syncProjection(l).map((row,i)=>{
  const y=740-(l.form==='floor'?l.h+6+(i+.5)*128/7:l.h)*3.3;
  if(!row.point||row.gain<=0)return {x,y,on:false};
  const [u,v]=row.point;
  return {x,y,on:true,u,v,sx:cx+(u-.5)*mw,sy:cy+(.5-v)*mh,c:zoneColor(l,i)};
 });
}
function rayMarkup(l,z,i){return '<path class="ray" data-k="'+l.id+':r'+i+'" stroke="'+z.c+'" d="M'+z.x+' '+z.y+'L'+z.sx+' '+z.sy+'"/><rect class="zone-tick" data-k="'+l.id+':t'+i+'" x="'+(z.x-3)+'" y="'+(z.y-3)+'" width="6" height="6" rx="1.5" fill="'+z.c+'"/>'}
function sampleMarkup(l,z,i){return '<circle class="sample" data-k="'+l.id+':s'+i+'" cx="'+z.sx+'" cy="'+z.sy+'" r="3.5" fill="'+z.c+'"/>'}
function syncLinksSvg(geo){
 let out='<g id="sync-links" pointer-events="none">';
 if(selected==='monitor'){
  for(const l of lamps){
   if(l.id===MONITOR||l.unbound||l.enabled===false||lampDark(l))continue;
   syncZones(l,geo).forEach((z,i)=>{if(z.on)out+=rayMarkup(l,z,i)+sampleMarkup(l,z,i)});
  }
  return out+'</g>';
 }
 const l=lamps.find(l=>l.id===selected);if(!l||l.unbound)return '';
 syncZones(l,geo).forEach((z,i)=>{
  if(!z.on){if(l.id!==MONITOR)out+='<circle class="zone-off" cx="'+z.x+'" cy="'+z.y+'" r="2.5"/>';return}
  if(l.id!==MONITOR)out+=rayMarkup(l,z,i);
  out+=sampleMarkup(l,z,i);
 });
 return out+'</g>';
}
// The fade zone of the current view; switched off, it stays as a faint outline to select and turn on.
function syncVisibilitySvg(){
 const P=zonePoints(),sx=x=>500+x*3.3,sy=h=>740-h*3.3,pt=p=>sx(p.x)+' '+sy(p.h),on=environment.fade[view()];
 let out='<g id="sync-visibility" class="zone-group'+(selected==='zone'?' chosen':'')+(on?'':' fade-off')+'" data-select="zone" role="button" aria-label="Fade zone">';
 out+='<path class="zone-hit" pointer-events="all" d="M'+P.map(pt).join('L')+'Z"/>';
 out+='<path class="fade-line" pointer-events="none" d="M'+pt(P[0])+'L'+pt(P[1])+'"/>';
 out+='<path class="fade-line zero" pointer-events="none" d="M'+pt(P[2])+'L'+pt(P[3])+'L'+pt(P[4])+'L'+pt(P[5])+'"/>';
 out+='<path class="fade-line side" pointer-events="none" d="M'+pt(P[1])+'L'+pt(P[2])+'M'+pt(P[5])+'L'+pt(P[0])+'"/>';
 if(on)out+='<g pointer-events="none">'+tagSvg(sx(P[1].x)+4,sy(P[1].h),'100%','',0,'start')+tagSvg(sx(P[2].x)+4,sy(P[2].h),'0%','',0,'start')+'</g>';
 return out+'</g>';
}
function sceneGeometry(){const s=strip(),cx=500+s.x*3.3,cy=740-s.h*3.3,mw=monitorWidthCm(environment.inches)*3.3,mh=mw*9/16,w=environment.width*3.3;return {cx,cy,mw,mh,w,left:500-w/2,top:740-environment.height*3.3}}

// ------------------------------------------------------------ render --
let focusState=new Map(),skipClick=false,menuOpen=false;
function focusKey(n){return n.dataset.lamp||n.dataset.washFor||n.dataset.select}
function applyFocus(){
 const on=!!selected&&drag?.handle!=='scale';
 const keep=new Set([selected]);if(selected==='monitor'){keep.add(MONITOR);if(mode==='sync')lamps.forEach(l=>{if(!l.unbound&&l.enabled!==false&&!lampDark(l)&&syncSummary(l).active)keep.add(l.id)})}if(selected===MONITOR)keep.add('monitor');
 const next=new Map(),changed=[],nodes=$('#stage').querySelectorAll('.lamp,.desk-group,.monitor-group');
 nodes.forEach(n=>{
  const key=focusKey(n),want=on&&!keep.has(key)?.22:1,was=focusState.has(key)?focusState.get(key):want;
  next.set(key,want);n.style.transition='none';n.style.opacity=was;if(was!==want)changed.push([n,want]);
 });
 focusState=next;
 requestAnimationFrame(()=>{nodes.forEach(n=>n.style.transition='');requestAnimationFrame(()=>changed.forEach(([n,want])=>n.style.opacity=want))});
}
function renderBaseScene(){
 const g=sceneGeometry(),{cx}=g;
 let out='<defs><linearGradient id="body-metal" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#344257"/><stop offset=".22" stop-color="#73829a"/><stop offset=".48" stop-color="#a6b2c3"/><stop offset=".62" stop-color="#738198"/><stop offset="1" stop-color="#344257"/></linearGradient><filter id="wall-wash" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="35"/></filter><filter id="bloom" x="-200%" y="-100%" width="500%" height="300%"><feGaussianBlur stdDeviation="23"/></filter></defs><path class="floor" d="M70 744H930"/>';
 out+='<path class="desk-axis" d="M500 120V744"/>';if(selected&&Math.abs(cx-500)>1)out+='<path class="axis" d="M'+cx+' 170V744"/>';
 out+=syncVisibilitySvg();
 out+=environmentSvg(g)+(mode==='sync'?syncScreenSvg(g):'')+activeLamps().map(lampSvg).join('')+(mode==='sync'?syncLinksSvg(g):'')+handlesSvg(g);
 $('#stage').innerHTML=out;applyFocus();updateStageViewport();
}
const RAY_MS=280;
let rayBorn=new Map(),rayGhosts=[];
function rayElements(root){return root?[...root.querySelectorAll('[data-k]:not(.ray-ghost)')]:[]}
function renderScene(){
 const before=new Map(rayElements($('#sync-links')).map(n=>[n.dataset.k,n.outerHTML]));
 renderBaseScene();paintStatus();paintTitle();layerWashes();animateRays(before);paintPanel();paintEmptyRoom();
}
function animateRays(prev){
 const now=performance.now(),links=$('#sync-links'),cur=new Map(rayElements(links).map(n=>[n.dataset.k,n]));
 const reduce=matchMedia('(prefers-reduced-motion:reduce)').matches;
 cur.forEach((n,k)=>{if(!rayBorn.has(k))rayBorn.set(k,now);const age=now-rayBorn.get(k);if(!reduce&&age<RAY_MS){n.style.animation='ray-in '+RAY_MS+'ms ease both';n.style.animationDelay=(-age)+'ms'}});
 prev.forEach((html,k)=>{if(!cur.has(k)){rayGhosts.push({k,html,dead:now});rayBorn.delete(k)}});
 [...rayBorn.keys()].forEach(k=>{if(!cur.has(k))rayBorn.delete(k)});
 rayGhosts=rayGhosts.filter(g=>now-g.dead<RAY_MS&&!cur.has(g.k));
 if(reduce||!rayGhosts.length)return;
 let host=links;if(!host){host=document.createElementNS('http://www.w3.org/2000/svg','g');host.id='sync-links';host.setAttribute('pointer-events','none');$('#stage').append(host)}
 rayGhosts.forEach(g=>{
  const box=document.createElementNS('http://www.w3.org/2000/svg','g');box.innerHTML=g.html;const el=box.firstElementChild;if(!el)return;
  el.classList.add('ray-ghost');el.style.animation='ray-out '+RAY_MS+'ms ease both';el.style.animationDelay=(-(now-g.dead))+'ms';
  host.append(el);setTimeout(()=>el.remove(),RAY_MS-(now-g.dead)+20);
 });
}
function updateStageViewport(){
 if(drag)return;
 const m=strip(),half=environment.inches*2.54*9/Math.hypot(16,9)*3.3/2;
 const top=Math.min(90,740-m.h*3.3-half-28,...activeLamps().map(l=>740-l.h*3.3-(l.form==='floor'?470:l.vertical?65:35)));
 // Widen the room symmetrically so a lamp placed far from the desk stays visible.
 const reach=Math.max(500,...activeLamps().map(l=>Math.abs(l.x)*3.3+90));
 $('#stage').setAttribute('viewBox',(500-reach)+' '+top+' '+(2*reach)+' '+(830-top));
}
function syncScreenAxis(){const g=sceneGeometry(),stage=$('#stage'),ax=stage.querySelector('.axis'),need=!!selected&&Math.abs(g.cx-500)>1,d='M'+g.cx+' 170V744';if(!need){ax?.remove();return}if(ax){ax.setAttribute('d',d);return}const p=document.createElementNS('http://www.w3.org/2000/svg','path');p.setAttribute('class','axis');p.setAttribute('d',d);stage.querySelector('.desk-axis')?.after(p)}
function refreshHandles(washes=true){syncScreenAxis();const old=$('#stage').querySelector('.handles'),box=document.createElementNS('http://www.w3.org/2000/svg','g');box.innerHTML=handlesSvg(sceneGeometry());const fresh=box.firstElementChild;if(old&&fresh)old.replaceWith(fresh);else if(fresh)$('#stage').append(fresh);else old?.remove();if(washes)syncWashes()}
function renderHistory(){$('#undo').disabled=!past.length;$('#redo').disabled=!future.length;persistIfChanged()}
function renderList(){$('#count').textContent=activeLamps().length}
function render(){renderScene();renderInspector();renderList();renderHistory()}
function renderInspector(){if(menuOpen&&!lamps.some(l=>l.id===selected))closeObjectMenu()}
function paintTitle(){const node=$('#room-name');if(node&&node.textContent!==room)node.textContent=room}
function paintEmptyRoom(){$('#empty-room').hidden=activeLamps().length>0}

// Wall washes sit behind every object, in one layer after the scene defs.
function syncWashes(){
 const stage=$('#stage');
 stage.querySelectorAll('#wash-layer [data-wash-for]').forEach(w=>{
  const lamp=stage.querySelector('[data-lamp="'+w.dataset.washFor+'"]');if(!lamp){w.remove();return}
  w.setAttribute('transform',lamp.getAttribute('transform')||'');
  paintWash(w,lamp);
 });
 maskWashes();
}
function paintWash(w,lamp){
 const lit=lamp.querySelector('[data-beam]');if(lit&&w.style.getPropertyValue('--beam')!==lit.style.getPropertyValue('--beam'))w.style.setProperty('--beam',lit.style.getPropertyValue('--beam'));
 const power=lamp.querySelector('[data-sync-power]');if(power)[...w.children].forEach(g=>{if(g.tagName.toLowerCase()==='g'&&g.getAttribute('opacity')!==power.getAttribute('opacity'))g.setAttribute('opacity',power.getAttribute('opacity'))});
}
function layerWashes(){
 const stage=$('#stage'),NS='http://www.w3.org/2000/svg';
 stage.querySelector('#wash-layer')?.remove();
 const layer=document.createElementNS(NS,'g');layer.id='wash-layer';layer.setAttribute('pointer-events','none');
 const defs=stage.querySelector(':scope > defs');if(defs)defs.after(layer);else stage.prepend(layer);
 stage.querySelectorAll('.lamp[data-lamp]').forEach(n=>{
  const lit=n.querySelector('[data-beam]'),wash=lit?.querySelector(':scope > g[data-sync-power]');if(!wash)return;
  const w=document.createElementNS(NS,'g');w.setAttribute('class','lamp lamp-wash');w.dataset.washFor=n.dataset.lamp;
  w.setAttribute('transform',n.getAttribute('transform')||'');w.style.setProperty('--beam',lit.style.getPropertyValue('--beam'));
  w.style.opacity=n.style.opacity;wash.removeAttribute('data-sync-power');w.append(wash);
  const lens=lit.querySelector(':scope > g[data-sync-power]'),blooms=lens?[...lens.querySelectorAll('.render-bloom')]:[];
  if(blooms.length){const glow=document.createElementNS(NS,'g');glow.setAttribute('opacity',lens.getAttribute('opacity'));
   blooms.forEach(b=>{const parent=b.parentNode;if(parent!==lens&&parent.getAttribute('transform')){const t=document.createElementNS(NS,'g');t.setAttribute('transform',parent.getAttribute('transform'));t.append(b);glow.append(t)}else glow.append(b)});
   w.append(glow)}
  layer.append(w);
 });
 requestAnimationFrame(()=>requestAnimationFrame(()=>layer.querySelectorAll('[data-wash-for]').forEach(w=>{if(focusState.has(w.dataset.washFor))w.style.opacity=focusState.get(w.dataset.washFor)})));
 maskWashes();
}
// In Sync a floor lamp lights the wall only next to its lit zones.
function washMask(w){
 const NS='http://www.w3.org/2000/svg',l=lamps.find(l=>l.id===w.dataset.washFor),id='wash-mask-'+w.dataset.washFor;
 let defs=$('#wash-masks');
 const rows=l&&l.form==='floor'&&mode==='sync'?syncSummary(l).rows:[];
 if(rows.length<2){w.removeAttribute('mask');defs?.querySelector('[id="'+id+'"]')?.remove();return}
 if(!defs){defs=document.createElementNS(NS,'defs');defs.id='wash-masks';$('#wash-layer').prepend(defs)}
 const n=rows.length,stops=rows.map((r,i)=>'<stop offset="'+((i+.5)/n).toFixed(4)+'" stop-color="#fff" stop-opacity="'+clamp(Math.max(0,r.gain)).toFixed(3)+'"/>').join('');
 let mask=defs.querySelector('[id="'+id+'"]');
 if(!mask){mask=document.createElementNS(NS,'mask');mask.id=id;mask.setAttribute('maskUnits','userSpaceOnUse');mask.setAttribute('maskContentUnits','userSpaceOnUse');['x','y','width','height'].forEach((k,i)=>mask.setAttribute(k,[-400,-520,800,1040][i]));defs.append(mask)}
 mask.innerHTML='<linearGradient id="'+id+'-g" gradientUnits="userSpaceOnUse" x1="0" y1="200" x2="0" y2="-220">'+stops+'</linearGradient><rect x="-400" y="-520" width="800" height="1040" fill="url(#'+id+'-g)"/>';
 w.setAttribute('mask','url(#'+id+')');
}
function maskWashes(){document.querySelectorAll('#wash-layer [data-wash-for]').forEach(washMask)}

// ------------------------------------------------- live paint (no rebuild) --
function setAttr(node,key,value){if(node&&node.getAttribute(key)!==String(value))node.setAttribute(key,String(value))}
function setVar(node,key,value){if(node&&node.style.getPropertyValue(key)!==value)node.style.setProperty(key,value)}
function statusKey(){
 return JSON.stringify([syncStatus(),lamps.map(l=>[l.id,!!lampBlocker(l)&&blockerText(lampBlocker(l)),lampDark(l)]),globalHealth().level,screenDemo(),syncPreview()]);
}
// Telemetry changes existing SVG attributes only: no layout rebuild, no save.
function paintLive(){
 const stage=$('#stage');
 for(const l of activeLamps()){
  const node=stage.querySelector('[data-lamp="'+l.id+'"]');if(!node)continue;
  const colors=liveColors(l),beam=beamColor(colors);
  setVar(node.querySelector('[data-beam]'),'--beam',beam);
  const level=lampLevel(colors);
  node.querySelectorAll('[data-sync-power]').forEach(g=>setAttr(g,'opacity',opacityText(level)));
  const stops=node.querySelectorAll('stop');
  if(l.form==='strip')stripStops(colors).forEach((c,i)=>{const z=zoneStop(c,level);setAttr(stops[i],'stop-color',z.color);setAttr(stops[i],'stop-opacity',z.opacity)});
  else if(l.zones>1){const cs=(colors||Array.from({length:l.zones},()=>[0,0,0])).slice().reverse();cs.forEach((c,i)=>{const z=zoneStop(c,level);for(const stop of [stops[2*i],stops[2*i+1]]){setAttr(stop,'stop-color',z.color);setAttr(stop,'stop-opacity',z.opacity)}})}
  else{setAttr(stops[1],'stop-color',beam);setAttr(stops[2],'stop-color',beam)}
  const badge=node.querySelector('[data-badge]'),b=lampBlocker(l);
  if(badge){setAttr(badge,'visibility',b?'visible':'hidden');setVar(badge,'--h-color',b?levelColor[blockerLevel(b)]:'var(--bad)');setAttr(badge,'data-tooltip',b?capital(blockerText(b)):'')}
 }
 stage.querySelectorAll('#wash-layer [data-wash-for]').forEach(w=>{const lamp=stage.querySelector('[data-lamp="'+w.dataset.washFor+'"]');if(lamp)paintWash(w,lamp)});
 const screen=stage.querySelector('#sync-screen');
 if(screen){const stops=screenStops();screen.querySelectorAll('#screen-colors stop').forEach((s,i)=>setAttr(s,'stop-color',stops[i]));const demo=screenDemo()?1:0;setAttr(screen.querySelector('[data-screen-demo]'),'opacity',demo);setAttr(screen.querySelector('#screen-demo-image'),'opacity',demo);setAttr(screen.querySelector('#screen-image'),'opacity',1-demo)}
 stage.querySelectorAll('#sync-links [data-k]').forEach(n=>{
  const [id,part]=n.dataset.k.split(':'),l=lamps.find(l=>l.id===id);if(!l)return;
  const color=zoneColor(l,+part.slice(1));setAttr(n,part[0]==='r'?'stroke':'fill',color);
 });
 paintSwatches();
 const key=statusKey();
 if(key!==lastStatusKey){
  lastStatusKey=key;paintStatus();
  // Overlays follow the new status; the layout itself is left untouched.
  if(!drag&&!editingLabel){
   refreshHandles(false);
   if(mode==='sync'){const links=$('#sync-links'),markup=syncLinksSvg(sceneGeometry());if(links)links.outerHTML=markup||'<g id="sync-links" pointer-events="none"></g>';else if(markup)stage.insertAdjacentHTML('beforeend',markup)}
   applyFocus();
  }
  paintPanel();
 }
}

// -------------------------------------------------------------- editing --
let editingLabel=false;
function startLabelEdit(node){
 const a=$('#app').getBoundingClientRect(),r=node.getBoundingClientRect(),edit=node.dataset.edit,target=selected;
 const input=document.createElement('input');input.className='dim-input';input.type='number';input.value=node.dataset.value;
 input.setAttribute('aria-label',node.dataset.tooltip);
 input.style.left=(r.left-a.left+r.width/2)+'px';input.style.top=(r.top-a.top+r.height/2)+'px';
 $('#app').append(input);node.style.visibility='hidden';editingLabel=true;
 input.focus();input.select();requestAnimationFrame(()=>{if(document.activeElement!==input&&input.isConnected){input.focus();input.select()}});
 let done=false;
 const finish=apply=>{if(done)return;done=true;editingLabel=false;const v=input.valueAsNumber;input.remove();if(apply&&Number.isFinite(v)&&selected===target)applyLabelEdit(edit,v);else renderScene()};
 input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();finish(true)}else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();finish(false)}});
 input.addEventListener('blur',()=>finish(true));
}
function applyLabelEdit(edit,v){
 if(edit==='corr'){const l=lamps.find(l=>l.id===selected);if(!l)return;const before=snapshot();setCorr(l,clamp(Math.round(v),0,100));recordChange(before);renderScene();renderHistory();return}
 if(edit==='gap'){changeField('h',v+monitorHalfCm()+environment.height);return}
 changeField(edit,v);
}
function changeField(key,value){
 if(!Number.isFinite(value))return;
 const before=snapshot(),l=movableFixture();
 if(selected==='desk'){
  const previous=environment.height;
  environment[key]=clamp(Math.round(value/5)*5,key==='width'?80:50,key==='width'?240:120);
  if(key==='height'){strip().h+=environment.height-previous;clampFixture(strip())}
 }else if(selected==='monitor'){
  if(key==='inches')environment.inches=clamp(Math.round(value),19,55);else strip()[key]=value;
  clampFixture(strip());
 }else if(l){l[key]=key==='x'?value+(l.id===MONITOR?0:strip().x):value;clampFixture(l)}
 recordChange(before);renderScene();renderHistory();
}
function dragHandle(kind,p){
 if(kind.startsWith('zp-')){const before=snapshot();dragZone(kind,p);if(snapshot()!==before){drag.moved=true;renderScene();renderHistory()}return}
 const s=strip(),l=lamps.find(l=>l.id===selected),before=snapshot();
 if(kind==='desk-w')environment.width=clamp(Math.round(Math.abs(p.x-500)*2/3.3/5)*5,80,240);
 else if(kind==='desk-h'){const prev=environment.height;environment.height=clamp(Math.round((740-p.y)/3.3/5)*5,50,120);if(environment.height!==prev){s.h+=environment.height-prev;clampFixture(s)}}
 else if(kind==='mon-size'){const cx=500+s.x*3.3,cy=740-s.h*3.3,wcm=Math.max(Math.abs(p.x-cx)*2,Math.abs(p.y-cy)*2*16/9)/3.3;environment.inches=clamp(Math.round(wcm/2.54*Math.hypot(16,9)/16),19,55);clampFixture(s)}
 else if(kind==='scale'&&l){const t=correctionTrack(l);setCorr(l,clamp(Math.round((t.top+t.len-p.y)/t.len*20)*5,0,100))}
 if(snapshot()===before)return;
 drag.moved=true;renderScene();renderInspector();renderHistory();
}
function rotateSelected(){const l=lamps.find(l=>l.id===selected);if(!l||l.shape!=='bar')return;remember();l.vertical=!l.vertical;clampFixture(l);render()}
function select(id){if(id!==selected)closeObjectMenu();selected=id;render()}
function deselect(){closeObjectMenu();selected=null;render()}
function endDragView(){$('#app').classList.remove('is-dragging')}
function point(e){return new DOMPoint(e.clientX,e.clientY).matrixTransform($('#stage').getScreenCTM().inverse())}

// ---------------------------------------------------- scene and Sync view --
let darkScene=store.get(DARK_KEY)==='true',sceneSweep=null,sceneSweepId=0,modeFade=null;
function applyDarkScene(){
 $('#app').classList.toggle('dark-scene',darkScene);
 $('#scene-toggle').setAttribute('aria-pressed',String(darkScene));
 $('#scene-toggle').title=darkScene?'Show the light scene':'Show the dark scene';
}
// A static copy of the room for the scene and Sync-view transitions.
function captureScene(wasDark){
 const layer=document.createElement('div');
 layer.className='scene-snapshot motion-halo '+(wasDark?'snapshot-dark reveal-light':'snapshot-light reveal-dark');
 layer.setAttribute('aria-hidden','true');layer.inert=true;
 layer.append($('#app > .scene-background').cloneNode(true));
 const vector=$('#stage').cloneNode(true),prefix='sweep-'+(++sceneSweepId)+'-';
 vector.removeAttribute('id');vector.removeAttribute('role');vector.removeAttribute('aria-label');
 vector.querySelectorAll('[tabindex]').forEach(n=>n.removeAttribute('tabindex'));
 vector.querySelectorAll('[id]').forEach(n=>n.id=prefix+n.id);
 vector.querySelectorAll('*').forEach(n=>{
  for(const attr of [...n.attributes])if(attr.value.includes('url(#'))n.setAttribute(attr.name,attr.value.replace(/url\(#([^)]+)\)/g,(_,id)=>'url(#'+prefix+id+')'));
 });
 vector.querySelectorAll('.render-wash').forEach(n=>n.style.filter='url(#'+prefix+'wall-wash)');
 vector.querySelectorAll('.render-bloom,.contact-shadow').forEach(n=>n.style.filter='url(#'+prefix+'bloom)');
 layer.append(vector);return layer;
}
function toggleDarkScene(){
 const wasDark=darkScene;darkScene=!darkScene;
 store.set(DARK_KEY,String(darkScene));
 if(matchMedia('(prefers-reduced-motion:reduce)').matches){
  if(sceneSweep){sceneSweep.animation.cancel();sceneSweep.layer.remove();sceneSweep=null}
  applyDarkScene();return;
 }
 if(sceneSweep){
  sceneSweep.animation.updatePlaybackRate(darkScene===sceneSweep.toDark?1:-1);
  sceneSweep.animation.play();
  $('#scene-toggle').setAttribute('aria-pressed',String(darkScene));
  $('#scene-toggle').title=darkScene?'Show the light scene':'Show the dark scene';
  return;
 }
 const layer=captureScene(wasDark);
 const app=$('#app').getBoundingClientRect(),origin=$('#scene-toggle').getBoundingClientRect();
 layer.style.setProperty('--sweep-x',(origin.left+origin.width/2-app.left)+'px');
 layer.style.setProperty('--sweep-y',(origin.top+origin.height/2-app.top)+'px');
 applyDarkScene();$('#app').append(layer);
 const ends=darkScene?['1.2','-0.2']:['-0.2','1.2'];
 layer.style.setProperty('--scene-sweep',ends[0]);
 const animation=layer.animate([{'--scene-sweep':ends[0]},{'--scene-sweep':ends[1]}],{duration:650,easing:'cubic-bezier(.4,0,.2,1)',fill:'both'});
 const sweep={layer,animation,toDark:darkScene};sceneSweep=sweep;
 animation.onfinish=()=>{
  if(sceneSweep!==sweep)return;
  applyDarkScene();layer.remove();sceneSweep=null;animation.cancel();
 };
}
// The Sync switch chooses which settings are shown; it does not start or stop Sync.
function applyMode(m){
 mode=m;
 $('#sync-tool').classList.toggle('active',m==='sync');$('#sync-tool').setAttribute('aria-pressed',String(m==='sync'));$('#app').classList.toggle('sync-mode',m==='sync');
 store.set(VIEW_KEY,m);renderScene();renderInspector();
}
function setMode(m){
 if(m===mode||matchMedia('(prefers-reduced-motion:reduce)').matches)return applyMode(m);
 if(modeFade){modeFade.animation.cancel();modeFade.layer.remove();modeFade=null}
 const layer=captureScene(darkScene);layer.classList.remove('motion-halo','reveal-light','reveal-dark');layer.classList.add('mode-fade');
 applyMode(m);
 $('#app').append(layer);
 const animation=layer.animate([{opacity:1},{opacity:0}],{duration:600,easing:'ease',fill:'forwards'});
 const fade={layer,animation};modeFade=fade;
 animation.onfinish=()=>{if(modeFade===fade)modeFade=null;layer.remove()};
}

// ---------------------------------------------------------- lamp card --
function zonesText(n){return n+' '+(n===1?'zone':'zones')}
function lampInfo(l){return l.model+' · '+zonesText(l.zones)}
function closeObjectMenu(){if(!menuOpen)return;menuOpen=false;$('#object-menu').hidden=true;$('#stage').querySelector('[data-chip-action="more"]')?.classList.remove('open')}
function seg(prefix,options,value,disabled){return '<div class="seg" role="group">'+options.map(([v,t])=>'<button data-menu="'+prefix+v+'" aria-pressed="'+(v===value)+'"'+(disabled?' disabled':'')+'>'+t+'</button>').join('')+'</div>'}
function cardMenu(l){
 const c=corr(l),b=lampBlocker(l),editable=virtualDevice(l.id)||isRole(l.id);
 return '<input class="card-name" value="'+esc(l.name)+'" aria-label="Lamp name" maxlength="'+nameLimit(l.id)+'"'+(editable?'':' readonly')+'>'
  +'<div class="card-sub">'+esc(lampInfo(l))+'</div>'
  +(b?'<div class="card-alert" style="--h-color:'+levelColor[blockerLevel(b)]+'"><i></i><div><b>'+esc(capital(blockerText(b)))+'</b><span>'+esc(blockerHint(b))+'</span></div></div>':'')
  +'<div class="card-set"><div class="set-row'+(l.unbound?' disabled':'')+'">During Sync'+seg('role-',[['sync','Sync'],['own','Own light']].concat(isRole(l.id)?[['off','Off']]:[]),syncRole(l),l.unbound)+'</div></div>'
  +'<div class="card-actions"><button data-menu="blink"'+(l.unbound?' disabled title="Not connected"':'')+'>'+iconSvg('blink')+'Blink</button>'
  +'<button data-menu="reset" title="Reset correction to 100%"'+(c===100?' disabled':'')+'>'+iconSvg('reset')+(c===100?'Correction 100%':c+'% → 100%')+'</button></div>'
  +'<button class="mi danger" data-menu="remove">'+iconSvg('remove')+'Remove from layout</button>';
}
// The card sits beside the selected object, never over it, and does not follow the chip width.
function selectionRect(){
 const nodes=[$('#stage').querySelector('[data-lamp="'+selected+'"] .outline')||$('#stage').querySelector('[data-lamp="'+selected+'"]'),...$('#stage').querySelectorAll('.handle')].filter(Boolean);
 const rs=nodes.map(n=>n.getBoundingClientRect()).filter(r=>r.width||r.height);if(!rs.length)return null;
 return {left:Math.min(...rs.map(r=>r.left)),right:Math.max(...rs.map(r=>r.right)),top:Math.min(...rs.map(r=>r.top)),bottom:Math.max(...rs.map(r=>r.bottom))};
}
function placeMenu(menu,anchorRect){
 const a=$('#app').getBoundingClientRect(),sel=selectionRect();
 if(!sel){let left=anchorRect.left+anchorRect.width/2-a.left-menu.offsetWidth/2;left=Math.max(12,Math.min(a.width-menu.offsetWidth-12,left));menu.style.left=left+'px';menu.style.top=(anchorRect.bottom-a.top+8)+'px';return}
 const w=menu.offsetWidth,h=menu.offsetHeight,gap=12,pad=12;
 const top=Math.max(pad,Math.min(a.height-h-pad,anchorRect.bottom-a.top+8));
 const right=sel.right-a.left+gap,left=sel.left-a.left-gap-w;
 let x;
 if(right+w<=a.width-pad)x=right;
 else if(left>=pad)x=left;
 else x=Math.max(pad,Math.min(a.width-w-pad,anchorRect.left-a.left));
 menu.style.left=x+'px';menu.style.top=top+'px';
}
function openObjectMenu(anchor){
 const l=lamps.find(l=>l.id===selected);if(!l)return;
 if(menuOpen){closeObjectMenu();return}
 menuOpen=true;anchor.classList.add('open');
 const menu=$('#object-menu');menu.className='object-menu card';
 menu.innerHTML=cardMenu(l);menu.hidden=false;
 placeMenu(menu,anchor.closest('.obj-chip').getBoundingClientRect());
 const name=menu.querySelector('.card-name');
 if(name&&!name.readOnly){name.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();renameLamp(l.id,name.value);name.blur()}else if(e.key==='Escape'){name.value=l.name;closeObjectMenu()}});name.addEventListener('change',()=>renameLamp(l.id,name.value))}
}
function refreshCard(){requestAnimationFrame(()=>{closeObjectMenu();const b=$('#stage').querySelector('[data-chip-action="more"]');if(b)openObjectMenu(b)})}
// Layout-only drawings and the six desk lamps carry a stored name (a desk lamp's name also
// renames it in the Hue app and HomeKit); discovered lamps keep their canonical name.
function renameLamp(id,value){
 const l=lamps.find(l=>l.id===id),v=String(value).trim().slice(0,l?nameLimit(l.id):0);if(!l||!(virtualDevice(l.id)||isRole(l.id))||!v||v===l.name)return;
 const before=snapshot();l.name=v;if(isRole(l.id))l.named=true;recordChange(before);renderScene();renderList();renderHistory();
 if(menuOpen){const chip=$('#stage').querySelector('.obj-chip');if(chip)placeMenu($('#object-menu'),chip.getBoundingClientRect())}
}
function resetCorrection(){
 const l=lamps.find(l=>l.id===selected);if(!l||corr(l)===100)return;
 const before=snapshot();setCorr(l,100);recordChange(before);renderScene();renderHistory();tell('Correction reset to 100%');
}
// Blink asks the adapter to flash the real lamp once; the drawing blinks too, so the owner sees which lamp was asked.
const BLINK_REFUSED={'lamp off':'The lamp is off','writer closed':'The lamp is still turning on','sync active':'The lamp is syncing','busy':'The lamp is already blinking','paused':'Light output is paused','brightness unknown':'The lamp has not reported its brightness yet'};
async function blinkLamp(l){
 const node=$('#stage').querySelector('[data-lamp="'+l.id+'"]');
 if(node){node.classList.remove('blinking');void node.getBoundingClientRect?.();node.classList.add('blinking');setTimeout(()=>node.classList.remove('blinking'),1000)}
 try{
  const response=await fetch('/api/identify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lamp:l.id})});
  const body=await response.json().catch(()=>({}));
  if(response.ok)tell('Blinking '+l.name);
  else tell(response.status===409?BLINK_REFUSED[body.reason]||'The lamp cannot blink now':'Blink failed: the adapter is not responding');
 }catch{tell('Blink failed: the adapter is not responding')}
}
function setLamp(l,change,message){remember();change(l);render();if(message)tell(message);refreshCard()}
function menuAction(id){
 const menu=$('#object-menu'),l=lamps.find(l=>l.id===selected);if(!l)return false;
 if(id.startsWith('role-')){const v=id.slice(5);if(SYNC_ROLES.includes(v)&&(v!=='off'||isRole(l.id))&&v!==syncRole(l)&&!l.unbound)setLamp(l,l=>{l.syncRole=v});return true}
 if(id==='remove'){const b=menu.querySelector('[data-menu="remove"]');if(b)b.outerHTML='<div class="card-confirm"><span>Remove '+esc(l.name)+'?</span><button data-menu="remove-cancel">Cancel</button><button class="danger" data-menu="remove-ok">Remove</button></div>';menu.querySelector('[data-menu="remove-cancel"]')?.focus();return true}
 if(id==='remove-cancel'){const c=menu.querySelector('.card-confirm');if(c)c.outerHTML='<button class="mi danger" data-menu="remove">'+iconSvg('remove')+'Remove from layout</button>';return true}
 if(id==='remove-ok'){closeObjectMenu();removeLampById(l.id);return true}
 if(id==='reset'){closeObjectMenu();resetCorrection();return true}
 if(id==='blink'){if(!l.unbound)blinkLamp(l);return true}
 return false;
}

// ---------------------------------------------------------- lamp panel --
let panelKind=null,confirmRemove=null,pendingDevice=null;
let discovery=null,discoveryTimer=null,discoverySession=0,discoveryLoading=null;
function catalogRows(){return discovery&&discovery.connected===true&&Array.isArray(discovery.devices)?discovery.devices.filter(d=>d&&realDevice(d.id)):[]}
// Discovered lamps that are not in the layout; role lamps return through their removed row.
function catalogEntries(){return catalogRows().filter(d=>!d.layout_id&&!lamps.some(l=>l.id===d.id))}
function catalogReason(d){
 if(d.ambiguous!==false)return 'Ambiguous';
 if(d.supported!==true||!SHAPES.includes(d.shape)||!Number.isInteger(d.zones))return 'Unsupported';
 if(d.online!==true)return 'Offline';
 if(d.selectable!==true)return 'Unavailable';
 return '';
}
function removedLamps(){return lamps.filter(l=>l.enabled===false)}
function lampSwatch(l){
 const colors=liveColors(l);
 if(!colors)return '<span class="lp-swatch off" data-swatch="'+l.id+'"></span>';
 return '<span class="lp-swatch" data-swatch="'+l.id+'" style="--sw:'+swatchFill(l,colors)+'"></span>';
}
function swatchFill(l,colors){
 if(mode==='sync'&&colors.length>1){const cs=colors.slice().reverse(),n=cs.length;return 'linear-gradient('+cs.map((c,i)=>rgbHex(c)+' '+(i/n*100)+'% '+((i+1)/n*100)+'%').join(',')+')'}
 return beamColor(colors);
}
function paintSwatches(){
 const panel=$('#lamp-panel');if(!panel||panel.hidden)return;
 panel.querySelectorAll('[data-swatch]').forEach(node=>{
  const l=lamps.find(l=>l.id===node.dataset.swatch);if(!l)return;const colors=liveColors(l);
  node.classList.toggle('off',!colors);if(colors)setVar(node,'--sw',swatchFill(l,colors));
 });
}
function lampRowStatus(l){
 if(l.unbound)return '<span class="lp-status">Not connected</span>';
 const b=lampBlocker(l);if(b)return '<span class="lp-status warn" style="--h-color:'+levelColor[blockerLevel(b)]+'">'+esc(capital(blockerText(b)))+'</span>';
 if(mode==='sync'&&syncRole(l)==='own')return '<span class="lp-status">Own light</span>';
 if(mode==='sync'&&syncRole(l)==='off')return '<span class="lp-status">Off during Sync</span>';
 if(mode==='sync'&&!syncSummary(l).active)return '<span class="lp-status">Outside Sync</span>';
 return corr(l)!==100?'<span class="lp-status">'+corr(l)+'%</span>':'';
}
function roomRows(){
 return activeLamps().map(l=>confirmRemove===l.id
  ?'<div class="lp-row lamp-row confirm" role="group" aria-label="Confirm removal"><span><span class="lp-name">Remove '+esc(l.name)+'?</span><span class="lp-sub">⌘Z brings it back</span></span><button class="lp-confirm-btn" data-remove-cancel>Cancel</button><button class="lp-confirm-btn danger" data-remove-ok="'+l.id+'">Remove</button></div>'
  :'<div class="lp-row lamp-row'+(selected===l.id?' active':'')+'" role="button" tabindex="0" data-list-lamp="'+l.id+'">'+lampSwatch(l)+'<span><span class="lp-name">'+esc(l.name)+'</span><span class="lp-sub">'+esc(lampInfo(l))+'</span></span>'+(lampRowStatus(l)||'<span></span>')+'<button class="lp-remove" data-remove="'+l.id+'" aria-label="Remove '+esc(l.name)+'" data-tooltip="Remove from layout">'+trashIcon+'</button></div>').join('')
  +'<div class="lp-divider"></div>'
  +'<button class="lp-row'+(selected==='desk'?' active':'')+'" data-list-lamp="desk"><span class="lp-glyph">'+glyph.desk+'</span><span><span class="lp-name">Desk</span><span class="lp-sub">'+environment.width+' × '+environment.height+' cm</span></span></button>'
  +'<button class="lp-row'+(selected==='monitor'?' active':'')+'" data-list-lamp="monitor"><span class="lp-glyph">'+glyph.monitor+'</span><span><span class="lp-name">Monitor</span><span class="lp-sub">'+environment.inches+'″</span></span></button>';
}
function foundRows(){
 const back=removedLamps().map(l=>'<div class="lp-row found"><span class="lp-glyph">'+glyph[l.shape]+'</span><span><span class="lp-name">'+esc(l.name)+'</span><span class="lp-sub">'+esc(lampInfo(l))+'</span></span><button class="lp-add" data-restore="'+l.id+'" aria-label="Return '+esc(l.name)+'" data-tooltip="Return to its place">'+plusIcon+'</button></div>').join('');
 const items=catalogEntries();
 if(!items.length&&!back){
  if(!discovery)return '<div class="lp-empty">Searching for lamps…</div>';
  if(discovery.connected!==true)return '<div class="lp-empty">Lamp search is unavailable</div>';
  return '<div class="lp-empty">Every found lamp is in the room</div>';
 }
 return back+items.map(e=>{const reason=catalogReason(e);return '<div class="lp-row found'+(reason?' unavailable':'')+'"><span class="lp-glyph">'+(glyph[e.shape]||glyph.bar)+'</span><span><span class="lp-name">'+esc(e.name)+'</span><span class="lp-sub">'+esc(e.model)+(Number.isInteger(e.zones)?' · '+zonesText(e.zones):'')+'</span></span>'+(reason?'<span class="lp-status">'+reason+'</span>':'<button class="lp-add" data-place="'+e.id+'" aria-label="Place '+esc(e.name)+'" data-tooltip="Place in the room">'+plusIcon+'</button>')+'</div>'}).join('');
}
function foundCount(){return catalogEntries().filter(e=>!catalogReason(e)).length+removedLamps().length}
const PANEL_FOCUS=['data-list-lamp','data-remove','data-remove-cancel','data-remove-ok','data-restore','data-place','data-close'];
function paintPanel(){
 const p=$('#lamp-panel');if(!p||p.hidden)return;const top=p.scrollTop;
 const active=p.contains(document.activeElement)?document.activeElement:null;
 const focusAttr=active&&PANEL_FOCUS.find(a=>active.hasAttribute(a)),focusValue=focusAttr&&active.getAttribute(focusAttr);
 p.innerHTML='<div class="lp-head"><h2>Lamps</h2><button data-close aria-label="Close" data-tooltip="Close · Esc">'+closeIcon+'</button></div>'
  +'<div class="lp-section"><span>In the room</span><span>'+activeLamps().length+'</span></div>'+roomRows()
  +'<div class="lp-section" id="lp-found"><span>Found</span><span>'+foundCount()+' available</span></div>'+foundRows();
 p.scrollTop=top;
 if(focusAttr)p.querySelector('['+focusAttr+(focusValue?'="'+focusValue+'"':'')+']')?.focus({preventScroll:true});
}
function placePanel(){
 const p=$('#lamp-panel'),a=$('#app').getBoundingClientRect(),r=$('#list-toggle').getBoundingClientRect();
 p.style.left=(r.left-a.left)+'px';p.style.bottom=(a.bottom-r.top+8)+'px';
}
function markLamp(id){$('#stage').querySelectorAll('.lamp.list-hover').forEach(n=>n.classList.remove('list-hover'));if(id)$('#stage').querySelector('[data-lamp="'+id+'"]')?.classList.add('list-hover')}
function openPanel(kind){
 cancelPlacement();panelKind=kind;const p=$('#lamp-panel');p.hidden=false;p.scrollTop=0;paintPanel();p.scrollTop=0;placePanel();
 $('#list-toggle').setAttribute('aria-expanded','true');$('#list-toggle').classList.add('active');
 startDiscovery();
}
function closePanel(){
 confirmRemove=null;$('#lamp-panel').hidden=true;panelKind=null;markLamp(null);stopDiscovery();
 $('#list-toggle').setAttribute('aria-expanded','false');$('#list-toggle').classList.remove('active');
 if(document.activeElement?.id==='list-toggle')document.activeElement.blur();
}
function toggleList(){if(panelKind==='list')closePanel();else openPanel('list')}
function openAdd(){
 openPanel('list');const f=$('#lp-found');
 if(f){f.scrollIntoView({block:'nearest'});$('#lamp-panel').querySelectorAll('.lp-row.found').forEach(r=>{r.classList.remove('lp-flash');void r.offsetWidth;r.classList.add('lp-flash')})}
}
// The six role rows must stay in the layout, so removing one only excludes it.
// Optional rows are deleted to free their slot; a discovered lamp returns to Found.
function removeLampById(id){const l=lamps.find(l=>l.id===id);if(!l)return;remember();if(isRole(id))l.enabled=false;else lamps=lamps.filter(x=>x.id!==id);if(selected===id)selected=null;render();tell(l.name+' removed · ⌘Z to restore')}
function restoreLamp(id){const l=lamps.find(l=>l.id===id&&l.enabled===false);if(!l)return;remember();l.enabled=true;clampFixture(l);selected=l.id;render();tell(l.name+' is back in the layout')}
function askRemove(id){if(!lamps.some(l=>l.id===id&&l.enabled!==false))return;if(panelKind!=='list')openPanel('list');confirmRemove=id;paintPanel();const p=$('#lamp-panel');p.querySelector('.lp-row.confirm')?.scrollIntoView({block:'nearest'});p.querySelector('[data-remove-cancel]')?.focus()}
function startDiscovery(){discoverySession++;clearTimeout(discoveryTimer);refreshDiscovery(discoverySession)}
function stopDiscovery(){discoverySession++;clearTimeout(discoveryTimer);discoveryTimer=null}
async function refreshDiscovery(session){
 if(panelKind!=='list'||session!==discoverySession||discoveryLoading===session)return;
 discoveryLoading=session;
 try{const response=await fetch('/api/discovery',{cache:'no-store'});if(!response.ok)throw new Error('discovery');const snapshot=await response.json();if(session===discoverySession)discovery=snapshot}
 catch{if(session===discoverySession)discovery={connected:false,devices:[],providers:{}}}
 finally{
  if(discoveryLoading===session)discoveryLoading=null;
  if(panelKind==='list'&&session===discoverySession){paintPanel();clearTimeout(discoveryTimer);discoveryTimer=setTimeout(()=>refreshDiscovery(session),DISCOVERY_POLL_MS)}
 }
}

// ------------------------------------------------------------ placement --
function cancelPlacement(){pendingDevice=null;$('#placement-ghost')?.remove();$('#placement-hint').hidden=true;$('#app').classList.remove('placing')}
function beginPlacement(id){
 const entry=catalogEntries().find(e=>e.id===id);if(!entry||catalogReason(entry))return;
 if(lamps.length>=MAX_ROWS){tell('The layout holds up to '+MAX_ROWS+' lamps');return}
 closePanel();cancelPlacement();
 pendingDevice={...entry,form:formOf(entry.id,entry.shape)};$('#app').classList.add('placing');
 const hint=$('#placement-hint');hint.textContent='Click in the room to place '+entry.name+' · Esc to cancel';hint.hidden=false;
 paintPlacementGhost({x:entry.shape==='floor'?105:45,h:entry.shape==='floor'?0:65});
}
function placementPosition(event){const p=point(event);return {x:clamp(Math.round((p.x-500)/3.3/5)*5,-115,115),h:pendingDevice.form==='floor'?0:clamp(Math.round((740-p.y)/3.3/5)*5,5,160)}}
function paintPlacementGhost(position){
 if(!pendingDevice)return;let ghost=$('#placement-ghost');
 if(!ghost){ghost=document.createElementNS('http://www.w3.org/2000/svg','g');ghost.id='placement-ghost';ghost.classList.add('placement-ghost');ghost.setAttribute('aria-hidden','true');ghost.style.pointerEvents='none';$('#stage').append(ghost)}
 const lamp={id:'pending-placement',name:pendingDevice.name,model:pendingDevice.model,shape:pendingDevice.shape,form:pendingDevice.form,zones:pendingDevice.zones,x:position.x,h:position.h,scale:80,unbound:true,vertical:false};
 const f=lampFrame(lamp),off=position.x===0?'0 cm':(position.x>0?'+':'−')+Math.abs(position.x)+' cm';
 ghost.innerHTML=lampSvg(lamp)+'<text class="ghost-label" x="'+f.x+'" y="'+(f.top-10)+'">'+esc(pendingDevice.name)+'<tspan class="sub"> · '+off+'</tspan></text>';
}
function placeDevice(entry,position){
 if(lamps.length>=MAX_ROWS||lamps.some(l=>l.id===entry.id))return;
 remember();
 const l={id:entry.id,name:entry.name,model:entry.model,shape:entry.shape,form:entry.form,zones:entry.zones,x:position.x,h:position.h,scale:80,vertical:false,enabled:true,syncRole:'sync',unbound:true};
 clampFixture(l);lamps.push(l);selected=l.id;
 cancelPlacement();render();tell('Added to layout');
}

// ----------------------------------------------------------- persistence --
let serverReady=false,revision=null,pendingLayout=null,saving=false,saveTimer,retryTimer,offlineNotice=false,editGeneration=0;
let serverBaseline=readJson(KEY+'-base')||pack();
// A drag in progress is not saved; the last committed state is.
function committedModel(){return drag?{...JSON.parse(drag.before),room}:{room,environment,lamps}}
let lastPersisted=JSON.stringify(pack());
function persistIfChanged(){const value=JSON.stringify(pack(committedModel()));if(value===lastPersisted)return;lastPersisted=value;editGeneration++;save()}
function setDraftState(state){
 const node=$('#draft-status');if(!node)return;const label={saved:'Saved',pending:'Saving',error:'Not saved'}[state];
 if(node.dataset.state!==state)node.dataset.state=state;if(node.getAttribute('aria-label')!==label)node.setAttribute('aria-label',label);node.title=label;
}
function save(){
 const saved=pack(committedModel());
 if(!store.set(KEY,JSON.stringify(saved))){setDraftState('error');tell('The browser did not keep the local draft')}
 else if(editGeneration>0)store.set(KEY+'-pending','true');
 pendingLayout=saved;clearTimeout(saveTimer);
 if(serverReady)saveTimer=setTimeout(flushLayout,SAVE_DELAY_MS);
 setDraftState(serverReady||!offlineNotice?'pending':'error');
}
function acceptBaseline(saved){serverBaseline=clone(saved.layout);revision=saved.revision;store.set(KEY+'-base',JSON.stringify(serverBaseline))}
function applyModel(m){
 ({room,environment,lamps}=m);
 if(selected&&!['desk','monitor','zone'].includes(selected)&&!lamps.some(l=>l.id===selected&&l.enabled!==false))selected=null;
 lastPersisted=JSON.stringify(pack());
}
function mergeLayouts(base,local,remote){
 const result=clone(remote),equal=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
 const field=(out,key,b,l)=>{if(!equal(b,l)){if(l===undefined)delete out[key];else out[key]=clone(l);}};
 for(const key of ['room','monitorInches'])field(result,key,base[key],local[key]);
 // Canonical responses omit an automatic zone; treat that like this client's explicit null.
 for(const key of ['syncZone','sceneZone'])field(result,key,base[key]??null,local[key]??null);
 // Canonical responses also omit fade switches at their defaults.
 field(result,'syncFalloff',base.syncFalloff??true,local.syncFalloff??true);
 field(result,'sceneFalloff',base.sceneFalloff??false,local.sceneFalloff??false);
 for(const key of ['width','height'])field(result.desk,key,base.desk[key],local.desk[key]);
 const old=new Map(base.devices.map(d=>[d.id,d])),mine=new Map(local.devices.map(d=>[d.id,d]));
 for(const [id]of old)if(!mine.has(id))result.devices=result.devices.filter(d=>d.id!==id);
 for(const [id,current]of mine){
  const previous=old.get(id),target=result.devices.find(d=>d.id===id);
  if(!previous){if(!target)result.devices.push(clone(current));continue;}
  if(!target){if(!equal(previous,current))result.devices.push(clone(current));continue;}
  for(const key of ['scale','sceneScale','orientation','name','shape','model','zones','calibration'])field(target,key,previous[key],current[key]);
  field(target,'enabled',previous.enabled===false?false:undefined,current.enabled===false?false:undefined);
  field(target,'syncRole',previous.syncRole!=='sync'?previous.syncRole:undefined,current.syncRole!=='sync'?current.syncRole:undefined);
  for(const key of ['x','height'])field(target.positionCm,key,previous.positionCm[key],current.positionCm[key]);
 }
 return result;
}
function reconcile(saved,base=serverBaseline){
 if(drag){({lamps,environment}=JSON.parse(drag.before));drag=null;endDragView()}
 const remap=s=>{const m=fromLayout(mergeLayouts(base,pack({...JSON.parse(s),room}),saved.layout));return m?JSON.stringify({lamps:m.lamps,environment:m.environment}):s};
 past=past.map(remap);future=future.map(remap);
 applyModel(fromLayout(mergeLayouts(base,pack(),saved.layout))||fromLayout(saved.layout)||defaults());
 acceptBaseline(saved);store.set(KEY,JSON.stringify(pack()));render();
}
async function flushLayout(){
 if(saving||!serverReady)return;
 if(drag){clearTimeout(saveTimer);saveTimer=setTimeout(flushLayout,SAVE_DELAY_MS);return}
 saving=true;
 try{
  let conflicts=0;
  while(pendingLayout){
   const payload=pendingLayout,base=clone(serverBaseline);pendingLayout=null;
   const response=await fetch('/api/layout',{method:'POST',headers:{'Content-Type':'application/json','If-Match':'"'+(revision||'')+'"'},body:JSON.stringify(payload)});
   if(response.status===409&&conflicts++<2){
    const current=await fetch('/api/layout',{cache:'no-store'});if(!current.ok)throw new Error('offline');
    reconcile(await current.json(),base);pendingLayout=pack(committedModel());continue;
   }
   if(!response.ok){pendingLayout=pendingLayout||payload;throw new Error('save');}
   acceptBaseline(await response.json());if(!pendingLayout)store.remove(KEY+'-pending');offlineNotice=false;
  }
  setDraftState('saved');
 }catch{
  serverReady=false;setDraftState('error');if(!offlineNotice)tell('Changes are not saved yet. They are kept in this browser.');offlineNotice=true;
  clearTimeout(retryTimer);retryTimer=setTimeout(connectLayout,RETRY_MS);
 }finally{saving=false;}
}
async function connectLayout(){
 try{
  const response=await fetch('/api/layout',{cache:'no-store'});if(!response.ok)throw new Error('offline');
  const saved=await response.json(),hasLocalEdits=editGeneration>0||!!store.get(KEY+'-pending');revision=saved.revision;
  if(saved.layout){
   if(hasLocalEdits)reconcile(saved);
   else{const m=fromLayout(saved.layout);if(m)applyModel(m);acceptBaseline(saved);pendingLayout=null;store.set(KEY,JSON.stringify(pack()));render();}
  }
  serverReady=true;offlineNotice=false;
  // The first load imports this browser's draft when the server has no layout yet.
  if(!saved.layout||hasLocalEdits)save();else setDraftState('saved');
 }catch{
  serverReady=false;setDraftState('error');if(!offlineNotice)tell('Light Studio server is unavailable. The layout is kept in this browser.');offlineNotice=true;
  clearTimeout(retryTimer);retryTimer=setTimeout(connectLayout,RETRY_MS);
 }
}

// -------------------------------------------------------------- live poll --
function expireLive(){liveSnapshot=null;liveAt=-Infinity;paintLive()}
function watchLive(){
 clearTimeout(liveWatchdog);
 if(document.hidden)return;
 paintLive();
 const remaining=LIVE_FRESH_MS-(performance.now()-liveAt);
 if(remaining>0)liveWatchdog=setTimeout(watchLive,remaining);
}
async function refreshLive(){
 if(document.hidden||liveRequest)return;
 const generation=++liveGeneration,controller=new AbortController();
 let rejectCancelled;
 const cancelled=new Promise((_,reject)=>{rejectCancelled=reject;});
 const request={controller,cancel:()=>{controller.abort();rejectCancelled(new Error('live status cancelled'));}};
 liveRequest=request;
 const deadline=setTimeout(request.cancel,LIVE_DEADLINE_MS);
 try{
  const snapshot=await Promise.race([fetch('/api/live',{signal:controller.signal,cache:'no-store'}).then(async response=>{if(!response.ok)throw new Error('live status unavailable');return response.json();}),cancelled]);
  if(generation!==liveGeneration||document.hidden)return;
  liveSnapshot=snapshot&&typeof snapshot==='object'?snapshot:null;liveAt=performance.now();watchLive();
 }catch{if(generation===liveGeneration)expireLive();}
 finally{
  clearTimeout(deadline);
  if(liveRequest===request)liveRequest=null;
  if(generation===liveGeneration&&!document.hidden)liveTimer=setTimeout(refreshLive,LIVE_POLL_MS);
 }
}
document.addEventListener('visibilitychange',()=>{
 ++liveGeneration;clearTimeout(liveTimer);clearTimeout(liveWatchdog);
 liveRequest?.cancel();liveRequest=null;expireLive();
 if(!document.hidden){watchLive();refreshLive();}
});

// ----------------------------------------------------------------- hover --
let hoverKey=null;
function hoverAnchor(key){
 if(key==='desk'){const top=740-environment.height*3.3;return {x:500,y:top-23,name:'Desk'}}
 if(key==='monitor'){const m=strip(),mw=monitorWidthCm(environment.inches)*3.3,mh=mw*9/16;return {x:500+m.x*3.3,y:740-m.h*3.3-mh/2-41,name:'Monitor'}}
 if(key==='zone'){const P=zonePoints();return {x:(500+P[0].x*3.3+500-environment.width/2*3.3)/2,y:740-P[0].h*3.3-23,name:'Fade zone'}}
 const l=lamps.find(l=>l.id===key&&l.enabled!==false);if(!l)return null;const f=lampFrame(l);return {x:f.x,y:f.top-17,name:l.name};
}
function showHover(key){
 const stage=$('#stage');stage.querySelector('#hover-tag')?.remove();hoverKey=key;
 if(!key||key===selected||drag)return;
 const a=hoverAnchor(key);if(!a)return;
 const g=document.createElementNS('http://www.w3.org/2000/svg','g');g.id='hover-tag';g.setAttribute('pointer-events','none');
 g.innerHTML=tagSvg(a.x,a.y,a.name,'',0);stage.append(g);
}

// -------------------------------------------------------------- tooltips --
// Tooltips only on icon-only controls; they never repeat visible text.
let tooltipTarget=null,tooltipTimer=null;
function repeatsText(node,label){
 if(!label)return false;if(node.closest('#object-menu'))return true;
 const words=t=>String(t).toLowerCase().match(/[a-z]+|[0-9]+%?/g)||[],shown=new Set(words(node.textContent));if(!shown.size)return false;
 const said=words(label).filter(w=>!['the','to','a','of','from','in','·'].includes(w));return said.length>0&&said.filter(w=>shown.has(w)).length/said.length>=.5;
}
function tooltipLabelText(node){
 const native=node.getAttribute('title');if(native){node.dataset.originalTitle=native;node.removeAttribute('title')}
 if(node.classList.contains('lp-row'))return '';
 if(node.id==='sync-tool')return mode==='sync'?'Settings with Sync':'Settings without Sync';
 if(node.id==='sync-status')return syncStatus().detail;
 if(node.classList.contains('health-badge'))return node.dataset.tooltip;
 if(node.id==='scene-toggle')return darkScene?'Show the light scene':'Show the dark scene';
 if(node.id==='undo')return node.disabled?'Nothing to undo':'Undo · ⌘Z';
 if(node.id==='redo')return node.disabled?'Nothing to redo':'Redo · ⇧⌘Z';
 if(node.id==='list-toggle')return 'All lamps';
 if(node.dataset.lamp)return '';
 if(node.closest('#stage')&&['zone','desk','monitor'].includes(node.dataset.select))return '';
 return node.dataset.originalTitle||node.getAttribute('aria-label')||node.textContent.trim().replace(/\s+/g,' ');
}
function tooltipLabel(node){const label=tooltipLabelText(node);return repeatsText(node,label)?'':label}
function annotateTooltips(){
 document.querySelectorAll('#app button,#app [role="button"],#app [title],#app [data-tooltip]').forEach(node=>{
  const label=tooltipLabel(node);if(label&&node.dataset.tooltip!==label)node.dataset.tooltip=label;
 });
 if(tooltipTarget&&!tooltipTarget.isConnected)hideTooltip();else if(tooltipTarget&&!$('#ui-tooltip').hidden&&$('#ui-tooltip').textContent!==tooltipTarget.dataset.tooltip)revealTooltip(tooltipTarget);
}
function hideTooltip(){
 clearTimeout(tooltipTimer);tooltipTimer=null;const tip=$('#ui-tooltip');tip.hidden=true;tip.classList.remove('visible');
 if(tooltipTarget){const ids=(tooltipTarget.getAttribute('aria-describedby')||'').split(/\s+/).filter(id=>id&&id!=='ui-tooltip');if(ids.length)tooltipTarget.setAttribute('aria-describedby',ids.join(' '));else tooltipTarget.removeAttribute('aria-describedby')}
 tooltipTarget=null;
}
function revealTooltip(node){
 if(!node.isConnected||!node.getClientRects().length)return;
 const tip=$('#ui-tooltip'),label=tooltipLabel(node);if(!label)return;
 tip.textContent=label;tip.hidden=false;tip.classList.remove('visible');
 const box=node.getBoundingClientRect(),size=tip.getBoundingClientRect();
 let x=box.left+box.width/2-size.width/2,y=box.top-size.height-10;
 if(y<8)y=box.bottom+10;
 x=Math.max(8,Math.min(innerWidth-size.width-8,x));y=Math.max(8,Math.min(innerHeight-size.height-8,y));
 tip.style.left=x+'px';tip.style.top=y+'px';
 const ids=new Set((node.getAttribute('aria-describedby')||'').split(/\s+/).filter(Boolean));ids.add('ui-tooltip');node.setAttribute('aria-describedby',[...ids].join(' '));
 tip.classList.add('visible');
}
function queueTooltip(node,immediate=false){
 if(!node||!node.dataset.tooltip){hideTooltip();return}
 if(tooltipTarget===node)return;
 hideTooltip();tooltipTarget=node;
 tooltipTimer=setTimeout(()=>{if(tooltipTarget===node)revealTooltip(node)},immediate?0:180);
}

// ---------------------------------------------------------------- events --
const stage=$('#stage');
document.addEventListener('pointerdown',()=>{skipClick=false},true);
document.addEventListener('click',e=>{
 if(skipClick){skipClick=false;return}
 if(menuOpen&&!e.target.closest('#object-menu'))closeObjectMenu();
 const b=e.target.closest('[data-select]');if(b&&b.closest('#stage')){select(b.dataset.select);return}
});
stage.addEventListener('pointerdown',event=>{
 if(!pendingDevice)return;
 event.preventDefault();event.stopImmediatePropagation();skipClick=true;placeDevice(pendingDevice,placementPosition(event));
},true);
stage.addEventListener('pointerdown',()=>showHover(null),true);
stage.addEventListener('pointerdown',e=>{
 const action=e.target.closest('[data-chip-action]'),label=e.target.closest('[data-edit]');
 if(action||label){e.preventDefault();skipClick=true;if(label){closeObjectMenu();startLabelEdit(label)}else if(action.dataset.chipAction==='rotate'){closeObjectMenu();rotateSelected()}else if(action.dataset.chipAction==='auto'){resetZone()}else if(action.dataset.chipAction==='fade'){toggleFade()}else openObjectMenu(action);return}
 if(e.target.closest('.obj-chip')){e.preventDefault();skipClick=true;return}
 closeObjectMenu();
 const grip=e.target.closest('[data-handle]');
 if(grip){e.preventDefault();skipClick=true;drag={handle:grip.dataset.handle,before:snapshot(),moved:false};$('#app').classList.add('is-dragging');stage.setPointerCapture(e.pointerId);if(drag.handle==='scale')applyFocus();return}
 const node=e.target.closest('[data-lamp]')||e.target.closest('[data-select="monitor"]');
 if(!node){if(e.target.closest('[data-select]'))return;deselect();return}
 selected=node.dataset.select==='monitor'?'monitor':node.dataset.lamp;
 const l=movableFixture();if(!l)return;
 drag={start:point(e),x:l.x,h:l.h,before:snapshot(),moved:false};$('#app').classList.add('is-dragging');stage.setPointerCapture(e.pointerId);renderInspector();renderList();
 stage.querySelectorAll('[data-lamp],[data-select="monitor"]').forEach(n=>n.classList.toggle('chosen',n.dataset.lamp===selected||selected==='monitor'&&n.dataset.select==='monitor'));
});
stage.addEventListener('pointermove',e=>{
 if(!drag)return;if(drag.handle){dragHandle(drag.handle,point(e));return}
 const l=movableFixture();if(!l)return;const p=point(e),oldX=l.x,oldH=l.h;
 l.x=drag.x+(p.x-drag.start.x)/3.3;l.h=drag.h-(p.y-drag.start.y)/3.3;clampFixture(l);
 if(l.x===oldX&&l.h===oldH)return;
 drag.moved=l.x!==drag.x||l.h!==drag.h;
 renderScene();refreshHandles();renderInspector();
});
stage.addEventListener('pointerup',()=>{
 if(!drag)return;const before=drag.before;drag=null;recordChange(before);endDragView();renderScene();renderHistory();renderInspector();stage.querySelector(selectionFocus())?.focus({preventScroll:true});
});
stage.addEventListener('pointercancel',()=>{if(drag){const before=drag.before;drag=null;restore(before)}endDragView()});
stage.addEventListener('keydown',e=>{
 const id=e.target.dataset.lamp||(e.target.dataset.select==='monitor'?'monitor':e.target.dataset.select);
 if(e.key==='Enter'||e.key===' '){e.preventDefault();if(id)select(id);return}
 if(!e.key.startsWith('Arrow')||!id||id==='desk'||id==='zone')return;
 e.preventDefault();selected=id;const l=movableFixture();if(!l)return;const before=snapshot();
 if(e.key==='ArrowLeft')l.x-=5;if(e.key==='ArrowRight')l.x+=5;if(e.key==='ArrowUp')l.h+=5;if(e.key==='ArrowDown')l.h-=5;
 clampFixture(l);recordChange(before);render();stage.querySelector(selectionFocus())?.focus({preventScroll:true});
});
stage.addEventListener('pointermove',event=>{if(pendingDevice)paintPlacementGhost(placementPosition(event))});
stage.addEventListener('pointermove',e=>{
 if(drag){if(hoverKey)showHover(null);return}
 const node=e.target.closest('#stage [data-lamp],#stage [data-select]');
 const key=node?(node.dataset.lamp||node.dataset.select):null;
 if(key!==hoverKey||(key&&!stage.querySelector('#hover-tag')&&key!==selected))showHover(key);
});
stage.addEventListener('pointerleave',()=>showHover(null));
document.addEventListener('keydown',e=>{
 const typing=['INPUT','TEXTAREA'].includes(document.activeElement?.tagName);
 if(['Delete','Backspace'].includes(e.key)&&!typing&&lamps.some(l=>l.id===selected)){e.preventDefault();closeObjectMenu();askRemove(selected);return}
 if(e.key==='Escape'&&menuOpen){closeObjectMenu();return}
 if(e.key==='Escape'){if(drag){restore(drag.before);drag=null;endDragView()}else deselect()}
 if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='z'&&!typing){e.preventDefault();e.shiftKey?redo():undo()}
});
window.addEventListener('keydown',event=>{
 if(event.key==='Escape'&&pendingDevice){event.preventDefault();event.stopImmediatePropagation();cancelPlacement()}
},true);
document.addEventListener('keydown',e=>{
 if(e.key==='Escape'&&confirmRemove){e.preventDefault();e.stopImmediatePropagation();confirmRemove=null;paintPanel();return}
 if(e.key==='Escape'&&panelKind){e.preventDefault();e.stopImmediatePropagation();closePanel()}
},true);
document.addEventListener('pointerdown',e=>{if(panelKind&&!e.target.closest('#lamp-panel,#list-toggle'))closePanel()},true);
$('#object-menu').addEventListener('click',e=>{const b=e.target.closest('[data-menu]');if(!b||b.disabled)return;e.stopPropagation();menuAction(b.dataset.menu)});
const panel=$('#lamp-panel');
panel.addEventListener('click',e=>{
 if(e.target.closest('[data-close]')){e.stopPropagation();closePanel();return}
 const rm=e.target.closest('[data-remove]');if(rm){e.stopPropagation();confirmRemove=rm.dataset.remove;paintPanel();panel.querySelector('[data-remove-cancel]')?.focus();return}
 if(e.target.closest('[data-remove-cancel]')){e.stopPropagation();const id=confirmRemove;confirmRemove=null;paintPanel();panel.querySelector('[data-list-lamp="'+id+'"]')?.focus();return}
 const ok=e.target.closest('[data-remove-ok]');if(ok){e.stopPropagation();confirmRemove=null;removeLampById(ok.dataset.removeOk);return}
 const back=e.target.closest('[data-restore]');if(back){e.stopPropagation();restoreLamp(back.dataset.restore);return}
 const add=e.target.closest('[data-place]');if(add){e.stopPropagation();beginPlacement(add.dataset.place);return}
 const row=e.target.closest('[data-list-lamp]');if(row){e.stopPropagation();select(row.dataset.listLamp);paintPanel()}
});
panel.addEventListener('mouseover',e=>{const row=e.target.closest('[data-list-lamp]');markLamp(row?.dataset.listLamp)});
panel.addEventListener('mouseleave',()=>markLamp(null));
panel.addEventListener('keydown',e=>{const row=e.target.closest('[data-list-lamp]');if(row&&e.target===row&&(e.key==='Enter'||e.key===' ')){e.preventDefault();select(row.dataset.listLamp);paintPanel()}if(row&&e.target===row&&['Delete','Backspace'].includes(e.key)){e.preventDefault();e.stopPropagation();askRemove(row.dataset.listLamp)}});
$('#list-toggle').addEventListener('click',toggleList);
$('#sync-tool').addEventListener('click',()=>setMode(mode==='sync'?'placement':'sync'));
$('#scene-toggle').addEventListener('click',toggleDarkScene);
$('#undo').addEventListener('click',undo);
$('#redo').addEventListener('click',redo);
$('#empty-add').addEventListener('click',e=>{e.stopPropagation();openAdd()});
$('#sync-status').addEventListener('click',()=>{const st=syncStatus();if(st.lamp)select(st.lamp)});
new MutationObserver(annotateTooltips).observe($('#app'),{subtree:true,childList:true,attributes:true,attributeFilter:['title','aria-label','disabled','aria-pressed','aria-expanded','data-state']});
document.addEventListener('pointermove',event=>{
 if(event.buttons||drag||pendingDevice){hideTooltip();return}
 const target=document.elementFromPoint(event.clientX,event.clientY)?.closest('[data-tooltip]');queueTooltip(target);
},true);
document.addEventListener('pointerleave',hideTooltip,true);
document.addEventListener('focusin',event=>queueTooltip(event.target.closest('[data-tooltip]'),true));
document.addEventListener('focusout',hideTooltip);
document.addEventListener('click',hideTooltip,true);
document.addEventListener('keydown',event=>{if(event.key==='Escape')hideTooltip()});
window.addEventListener('blur',hideTooltip);window.addEventListener('resize',hideTooltip);document.addEventListener('scroll',hideTooltip,true);

// ----------------------------------------------------------------- start --
applyDarkScene();applyMode(mode);render();annotateTooltips();
connectLayout();
if(!document.hidden){watchLive();refreshLive();}
