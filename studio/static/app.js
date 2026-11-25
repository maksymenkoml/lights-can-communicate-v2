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
const VIEW_KEY='light-studio-view-v1',DARK_KEY='light-studio-dark-scene-v1',ROOM_KEY='light-studio-room-v1';
// Each room keeps its own browser draft; the first room keeps the draft from before rooms.
const FIRST_ROOM='room-00000001',ROOM_ID=/^room-[0-9a-f]{8}$/;
const draftKey=id=>!id||id===FIRST_ROOM?'light-studio-grid-v3':'light-studio-grid-v3:'+id;
// The room this browser edits; the server's first room until the rooms are known.
let currentRoom=ROOM_ID.test(store.get(ROOM_KEY)||'')?store.get(ROOM_KEY):null,KEY=draftKey(currentRoom);
// /api/rooms: the rooms in order, the Sync Box's room and where each light is.
let roomsInfo=null,roomsOpen=false,roomEditing=null,roomAdding=false,roomConfirm=null,roomNote=null,roomDragged=null,roomLayouts={},moveOpen=false;
const SAVE_DELAY_MS=300,RETRY_MS=3000,DISCOVERY_POLL_MS=3000;
// About 20 updates a second: Sync frames arrive at 15-25 Hz and a loopback answer takes a few ms.
const LIVE_POLL_MS=50,LIVE_DEADLINE_MS=1000,LIVE_FRESH_MS=1500;
const STEP=5,MAX_ROWS=12,DESK_CENTER=100,FLOOR_ANCHOR=60;
const MONITOR='hue-monitor';
// Adapter keys of the six enrolled lamps: the Govee light id or the Hue role name.
// The six role lamps; the adapter names each by its registry light id.
const ROLES={'govee-left':'govee-left','govee-right':'govee-right','hue-monitor':'hue-monitor','play-left':'play-left','play-center':'play-center','play-right':'play-right'};
// How the six migrated lamps are drawn: their rows store no shape, name or model.
// None of them is required; a room can hold any set of lamps, or none. A row or the
// placement gives the position; scale is the first brightness correction.
const MIGRATED=[
 {id:'govee-left',name:'Govee Left',model:'H6076',shape:'floor',zones:7,scale:65},
 {id:'govee-right',name:'Govee Right',model:'H6076',shape:'floor',zones:7,scale:65},
 {id:'hue-monitor',name:'Monitor Strip',model:'Hue LCX006',shape:'strip',zones:9,scale:100},
 {id:'play-left',name:'Hue Play Left',model:'Hue Play',shape:'bar',zones:1,scale:100},
 {id:'play-center',name:'Hue Play Center',model:'Hue Play',shape:'bar',zones:1,scale:50},
 {id:'play-right',name:'Hue Play Right',model:'Hue Play',shape:'bar',zones:1,scale:100}
];
const SHAPES=['floor','strip','bar'];
const realDevice=id=>/^device-[a-f0-9]{32}$/.test(id||'');
// A lamp the core can drive: a discovered device or one of the six migrated lamps.
const linkable=id=>realDevice(id)||MIGRATED.some(s=>s.id===id);
const virtualDevice=id=>/^virtual-[a-z0-9]{1,32}$/.test(id||'');
const isRole=id=>Object.hasOwn(ROLES,id);
// Lights enrolled in the core, from /api/live: the six migrated lamps always are; a
// device row is connected while the adapter lists it.
let enrolled=new Map();
const connected=l=>isRole(l.id)||enrolled.has(l.id);
const goveeLight=id=>/^govee-/.test(id)||enrolled.get(id)?.provider==='govee';
// Hue lights take names of up to 32 characters; Govee and layout-only names up to 35.
const nameLimit=id=>isRole(id)&&!id.startsWith('govee-')?32:35;
// The drawing form: the monitor strip wraps the monitor; other strips draw as bars.
const formOf=(id,shape)=>id===MONITOR?'strip':shape==='floor'?'floor':'bar';
// During Sync a lamp takes screen colour, keeps its own light, or (role lamps only) is switched off.
const SYNC_ROLES=['sync','own','off'];
function migratedLamp(s){return {...clone(s),sceneScale:s.scale,form:formOf(s.id,s.shape),vertical:false,enabled:true,syncRole:'sync',unbound:false}}
function defaults(){
 // A new room is empty: lamps are placed from the Found list.
 const environment={width:160,height:85,inches:32,screen:{x:0,h:110},zones:{sync:null,scene:null},fade:{sync:true,scene:false}};
 return {room:'Office',environment,lamps:[]};
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
 if(!data||typeof data!=='object'||![3,4,5,6,7].includes(data.version)||!Array.isArray(data.devices)||!data.desk||typeof data.desk!=='object')return null;
 const environment={width:finite(data.desk.width,80,240,160),height:finite(data.desk.height,50,120,85),inches:finite(data.screen?.inches??data.monitorInches,19,55,32),
  zones:{sync:null,scene:null},fade:{sync:data.syncFalloff!==false,scene:data.sceneFalloff===true}};
 for(const [c,key] of [['sync','syncZone'],['scene','sceneZone']])if(validSyncZone(data[key]))environment.zones[c]=data[key].points.map(p=>({u:p.u,h:p.h}));
 const rows=new Map(data.devices.filter(r=>r&&typeof r.id==='string'&&r.positionCm&&typeof r.positionCm==='object').map(r=>[r.id,r]));
 // The screen position: its own object from version 7, before that the monitor row's.
 const screenAt=data.screen?.positionCm??rows.get(MONITOR)?.positionCm;
 if(!screenAt||typeof screenAt!=='object')return null;
 environment.screen={x:finite(screenAt.x,-400,600,DESK_CENTER)-DESK_CENTER,h:finite(screenAt.height,-400,600,110)};
 const place=(lamp,row)=>{
  lamp.x=finite(row.positionCm.x,-400,600,DESK_CENTER)-DESK_CENTER;
  const height=finite(row.positionCm.height,-400,600,0);
  lamp.h=lamp.form==='floor'?height-FLOOR_ANCHOR:height;
  lamp.scale=Math.round(finite(row.scale,0,100,lamp.scale));
  if(isRole(row.id)&&typeof row.name==='string'&&row.name.trim()){lamp.name=row.name.trim().slice(0,nameLimit(row.id));lamp.named=true}
  // The correction without Sync is the lamp's own value; a row without it starts from the Sync one.
  lamp.sceneScale=Math.round(finite(row.sceneScale,0,100,lamp.scale));
  lamp.calibration=calibrationOf(row.calibration);
  lamp.vertical=row.orientation==='vertical';lamp.enabled=row.enabled!==false;lamp.syncRole=SYNC_ROLES.includes(row.syncRole)&&(row.syncRole!=='off'||isRole(row.id))?row.syncRole:'sync';
  clampFixture(lamp,environment);return lamp;
 };
 const lamps=[];
 for(const row of data.devices){
  if(lamps.length>=MAX_ROWS)break;
  const real=realDevice(row?.id),virtual=virtualDevice(row?.id),migrated=MIGRATED.find(s=>s.id===row?.id);
  if(!real&&!virtual&&!migrated||!rows.has(row.id)||lamps.some(l=>l.id===row.id))continue;
  if(migrated){lamps.push(place(migratedLamp(migrated),row));continue}
  const shape=SHAPES.includes(row.shape)?row.shape:'bar';
  const zones=real&&Number.isInteger(row.zones)?clamp(row.zones,1,32):shape==='floor'?7:shape==='strip'?9:1;
  lamps.push(place({id:row.id,name:String(row.name??'New lamp').slice(0,real?80:35),model:real?String(row.model||'').slice(0,64):'Layout only',shape,form:formOf(row.id,shape),zones,unbound:true,scale:80},row));
 }
 return {room:typeof data.room==='string'&&data.room?data.room.slice(0,40):'Office',environment,lamps};
}
function pack(m={room,environment,lamps}){
 const env=m.environment;
 const zone=z=>z?{version:1,points:z.map(p=>({u:p.u,h:p.h}))}:null;
 // The monitor strip row, when there is one, carries the screen position.
 const monitor=m.lamps.find(l=>l.id===MONITOR)||env.screen;
 return {version:7,gridStepCm:STEP,room:m.room,desk:{width:env.width,height:env.height},screen:{positionCm:{x:monitor.x+DESK_CENTER,height:monitor.h},inches:env.inches},
  syncZone:zone(env.zones.sync),sceneZone:zone(env.zones.scene),syncFalloff:env.fade.sync,sceneFalloff:env.fade.scene,
  devices:m.lamps.map(l=>({id:l.id,scale:l.scale,sceneScale:l.sceneScale,positionCm:{x:l.x+DESK_CENTER,height:l.form==='floor'?l.h+FLOOR_ANCHOR:l.h},orientation:l.vertical?'vertical':'horizontal',
   ...(l.enabled===false?{enabled:false}:{}),...((/^govee-/.test(l.id)||realDevice(l.id))&&calibrated(l.calibration)?{calibration:{...l.calibration}}:{}),...(syncRole(l)!=='sync'?{syncRole:syncRole(l)}:{}),...(isRole(l.id)&&l.named?{name:l.name}:{}),
   ...(realDevice(l.id)?{shape:l.shape,name:l.name,model:l.model,zones:l.zones}:virtualDevice(l.id)?{shape:l.shape,name:l.name}:{})}))};
}

let room,environment,lamps;
({room,environment,lamps}=fromLayout(readJson(KEY))||defaults());
let selected=null,mode=store.get(VIEW_KEY)==='sync'?'sync':'placement',past=[],future=[],drag=null,toastTimer;
// The monitor: the strip lamp when the room has one, otherwise the screen alone.
const strip=()=>lamps.find(l=>l.id===MONITOR)||environment.screen;
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
// The Sync view without running Sync previews Sync on every Sync lamp from the demo picture;
// the view without Sync while Sync runs previews the lamps' own light as a demo scene.
function liveColors(l){return syncPreview()?demoColors(l):emittedColors(l)}
// What the lamp emits now, as the adapter reports it.
function emittedColors(l){
 if(l.unbound||l.enabled===false||!connected(l)||!liveFresh()||lampBlocker(l))return null;
 const row=liveSnapshot.lamps&&typeof liveSnapshot.lamps==='object'?liveSnapshot.lamps[l.id]:null;
 if(!row||typeof row!=='object'||row.online!==true||row.on!==true||row.stale!==false||row.included===false)return null;
 // The view without Sync shows the lamps' own light; a lamp that runs Sync shows the demo scene.
 if(row.source==='sync-output'&&mode!=='sync')return sceneDemoColors(l);
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
const BLOCKER_TEXT={owner_off:'off in HomeKit',color_fenced:'color still locked',native_off:'off',native_stale:'not responding',native_foreign:'another app in control'};
const BLOCKER_HINT={owner_off:'HomeKit requests Off, so Sync keeps it off',color_fenced:'The lamp is turning on; color is allowed once power is confirmed',native_off:'The lamp reports that it is off',native_stale:'No recent response from the lamp',native_foreign:'Another app, such as Hue Sync, is streaming to Hue'};
const blockerText=b=>BLOCKER_TEXT[b.code]||'not ready';
const blockerHint=b=>BLOCKER_HINT[b.code]||'The adapter reports a problem with this lamp';
const capital=t=>t[0].toUpperCase()+t.slice(1);
function lampBlocker(l){
 const role=connected(l)?l.id:null;
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
// One Sync status in the corner. With a box, the box decides whether Sync is on;
// the adapter's health speaks only once Sync has had time to start, and never
// while it starts or after it stopped (the stream winding down is not news).
const SYNC_START_MS=20000;
let syncStartedAt=null,syncStartTimer=null;
// The status may change when the start time ends even if nothing else does.
function markSyncStart(){syncStartedAt=performance.now();clearTimeout(syncStartTimer);syncStartTimer=setTimeout(paintStatus,SYNC_START_MS+50)}
function cornerStatus(){
 const st=syncStatus(),s=box.state;
 // Without a box the adapter's view is news only once Sync is attempted (from the Hue app).
 if(s==='unknown'||s==='none')return st.level==='idle'||st.level==='unknown'?{...st,quiet:true}:st;
 const busy=boxBusy??box.changing??null;
 if(s==='offline')return {level:'bad',label:'Sync Box offline',detail:'The Sync Box does not answer'};
 if(s==='unauthorized')return {level:'bad',label:'Sync Box disconnected',detail:'Its access was removed'};
 if(s==='connecting'||s==='timeout'||s==='failed')return {level:'wait',label:'Connecting…',detail:'Connecting the Sync Box'};
 if(s==='ready'&&box.recovery==='restarting')return {level:'wait',label:'Restarting Sync…',detail:'No frames reached the lamps, so Sync starts again'};
 if(busy===true)return {level:'wait',label:'Starting…',detail:'The Sync Box is starting Sync'};
 if(busy===false)return {level:'wait',label:'Stopping…',detail:'The Sync Box is stopping Sync'};
 if(!box.syncing)return {level:'idle',label:'Sync off',detail:'Lamps keep their own light'};
 if(st.level!=='ok'&&syncStartedAt!==null&&performance.now()-syncStartedAt<SYNC_START_MS)return {level:'wait',label:'Starting…',detail:'Waiting for the first frames'};
 if(st.level==='idle'&&st.label==='Not syncing')return {level:'wait',label:'Waiting for frames',detail:'No image from the monitor'};
 return st;
}
function paintStatus(){
 const el=$('#sync-status');if(!el)return;const st=cornerStatus(),label='Sync status: '+st.label;
 if(el.hidden!==!!st.quiet)el.hidden=!!st.quiet;
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
const corr=(l,c=view())=>c==='sync'?l.scale:l.sceneScale;
function setCorr(l,v,c=view()){if(c==='sync')l.scale=v;else l.sceneScale=v}
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
 calibrate:'<circle r="4.6" style="fill:none;stroke:currentColor"/><path d="M0 -4.6A4.6 4.6 0 0 1 0 4.6Z" style="fill:currentColor;stroke:none"/>',
 next:'<path d="M-1.5 -3.5 2 0 -1.5 3.5"/>',back:'<path d="M1.5 -3.5 -2 0 1.5 3.5"/>',down:'<path d="M-3 -1.4 0 1.6 3 -1.4"/>',
 auto:'<path d="M-4 -1.5A4.2 4.2 0 1 1 -2.6 3.2M-4 -1.5V-5M-4 -1.5H-0.6"/>',
 rotate:'<path d="M-3.4 -2.6A4.2 4.2 0 1 1 -3.8 1.9M-3.4 -2.6V-5.8M-3.4 -2.6H-0.2"/>',
 blink:'<path d="M2 0A2 2 0 1 1 -2 0A2 2 0 1 1 2 0M0 -5.6V-4M0 4V5.6M-5.6 0H-4M4 0H5.6M-4 -4l1.1 1.1M2.9 2.9 4 4M4 -4 2.9 -2.9M-2.9 2.9-4 4"/>',
 reset:'<path d="M-4 -1.5A4.2 4.2 0 1 1 -2.6 3.2M-4 -1.5V-5M-4 -1.5H-0.6"/>',
 remove:'<path d="M-4.4 -3H4.4M-2.2 -3V-4.6H2.2V-3M-3.3 -3l.6 7.4H2.7l.6-7.4"/>',
 room:'<path d="M-4.8 -0.4 0 -4.6 4.8 -0.4M-3.5 -1.5V4.6H3.5V-1.5"/>'
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
// Labels in the scene (chips, dimension tags, the placement label) keep the UI's type size:
// the scene scales with the window, so each label is drawn at one screen pixel per unit
// around its anchor. labelUnit is the scene units per screen pixel at the last render.
let labelUnit=1;
function stageUnit(){const m=$('#stage').getScreenCTM?.(),a=m&&m.a;return a>0?1/a:1}
function atScreenSize(x,y,inner){return '<g transform="translate('+x+' '+y+') scale('+(+labelUnit.toFixed(4))+') translate('+(-x)+' '+(-y)+')">'+inner+'</g>'}
// Every object chip has one shape. A lamp chip is one button (name, status, chevron) that
// opens the lamp card; other actions (rotate, fade) sit after a divider.
function chipSvg(centerX,bottom,title,status,actions){
 const more=actions.some(a=>a.id==='more'),rest=actions.filter(a=>a.id!=='more'),bw=24;
 const main=title.length*6.9+(more?14:18)+(status?status.length*6.2+8:0)+(more?20:0),w=main+(rest.length?7+rest.length*bw:0);
 let out='<g class="obj-chip" transform="translate('+centerX+' '+bottom+') scale('+(+labelUnit.toFixed(4))+') translate('+(-w/2)+' -24)"><rect width="'+w+'" height="24" rx="5"/>';
 if(more)out+='<g class="chip-main'+(menuOpen?' open':'')+'" data-chip-action="more" role="button" aria-label="Lamp settings" aria-expanded="'+menuOpen+'"><rect class="chip-hit" x="1" y="1" width="'+(main-2)+'" height="22" rx="4"/>';
 out+='<text x="10" y="15.8">'+esc(title)+(status?'<tspan class="chip-status" dx="7">'+esc(status)+'</tspan>':'')+'</text>';
 if(more)out+='<g class="chev" transform="translate('+(main-13)+' 12)"><path d="M-3.2 -1.4 0 1.6 3.2 -1.4"/></g></g>';
 if(rest.length)out+='<line class="chip-sep" x1="'+(main+3)+'" x2="'+(main+3)+'" y1="5" y2="19"/>';
 rest.forEach((a,i)=>{const cx=main+7+i*bw+bw/2;out+='<g class="chip-btn" data-chip-action="'+a.id+'" data-tooltip="'+a.label+'" aria-label="'+a.label+'"><rect x="'+(cx-11)+'" y="1" width="22" height="22" rx="4"/><g transform="translate('+cx+' 12)">'+chipIcons[a.id]+'</g></g>'});
 return out+'</g>';
}
function editAttrs(edit,value){return edit?' data-edit="'+edit+'" data-value="'+value+'" data-tooltip="Type an exact value"':''}
function tagSvg(x,y,text,edit,value,anchor='middle'){
 const w=text.length*6.3+8,x0=anchor==='end'?x-w:anchor==='start'?x:x-w/2;
 return atScreenSize(x,y,'<g class="tag'+(edit?' editable':'')+'"'+editAttrs(edit,value)+'><rect class="tag-hit" x="'+(x0-3)+'" y="'+(y-10)+'" width="'+(w+6)+'" height="20" rx="6"/><text class="tag-text" x="'+(anchor==='end'?x-4:anchor==='start'?x+4:x)+'" y="'+(y+4)+'" text-anchor="'+anchor+'">'+esc(text)+'</text></g>');
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
// A segmented floor lamp's wall light: its zones from the foot (y 200) to the top (y -220),
// each at the centre of its zone, so the blur blends neighbours as the wall does.
function washStops(colors,zones){
 const cs=colors||Array.from({length:zones},()=>[0,0,0]),n=cs.length,level=lampLevel(colors);
 return cs.map((c,i)=>{const z=zoneStop(c,level);return {offset:(i+.5)/n,color:z.color,opacity:z.opacity}});
}
function washGradient(l,colors){
 if(l.form!=='floor'||l.zones<=1)return '';
 return '<linearGradient id="wash-'+l.id+'" gradientUnits="userSpaceOnUse" x1="0" y1="200" x2="0" y2="-220">'
  +washStops(colors,l.zones).map(z=>'<stop offset="'+z.offset.toFixed(4)+'" stop-color="'+z.color+'" stop-opacity="'+z.opacity+'"/>').join('')+'</linearGradient>';
}
function lensAxis(l){
 if(l.form!=='strip')return 'x1="0" y1="0" x2="0" y2="1"';
 const w=monitorWidthCm(environment.inches)*3.3/2+11;return 'gradientUnits="userSpaceOnUse" x1="'+(-w)+'" y1="0" x2="'+w+'" y2="0"';
}
// Every lamp gives the wall the same amount of light at the same brightness. A Play's
// spot is the reference; a larger wash spreads that light thinner (--wash-a is its opacity
// on the light wall, the dark scene scales the same way). White light is drawn stronger
// (--wash-boost): a pale wash barely shows on the light wall, while it brightens a real one.
const WASH_SPOT=Math.PI*125*100,WASH_LIGHT=.7;
function washShare(area){return +Math.min(1,WASH_LIGHT*WASH_SPOT/area).toFixed(3)}
function washBoost(colors){
 const lit=(colors||[]).filter(c=>Math.max(...c)>0);if(!lit.length)return 1;
 const sat=lit.reduce((a,c)=>a+(Math.max(...c)-Math.min(...c))/Math.max(...c),0)/lit.length;
 return +(1+.8*(1-sat)).toFixed(3);
}
function lampSvg(l){
 const x=500+l.x*3.3,y=740-l.h*3.3-(l.form==='floor'?224:0);
 const colors=liveColors(l),beam=beamColor(colors),power=opacityText(lampLevel(colors));
 let box='',body='',wash='',off='',lens='',hit='';
 const glowOpacity=.21;
 if(l.form==='floor'){
  box='x="-25" y="-239" width="50" height="477"';
  body='<ellipse class="contact-shadow" cx="5" cy="225" rx="28" ry="5"/><ellipse fill="url(#body-metal)" stroke="#526075" stroke-width="1" cx="0" cy="220" rx="20" ry="5"/><path d="M0 218v-17" stroke="#49566c" stroke-width="5"/><rect x="-7.5" y="-225" width="15" height="432" rx="7.5" fill="url(#body-metal)" stroke="#718097" stroke-width=".8"/><path class="object-edge" d="M-5 -215V196"/>';
  // The wall light runs the length of the lamp, as wide at the foot as at the top; a
  // segmented lamp lights the wall next to each zone in that zone's colour.
  const a=washShare(200*482-(4-Math.PI)*100*100),style=' style="--wash-a:'+a+(l.zones>1?';fill:url(#wash-'+l.id+')':'')+'"',dark=+(glowOpacity*a/WASH_LIGHT).toFixed(3);
  wash='<rect class="render-wash" opacity="'+dark+'"'+style+' x="-100" y="-262" width="200" height="482" rx="100"/><ellipse class="render-wash" opacity="'+dark+'"'+style+' cx="0" cy="-10" rx="44" ry="205"/>';
  off='<rect class="lens-off" x="-3.5" y="-220" width="7" height="420" rx="3.5"/>';
  lens='<path class="render-bloom" stroke-width="35" opacity=".3" d="M0 198V-218"'+(l.zones>1?' style="stroke:url(#wash-'+l.id+')"':'')+'/><rect x="-3.5" y="-220" width="7" height="420" rx="3.5" fill="url(#lens-'+l.id+')"/>';
 }else if(l.form==='strip'){
  const w=monitorWidthCm(environment.inches)*3.3/2,h=w*9/16;
  box='x="'+(-w-24)+'" y="'+(-h-24)+'" width="'+(2*w+48)+'" height="'+(2*h+48)+'"';
  const path='M'+(-w-11)+' '+h+'V'+(-h-11)+'H'+(w+11)+'V'+h;
  hit='<path class="hit" d="'+path+'" style="fill:none;stroke:transparent;stroke-width:25;pointer-events:stroke"/>';
  body='<path d="'+path+'" fill="none" stroke="#58667a" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/>';
  const a=washShare((4*h+2*w+44)*94);
  wash='<path class="render-bloom" stroke-width="94" opacity="'+(+(glowOpacity*1.6*a/WASH_LIGHT).toFixed(3))+'" d="'+path+'" style="--wash-a:'+a+';stroke:url(#lens-'+l.id+')"/>';
  off='<path class="lens-off" stroke-width="5" d="'+path+'"/>';
  lens='<path class="render-emission" stroke-width="5" d="'+path+'" style="stroke:url(#lens-'+l.id+')"/><path d="'+path+'" fill="none" stroke="#fff3df" stroke-opacity=".6" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>';
 }else{
  box=l.vertical?'x="-23" y="-55" width="46" height="110"':'x="-55" y="-23" width="110" height="46"';
  const rotation=l.vertical?-90:0;
  body='<g transform="rotate('+rotation+')"><ellipse class="contact-shadow" cx="3" cy="13" rx="43" ry="5"/><rect x="-41" y="-12" width="82" height="24" rx="10" fill="url(#body-metal)" stroke="#64738a" stroke-width="1"/><path class="object-edge" d="M-30 -9h60"/></g>';
  // A Play faces the wall like every other lamp: its light spreads around it, above
  // and below the desktop, longest along the bar.
  wash='<g transform="rotate('+rotation+')"><ellipse class="render-wash" opacity="'+(glowOpacity*1.1)+'" style="--wash-a:'+WASH_LIGHT+'" cx="0" cy="0" rx="125" ry="100"/><ellipse class="render-wash" opacity="'+(glowOpacity*1.2)+'" style="--wash-a:'+WASH_LIGHT+'" cx="0" cy="0" rx="72" ry="48"/></g>';
  off='<g transform="rotate('+rotation+')"><rect class="lens-off" x="-35" y="-7" width="70" height="13" rx="6.5"/></g>';
  lens='<g transform="rotate('+rotation+')"><rect x="-35" y="-7" width="70" height="13" rx="6.5" fill="url(#lens-'+l.id+')"/><path class="render-bloom" stroke-width="29" opacity=".25" d="M-30 -2H30"/></g>';
 }
 const mark=healthMarkSvg(l);
 return '<g class="lamp '+(selected===l.id?'chosen':'')+'" data-lamp="'+l.id+'" transform="translate('+x+' '+y+')" role="button" tabindex="0" aria-label="'+esc(l.name)+'"><g data-beam style="--beam:'+beam+';--wash-boost:'+washBoost(colors)+'" pointer-events="none"><defs><linearGradient id="lens-'+l.id+'" '+lensAxis(l)+'>'+lensStops(l,colors,beam)+'</linearGradient>'+washGradient(l,colors)+'</defs><g data-sync-power opacity="'+power+'">'+wash+'</g>'+body+off+'<g data-sync-power opacity="'+power+'">'+lens+'</g></g>'+(hit||'<rect class="hit" '+box+' rx="8"/>')+'<rect class="outline" '+box+' rx="8" pointer-events="none"/>'+mark+'</g>';
}
const BEZEL=5;
function environmentSvg({cx,cy,mw,mh,w,left,top}){
 const right=left+w,bottom=cy+mh/2,mx=cx-mw/2,my=cy-mh/2;
 // The bloom region is in each lamp's own coordinates, not its bounding box: a floor lamp's
 // glow is a vertical line and a Play's a horizontal one, whose zero-width box would switch the
 // blur off and leave a hard coloured capsule around the lens.
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
const scenePreview=()=>mode!=='sync'&&liveState()==='active'&&activeLamps().some(l=>liveSnapshot.lamps?.[l.id]?.source==='sync-output');
// A warm white, each lamp at its correction without Sync: what the demo scene shows.
const SCENE_DEMO=[255,214,170];
function sceneDemoColors(l){
 if(!connected(l)||l.unbound||l.enabled===false)return null;
 const k=corr(l,'scene')/100;if(!(k>0))return null;
 return Array.from({length:l.zones},()=>SCENE_DEMO.map(v=>Math.round(v*k)));
}
// The view without Sync has no picture; while its scene is a demo the screen says so.
function sceneScreenSvg({cx,cy,mh}){
 return '<g id="scene-screen" pointer-events="none"><text class="screen-demo" x="'+(cx+mh*.03)+'" y="'+cy+'" font-size="'+(+(mh*.2).toFixed(1))+'" text-anchor="middle" dominant-baseline="central" opacity="'+(scenePreview()?1:0)+'" data-scene-demo>Demo</text></g>';
}
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
 if(!connected(l)||l.unbound)return null;
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
 const on=!!selected;
 const keep=new Set([selected]);if(cal?.mode==='test')keep.add(cal.reference);if(selected==='monitor'){keep.add(MONITOR);if(mode==='sync')lamps.forEach(l=>{if(!l.unbound&&l.enabled!==false&&!lampDark(l)&&syncSummary(l).active)keep.add(l.id)})}if(selected===MONITOR)keep.add('monitor');
 const next=new Map(),changed=[],nodes=$('#stage').querySelectorAll('.lamp,.desk-group,.monitor-group');
 nodes.forEach(n=>{
  const key=focusKey(n),want=on&&!keep.has(key)?.22:1,was=focusState.has(key)?focusState.get(key):want;
  next.set(key,want);n.style.transition='none';n.style.opacity=was;if(was!==want)changed.push([n,want]);
 });
 focusState=next;
 requestAnimationFrame(()=>{nodes.forEach(n=>n.style.transition='');requestAnimationFrame(()=>changed.forEach(([n,want])=>n.style.opacity=want))});
}
function renderBaseScene(){
 updateStageViewport();labelUnit=stageUnit();
 const g=sceneGeometry(),{cx}=g;
 let out='<defs><linearGradient id="body-metal" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#344257"/><stop offset=".22" stop-color="#73829a"/><stop offset=".48" stop-color="#a6b2c3"/><stop offset=".62" stop-color="#738198"/><stop offset="1" stop-color="#344257"/></linearGradient><filter id="wall-wash" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="35"/></filter><filter id="bloom" filterUnits="userSpaceOnUse" x="-600" y="-600" width="1200" height="1200"><feGaussianBlur stdDeviation="23"/></filter></defs><path class="floor" d="M70 744H930"/>';
 out+='<path class="desk-axis" d="M500 120V744"/>';if(selected&&Math.abs(cx-500)>1)out+='<path class="axis" d="M'+cx+' 170V744"/>';
 out+=syncVisibilitySvg();
 out+=environmentSvg(g)+(mode==='sync'?syncScreenSvg(g):sceneScreenSvg(g))+activeLamps().map(lampSvg).join('')+(mode==='sync'?syncLinksSvg(g):'')+handlesSvg(g);
 $('#stage').innerHTML=out;applyFocus();
}
const RAY_MS=280;
let rayBorn=new Map(),rayGhosts=[];
function rayElements(root){return root?[...root.querySelectorAll('[data-k]:not(.ray-ghost)')]:[]}
function renderScene(){
 const before=new Map(rayElements($('#sync-links')).map(n=>[n.dataset.k,n.outerHTML]));
 renderBaseScene();paintStatus();paintTitle();layerWashes();animateRays(before);paintPanel();paintEmptyRoom();paintCalibration();
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
// The monitor strip carries the screen position while the room has one; the screen
// keeps it, so removing the strip later leaves the screen where it was.
function syncScreen(){const m=lamps.find(l=>l.id===MONITOR);if(m)environment.screen={x:m.x,h:m.h}}
function render(){syncScreen();renderScene();renderInspector();renderList();renderHistory()}
function renderInspector(){if(menuOpen&&!lamps.some(l=>l.id===selected))closeObjectMenu()}
function paintTitle(){
 const node=$('#room-name');if(node&&node.textContent!==room)node.textContent=room;
 const mark=$('#room-box');if(mark)mark.hidden=!(roomsInfo&&roomsInfo.sync===currentRoom&&roomsInfo.rooms.length>1);
}
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
 const lit=lamp.querySelector('[data-beam]');if(lit)for(const k of ['--beam','--wash-boost'])if(w.style.getPropertyValue(k)!==lit.style.getPropertyValue(k))w.style.setProperty(k,lit.style.getPropertyValue(k));
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
  w.setAttribute('transform',n.getAttribute('transform')||'');w.style.setProperty('--beam',lit.style.getPropertyValue('--beam'));w.style.setProperty('--wash-boost',lit.style.getPropertyValue('--wash-boost'));
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
 return JSON.stringify([syncStatus(),lamps.map(l=>[l.id,!!lampBlocker(l)&&blockerText(lampBlocker(l)),lampDark(l)]),globalHealth().level,screenDemo(),syncPreview(),scenePreview()]);
}
// Telemetry changes existing SVG attributes only: no layout rebuild, no save.
function paintLive(){
 const stage=$('#stage');
 for(const l of activeLamps()){
  const node=stage.querySelector('[data-lamp="'+l.id+'"]');if(!node)continue;
  const colors=liveColors(l),beam=beamColor(colors);
  setVar(node.querySelector('[data-beam]'),'--beam',beam);setVar(node.querySelector('[data-beam]'),'--wash-boost',String(washBoost(colors)));
  const level=lampLevel(colors);
  node.querySelectorAll('[data-sync-power]').forEach(g=>setAttr(g,'opacity',opacityText(level)));
  const stops=node.querySelectorAll('[id="lens-'+l.id+'"] stop');
  const washStopNodes=node.querySelectorAll('[id="wash-'+l.id+'"] stop');
  if(washStopNodes.length)washStops(colors,l.zones).forEach((z,i)=>{setAttr(washStopNodes[i],'stop-color',z.color);setAttr(washStopNodes[i],'stop-opacity',z.opacity)});
  if(l.form==='strip')stripStops(colors).forEach((c,i)=>{const z=zoneStop(c,level);setAttr(stops[i],'stop-color',z.color);setAttr(stops[i],'stop-opacity',z.opacity)});
  else if(l.zones>1){const cs=(colors||Array.from({length:l.zones},()=>[0,0,0])).slice().reverse();cs.forEach((c,i)=>{const z=zoneStop(c,level);for(const stop of [stops[2*i],stops[2*i+1]]){setAttr(stop,'stop-color',z.color);setAttr(stop,'stop-opacity',z.opacity)}})}
  else{setAttr(stops[1],'stop-color',beam);setAttr(stops[2],'stop-color',beam)}
  const badge=node.querySelector('[data-badge]'),b=lampBlocker(l);
  if(badge){setAttr(badge,'visibility',b?'visible':'hidden');setVar(badge,'--h-color',b?levelColor[blockerLevel(b)]:'var(--bad)');setAttr(badge,'data-tooltip',b?capital(blockerText(b)):'')}
 }
 stage.querySelectorAll('#wash-layer [data-wash-for]').forEach(w=>{const lamp=stage.querySelector('[data-lamp="'+w.dataset.washFor+'"]');if(lamp)paintWash(w,lamp)});
 setAttr(stage.querySelector('[data-scene-demo]'),'opacity',scenePreview()?1:0);
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
function applyMode(m,keep=true){
 mode=m;
 $('#sync-tool').classList.toggle('active',m==='sync');$('#sync-tool').setAttribute('aria-pressed',String(m==='sync'));$('#app').classList.toggle('sync-mode',m==='sync');
 if(keep)store.set(VIEW_KEY,m);renderScene();renderInspector();
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

// ------------------------------------------------------ shared controls --
// Every text button, icon button, confirmation and text field is built here and styled
// once ("Shared controls" in style.css), so the same control looks the same everywhere.
// Attribute values are escaped; labels and icons are HTML.
function attrs(a){return Object.entries(a).filter(([,v])=>v!==false&&v!=null).map(([k,v])=>' '+k+(v===true?'':'="'+esc(String(v))+'"')).join('')}
// kind: '' (secondary), 'primary', 'danger' or 'quiet', plus 'sm' in rows and confirmations
// and 'square' for an icon-only button.
function btn(label,a={},kind=''){return '<button type="button" class="btn'+(kind?' '+kind:'')+'"'+attrs(a)+'>'+label+'</button>'}
function iconBtn(icon,label,a={},kind=''){return '<button type="button" class="icon-btn'+(kind?' '+kind:'')+'" aria-label="'+esc(label)+'"'+attrs(a)+'>'+icon+'</button>'}
// A destructive step asks once, in place: the question, then Cancel and the red action.
// A confirm is destructive (danger) unless it only moves something (primary).
function confirmHtml({text,sub='',cancel,ok,okLabel,cls='',a={},label='Confirm',tone='danger'}){
 return '<div class="confirm'+(tone==='primary'?' calm':'')+(cls?' '+cls:'')+'" role="group" aria-label="'+esc(label)+'"'+attrs(a)+'><span class="confirm-text"><b>'+text+'</b>'+(sub?'<small>'+sub+'</small>':'')+'</span>'
  +btn('Cancel',cancel,'sm')+btn(okLabel,ok,'sm '+tone)+'</div>';
}
function field(a,size=''){return '<input type="text" class="field'+(size?' '+size:'')+'"'+attrs(a)+'>'}
function ipField(a){return field({inputmode:'decimal',maxlength:15,autocomplete:'off',spellcheck:'false',...a})}

// ---------------------------------------------------------- lamp card --
function zonesText(n){return n+' '+(n===1?'zone':'zones')}
function lampInfo(l){return l.model+' · '+zonesText(l.zones)}
const reduceMotion=()=>matchMedia('(prefers-reduced-motion:reduce)').matches;
let menuHideTimer=null;
function closeObjectMenu(){
 if(!menuOpen)return;if(cal)closeCalibrate(false);menuOpen=false;connectPrompt=null;moveOpen=false;
 const menu=$('#object-menu'),c=$('#stage').querySelector('[data-chip-action="more"]');c?.classList.remove('open');c?.setAttribute('aria-expanded','false');
 clearTimeout(menuHideTimer);
 if(reduceMotion()){menu.hidden=true;return}
 // The card fades toward its chip, then hides.
 menu.classList.remove('opening');menu.classList.add('closing');
 menuHideTimer=setTimeout(()=>{if(!menuOpen){menu.hidden=true;menu.classList.remove('closing')}},140);
}
function seg(prefix,options,value,disabled){return '<div class="seg" role="group">'+options.map(([v,t])=>'<button data-menu="'+prefix+v+'" aria-pressed="'+(v===value)+'"'+(disabled?' disabled':'')+'>'+t+'</button>').join('')+'</div>'}
function calSummary(l){return corr(l)+'%'+(goveeLight(l.id)&&calibrated(l.calibration)?' · color':'')}
function connectPromptHtml(l){
 return '<div class="card-head"><div class="card-prompt">Connect '+esc(l.name)+'?</div><div class="card-sub">Scenes and Sync will drive it</div></div>'
  +'<div class="card-foot"><span class="card-gap"></span>'+btn('Not now',{'data-menu':'connect-later'})+btn('Connect',{'data-menu':'connect-now'},'primary')+'</div>';
}
function connectedField(l){
 if(!linkable(l.id))return '';
 const link=linkState(l),on=link?link==='connect':!l.unbound;
 const label=link==='connect'?'Connecting…':link==='disconnect'?'Disconnecting…':'Connected';
 return '<div class="card-field" data-connected-field><span>'+label+'</span><button class="card-switch" role="switch" aria-label="Connected" aria-checked="'+on+'" data-menu="connected"'+(link?' disabled':'')+'><span class="switch"><i></i></span></button></div>';
}
function cardMenu(l){
 if(connectPrompt===l.id)return connectPromptHtml(l);
 const b=lampBlocker(l),editable=virtualDevice(l.id)||isRole(l.id);
 return '<div class="card-head"><input class="card-name" value="'+esc(l.name)+'" aria-label="Lamp name" maxlength="'+nameLimit(l.id)+'"'+(editable?'':' readonly')+'>'
  +'<div class="card-sub">'+esc(lampInfo(l))+'</div>'
  +(b?'<div class="card-alert" style="--h-color:'+levelColor[blockerLevel(b)]+'"><i></i><div><b>'+esc(capital(blockerText(b)))+'</b><span>'+esc(blockerHint(b))+'</span></div></div>':'')+'</div>'
  +connectedField(l)
  +(syncHere()?'<div class="card-field'+(l.unbound?' disabled':'')+'"><span>During Sync</span>'+seg('role-',[['sync','Sync'],['own','Own light']].concat(isRole(l.id)?[['off','Off']]:[]),syncRole(l),l.unbound)+'</div>':'')
  +'<div class="card-list"><button class="card-row" data-menu="blink"'+(l.unbound?' disabled title="Not connected"':'')+'>'+iconSvg('blink')+'<span>Blink</span></button>'
  +'<button class="card-row" data-menu="calibrate">'+iconSvg('calibrate')+'<span>Calibrate</span><em>'+calSummary(l)+'</em>'+iconSvg('next')+'</button>'
  +(movable(l)?'<button class="card-row" data-menu="move">'+iconSvg('room')+'<span>Move to room</span>'+iconSvg('next')+'</button>':'')+'</div>'
  +'<div class="card-list card-end"><button class="card-row danger" data-menu="remove">'+iconSvg('remove')+'<span>Remove from layout</span></button></div>';
}
// The card sits beside the selected object, never over it, and does not follow the chip width.
function selectionRect(){
 const nodes=[$('#stage').querySelector('[data-lamp="'+selected+'"] .outline')||$('#stage').querySelector('[data-lamp="'+selected+'"]'),...$('#stage').querySelectorAll('.handle')].filter(Boolean);
 const rs=nodes.map(n=>n.getBoundingClientRect()).filter(r=>r.width||r.height);if(!rs.length)return null;
 return {left:Math.min(...rs.map(r=>r.left)),right:Math.max(...rs.map(r=>r.right)),top:Math.min(...rs.map(r=>r.top)),bottom:Math.max(...rs.map(r=>r.bottom))};
}
// The card opens toward the nearer screen edge, where it covers less of the scene, and
// takes the other side when it does not fit there. While calibrating it leaves the side
// where the reference lamp is.
function placeMenu(menu,anchorRect){
 // A card whose height animates to a new page is placed by its target height.
 const a=$('#app').getBoundingClientRect(),base=selectionRect(),pad=12,gap=12,w=menu.offsetWidth,h=parseFloat(menu.style.height)||menu.offsetHeight;
 menu.style.top=Math.max(pad,Math.min(a.height-h-pad,anchorRect.bottom-a.top+8))+'px';
 const sel=base?{left:Math.min(base.left,anchorRect.left),right:Math.max(base.right,anchorRect.right)}:null;
 if(!sel){menu.style.left=Math.max(pad,Math.min(a.width-w-pad,anchorRect.left+anchorRect.width/2-a.left-w/2))+'px';return}
 const mid=(sel.left+sel.right)/2,right=sel.right-a.left+gap,left=sel.left-a.left-gap-w,fits={left:left>=pad,right:right+w<=a.width-pad};
 let side=mid-a.left<a.width/2?'left':'right';
 const ref=cal?.mode==='test'?$('#stage').querySelector('[data-lamp="'+cal.reference+'"]')?.getBoundingClientRect():null;
 if(ref&&(ref.left+ref.width/2<mid?'left':'right')===side)side=side==='left'?'right':'left';
 if(!fits[side])side=side==='left'?'right':'left';
 menu.style.left=(fits[side]?side==='left'?left:right:Math.max(pad,Math.min(a.width-w-pad,left)))+'px';
 menu.style.transformOrigin=(fits[side]&&side==='left'?'right':'left')+' top';
}
function placeOpenMenu(){
 if(!menuOpen)return;const chip=$('#stage').querySelector('.chip-main')?.closest('.obj-chip');
 if(chip)placeMenu($('#object-menu'),chip.getBoundingClientRect());
}
function openObjectMenu(anchor){
 const l=lamps.find(l=>l.id===selected);if(!l)return;
 if(menuOpen){closeObjectMenu();return}
 menuOpen=true;anchor.classList.add('open');anchor.setAttribute('aria-expanded','true');
 const menu=$('#object-menu');clearTimeout(menuHideTimer);menu.className='object-menu card opening';
 menu.innerHTML='<div class="card-pages"><div class="card-page">'+cardMenu(l)+'</div><div class="card-page"></div></div>';menu.hidden=false;menu.style.height='';
 menu.style.transition='none';placeMenu(menu,anchor.closest('.obj-chip').getBoundingClientRect());void menu.offsetWidth;menu.style.transition='';
 const name=menu.querySelector('.card-name');
 if(name&&!name.readOnly){name.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();renameLamp(l.id,name.value);name.blur()}else if(e.key==='Escape'){name.value=l.name;closeObjectMenu()}});name.addEventListener('change',()=>renameLamp(l.id,name.value))}
}
function refreshCard(){requestAnimationFrame(()=>{closeObjectMenu();const b=$('#stage').querySelector('[data-chip-action="more"]');if(b)openObjectMenu(b)})}
// Layout-only drawings and the six desk lamps carry a stored name (a desk lamp's name also
// renames it in the Hue app and HomeKit); discovered lamps keep their canonical name.
function renameLamp(id,value){
 const l=lamps.find(l=>l.id===id),v=String(value).trim().slice(0,l?nameLimit(l.id):0);if(!l||!(virtualDevice(l.id)||isRole(l.id))||!v||v===l.name)return;
 const before=snapshot();l.name=v;if(isRole(l.id))l.named=true;recordChange(before);renderScene();renderList();renderHistory();
 placeOpenMenu();
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
// ------------------------------------------------------------ connecting --
// Connecting enrolls a found lamp in the core; disconnecting removes it. The core
// restarts with the new light set, so a lamp shows Connecting or Disconnecting
// until /api/live lists it (or no longer lists it).
const LINK_TIMEOUT_MS=90000;
let linking=new Map(),connectPrompt=null;
const LIGHT_REFUSED={unknown_room:'This room no longer exists',sync_active:'Stop Sync to connect or disconnect a lamp',restarting:'The lamps are restarting; try again in a moment',enrolled:'The lamp is already connected',not_enrolled:'The lamp is already disconnected',unavailable:'The lamp does not respond',area_full:'The Hue entertainment area is full',channels_shared:'Hue gives this lamp the same channels as another lamp'};
const linkState=l=>linking.get(l.id)?.kind||null;
// A disconnected migrated lamp is discovered under a device id and named by its row
// (layout_id); the core enrolls that device and gives it its migrated id back.
function enrollId(l){return realDevice(l.id)?l.id:catalogRows().find(d=>d.layout_id===l.id)?.id||null}
async function changeLink(l,kind){
 if(!linkable(l.id)||linking.has(l.id))return;
 const verb=kind==='connect'?'Connect':'Disconnect',id=kind==='connect'?enrollId(l):l.id;
 if(!id){tell(LIGHT_REFUSED.unavailable);return}
 linking.set(l.id,{kind,since:performance.now()});paintLink();
 let status=0,body={};
 try{const r=await fetch(kind==='connect'?'/api/lights/enroll':'/api/lights/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(kind==='connect'&&roomsInfo?{id,room:currentRoom}:{id})});status=r.status;body=await r.json().catch(()=>({}))}catch{}
 // The deadline holds even when /api/live stops answering.
 if(status===200&&body.accepted===true){tell((kind==='connect'?'Connecting ':'Disconnecting ')+l.name);setTimeout(settleLinks,LINK_TIMEOUT_MS+50);return}
 linking.delete(l.id);paintLink();
 tell(status===409?LIGHT_REFUSED[body.reason]||'The lamp cannot be '+(kind==='connect'?'connected':'disconnected')+' now':verb+' failed: the adapter is not responding');
}
// A finished change says so; one the core never confirms gives up after a while.
function settleLinks(){
 let changed=false;
 for(const [id,link] of linking){
  const l=lamps.find(l=>l.id===id),done=link.kind==='connect'?enrolled.has(id):!enrolled.has(id);
  if(done){linking.delete(id);changed=true;if(l)tell((link.kind==='connect'?'Connected ':'Disconnected ')+l.name)}
  else if(performance.now()-link.since>LINK_TIMEOUT_MS){linking.delete(id);changed=true;if(l)tell((link.kind==='connect'?'Connecting ':'Disconnecting ')+l.name+' did not finish')}
 }
 if(changed)paintLink();
}
function paintLink(){if(drag)return;render();paintPanel();if(menuOpen&&!cal)refreshCard()}
function setLamp(l,change,message){remember();change(l);render();if(message)tell(message);refreshCard()}
function menuAction(id){
 const menu=$('#object-menu'),l=lamps.find(l=>l.id===selected);if(!l)return false;
 if(id.startsWith('role-')){const v=id.slice(5);if(SYNC_ROLES.includes(v)&&(v!=='off'||isRole(l.id))&&v!==syncRole(l)&&!l.unbound)setLamp(l,l=>{l.syncRole=v});return true}
 if(id==='remove'){const b=menu.querySelector('[data-menu="remove"]');if(b)b.outerHTML=confirmHtml({text:'Remove '+esc(l.name)+'?',cancel:{'data-menu':'remove-cancel'},ok:{'data-menu':'remove-ok'},okLabel:'Remove'});menu.querySelector('[data-menu="remove-cancel"]')?.focus();return true}
 if(id==='remove-cancel'){const c=menu.querySelector('.confirm');if(c)c.outerHTML='<button class="card-row danger" data-menu="remove">'+iconSvg('remove')+'<span>Remove from layout</span></button>';return true}
 if(id==='remove-ok'){closeObjectMenu();removeLampById(l.id);return true}
 if(id==='connect-now'){connectPrompt=null;refreshCard();changeLink(l,'connect');return true}
 if(id==='connect-later'){connectPrompt=null;closeObjectMenu();return true}
 if(id==='connected'){
  if(linkState(l))return true;
  if(l.unbound){changeLink(l,'connect');return true}
  const f=menu.querySelector('[data-connected-field]');
  // A migrated lamp's HomeKit accessory and Hue app light stay, without a response.
  if(f)f.outerHTML=confirmHtml({text:'Disconnect '+esc(l.name)+'?',sub:realDevice(l.id)?'':'HomeKit will show it as not responding',cancel:{'data-menu':'disconnect-cancel'},ok:{'data-menu':'disconnect-ok'},okLabel:'Disconnect',a:{'data-connected-field':true}});
  menu.querySelector('[data-menu="disconnect-cancel"]')?.focus();return true;
 }
 if(id==='disconnect-cancel'){const f=menu.querySelector('[data-connected-field]');if(f)f.outerHTML=connectedField(l);return true}
 if(id==='disconnect-ok'){changeLink(l,'disconnect');return true}
 if(id==='blink'){if(!l.unbound){const row=menu.querySelector('[data-menu="blink"]');row?.classList.remove('pulsing');void row?.offsetWidth;row?.classList.add('pulsing');setTimeout(()=>row?.classList.remove('pulsing'),1000);blinkLamp(l)}return true}
 if(id==='calibrate'){openCalibrate(l);return true}
 if(id==='move'){openMovePage(l);return true}
 if(id==='move-back'){closeMovePage();return true}
 if(id.startsWith('move-to-')){moveLamp(l,id.slice(8));return true}
 if(!cal)return false;
 if(id==='cal-back'||id==='cal-cancel'){closeCalibrate(false);return true}
 if(id==='cal-save'){closeCalibrate(true);return true}
 if(id==='cal-reset'){cal.correction=100;if(colorShown(cal))cal.values=calibrationOf({});animateCalSliders();calApplyLive();sendCalibration();return true}
 if(id.startsWith('cal-level-')){cal.level=id.slice(10);menu.querySelectorAll('[data-menu^="cal-level-"]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.menu===id));sendCalibration();return true}
 return false;
}

// ---------------------------------------------------------- calibration --
// "Calibrate" slides the lamp card to its second page. With the test ('test'), the lamp
// and a reference show the same test white through an adapter session that Light Studio
// renews every 2 s; the unsaved values apply at once. During Sync ('live') only the
// brightness changes, on the running Sync. A lamp the adapter does not drive ('plain')
// edits its brightness alone.
const CAL_REFUSED={'sync active':'Turn Sync off to calibrate','lamp off':'The lamp is off','reference off':'The reference lamp is off','busy':'Another lamp is being calibrated','writer closed':'The lamp is still turning on','paused':'Light output is paused'};
const CAL_STOPPED={'sync active':'Calibration stopped: Sync started','lamp off':'Calibration stopped: the lamp is off','reference off':'Calibration stopped: the reference lamp is off'};
const CAL_SLIDERS=[['warmth','Warmth','Cooler','Warmer'],['tint','Tint','Greener','Pinker'],['lowLight','Low light','Darker','Brighter']];
let cal=null,calTimer=null,calSending=false,calQueued=false,calLine=Promise.resolve();
const liveLampOff=id=>liveSnapshot?.lamps?.[id]?.on===false;
const calReferences=id=>lamps.filter(l=>connected(l)&&!l.unbound&&l.id!==id&&l.enabled!==false);
const isGoveeRole=l=>goveeLight(l.id);
function defaultReference(l){
 const on=calReferences(l.id).filter(r=>!liveLampOff(r.id)),plays=on.filter(r=>/^play-/.test(r.id));
 return (plays.length?plays:on).sort((a,b)=>Math.abs(a.x-l.x)-Math.abs(b.x-l.x))[0]?.id||null;
}
function calMode(l){return !connected(l)||l.unbound?'plain':liveState()==='active'?'live':'test'}
const calBody=c=>({lamp:c.lamp,reference:c.reference,level:c.level,view:c.view,correction:c.correction,...(goveeLight(c.lamp)?{calibration:{...c.values}}:{})});
const colorShown=c=>c.mode==='test'&&goveeLight(c.lamp);
const calChanged=c=>colorShown(c)&&calibrated(c.values)||c.correction!==100;
async function calPost(path,body){
 try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),keepalive:path.endsWith('/end')});return {status:r.status,body:await r.json().catch(()=>({}))}}
 catch{return {status:0,body:{}}}
}
// Session requests go out one after another: an end never overtakes an update that
// would otherwise start the session again after it ended.
function calSend(path,body){const sent=calLine.then(()=>calPost(path,body));calLine=sent.catch(()=>{});return sent}
async function openCalibrate(l){
 if(cal)return;
 const v=view(),next={lamp:l.id,mode:calMode(l),view:v,level:'bright',reference:null,correction:corr(l,v),start:corr(l,v),values:calibrationOf(l.calibration),before:snapshot()};
 if(next.mode==='test'){
  next.reference=defaultReference(l);if(!next.reference){tell('No reference lamp is on');return}
  const r=await calSend('/api/calibration',calBody(next));
  if(r.status!==200){tell(r.status===409?CAL_REFUSED[r.body.reason]||'Calibration is not possible now':'Calibration failed: the adapter is not responding');return}
  if(cal||!menuOpen||selected!==l.id){if(!cal)calSend('/api/calibration/end',{});return}
 }
 cal=next;
 if(cal.mode==='test')renewCalibration();
 const menu=$('#object-menu'),pages=menu.querySelector('.card-pages');if(!pages)return;
 const [main,page]=pages.children;page.innerHTML=calPageHtml(l);
 menu.style.height=main.offsetHeight+'px';
 requestAnimationFrame(()=>{pages.classList.add('at-cal');menu.style.height=page.offsetHeight+'px';placeOpenMenu();page.querySelector('[data-menu="cal-back"]')?.focus?.({preventScroll:true})});
 render();
}
function closeCalibrate(save,message){
 if(!cal)return;
 const c=cal,l=lamps.find(x=>x.id===c.lamp);
 clearTimeout(calTimer);calTimer=null;calQueued=false;cal=null;
 if(c.mode==='test')calSend('/api/calibration/end',{});
 if(l){
  if(save){if(corr(l,c.view)!==c.correction)setCorr(l,c.correction,c.view);if(colorShown(c))l.calibration={...c.values}}
  else if(c.mode==='live'&&corr(l,c.view)!==c.start)setCorr(l,c.start,c.view);
  recordChange(c.before);
 }
 const menu=$('#object-menu'),pages=menu.querySelector('.card-pages');
 if(menuOpen&&pages&&l){
  const [main,page]=pages.children,sum=main.querySelector('[data-menu="calibrate"] em');if(sum){if(save&&sum.textContent!==calSummary(l)){sum.classList.remove('flash');void sum.offsetWidth;sum.classList.add('flash')}sum.textContent=calSummary(l)}pages.classList.remove('at-cal');menu.style.height=main.offsetHeight+'px';placeOpenMenu();
  setTimeout(()=>{if(!cal){page.innerHTML='';menu.style.height=''}},300);
 }
 render();
 if(message)tell(message);else if(save)tell('Calibration saved');
}
function renewCalibration(){clearTimeout(calTimer);calTimer=setTimeout(()=>{if(cal?.mode!=='test')return;sendCalibration();renewCalibration()},2000)}
async function sendCalibration(){
 if(cal?.mode!=='test')return;if(calSending){calQueued=true;return}
 calSending=true;const sent=cal,r=await calSend('/api/calibration',calBody(cal));calSending=false;
 if(cal!==sent)return;
 if(r.status===409){closeCalibrate(false,CAL_STOPPED[r.body.reason]||'Calibration stopped');return}
 if(r.status!==200){closeCalibrate(false,'Calibration stopped: the adapter is not responding');return}
 if(calQueued){calQueued=false;sendCalibration()}
}
// During Sync the brightness shows on the running Sync: each change is saved at once.
function calApplyLive(){
 if(cal?.mode!=='live')return;const l=lamps.find(x=>x.id===cal.lamp);if(!l)return;
 setCorr(l,cal.correction,cal.view);renderScene();persistIfChanged();
}
function calSlider(key,label,min,max,value,text,sub,ends){
 return '<label class="card-slider"><span class="card-slider-top"><span>'+label+(sub?' <i>'+sub+'</i>':'')+'</span><output>'+text+'</output></span>'
  +'<input type="range" min="'+min+'" max="'+max+'" step="1" value="'+value+'" data-cal="'+key+'" aria-label="'+label+'" style="--p:'+((value-min)/(max-min)*100)+'%">'
  +(ends?'<span class="card-ends"><span>'+ends[0]+'</span><span>'+ends[1]+'</span></span>':'')+'</label>';
}
const calText=(k,v)=>k==='correction'?v+'%':k==='lowLight'?String(v):(v>0?'+':v<0?'−':'')+Math.abs(v);
function calPageHtml(l){
 const c=cal;let html='<div class="card-back">'+iconBtn(iconSvg('back'),'Back',{'data-menu':'cal-back'})+'<div><b>Calibrate</b><div class="card-sub">'+esc(l.name)+'</div></div></div>';
 if(c.mode==='test'){
  const refs=calReferences(l.id).map(r=>'<option value="'+r.id+'"'+(r.id===c.reference?' selected':'')+(liveLampOff(r.id)?' disabled':'')+'>'+esc(r.name)+(liveLampOff(r.id)?' (off)':'')+'</option>').join('');
  html+='<label class="card-field"><span>Compare with</span><span class="card-select"><select class="cal-ref" aria-label="Compare with">'+refs+'</select>'+iconSvg('down')+'</span></label>'
   +'<div class="card-field"><span>Test light</span>'+seg('cal-level-',[['bright','Bright'],['dim','Dim']],c.level)+'</div>';
 }
 html+='<div class="card-sliders">'+calSlider('correction','Brightness',0,100,c.correction,calText('correction',c.correction),c.view==='sync'?'with Sync':'without Sync');
 if(isGoveeRole(l))html+=c.mode==='test'?CAL_SLIDERS.map(([k,label,lo,hi])=>calSlider(k,label,CALIBRATION[k][0],CALIBRATION[k][1],c.values[k],calText(k,c.values[k]),'',[lo,hi])).join(''):c.mode==='live'?'<p class="note card-note">Turn Sync off to calibrate color</p>':'';
 return html+'</div><div class="card-foot">'+btn(iconSvg('reset')+'Reset',{'data-menu':'cal-reset',disabled:!calChanged(c)},'quiet')+'<span class="card-gap"></span>'+btn('Cancel',{'data-menu':'cal-cancel'})+btn('Save',{'data-menu':'cal-save'},'primary')+'</div>';
}
function setSliderView(input,k,v){
 const [lo,hi]=k==='correction'?[0,100]:CALIBRATION[k];
 input.value=String(v);input.style.setProperty('--p',((v-lo)/(hi-lo)*100)+'%');
 const out=input.closest('.card-slider')?.querySelector('output');if(out)out.textContent=calText(k,v);
}
// Reset glides each slider to its new value instead of jumping.
function animateCalSliders(){
 const inputs=[...cardMenuNode.querySelectorAll('[data-cal]')],target=k=>k==='correction'?cal.correction:cal.values[k];
 const from=inputs.map(i=>+i.value),start=performance.now(),ms=reduceMotion()?0:260;
 const reset=cardMenuNode.querySelector('[data-menu="cal-reset"]');if(reset)reset.disabled=!calChanged(cal);
 const step=now=>{
  const t=ms?Math.min(1,Math.max(0,(now-start)/ms)):1,e=1-Math.pow(1-t,3);
  inputs.forEach((input,i)=>{const k=input.dataset.cal;setSliderView(input,k,t<1?Math.round(from[i]+(target(k)-from[i])*e):target(k))});
  if(t<1)requestAnimationFrame(step);
 };
 requestAnimationFrame(step);
}
// The scene shows only the lamp and its reference, both lit with the test white.
function paintCalibration(){
 const app=$('#app'),stage=$('#stage'),test=cal?.mode==='test';
 app.classList.toggle('calibrating',test);
 stage.querySelector('#cal-ref-tag')?.remove();
 const keep=id=>test&&(id===cal.lamp||id===cal.reference);
 stage.querySelectorAll('.lamp[data-lamp]').forEach(n=>n.classList.toggle('cal-keep',keep(n.dataset.lamp)));
 stage.querySelectorAll('[data-wash-for]').forEach(n=>n.classList.toggle('cal-keep',keep(n.dataset.washFor)));
 const r=test&&lamps.find(x=>x.id===cal.reference);
 if(r){const f=lampFrame(r),g=document.createElementNS('http://www.w3.org/2000/svg','g');g.id='cal-ref-tag';g.setAttribute('pointer-events','none');g.innerHTML=tagSvg(f.x,f.top-12,'Compared with','',0);stage.append(g)}
 if(menuOpen)requestAnimationFrame(placeOpenMenu);
}
const cardMenuNode=$('#object-menu');
cardMenuNode.addEventListener('input',e=>{
 const s=e.target.closest('[data-cal]');if(!s||!cal)return;
 const k=s.dataset.cal,v=Math.round(+s.value);
 if(k==='correction')cal.correction=v;else cal.values[k]=v;
 setSliderView(s,k,v);
 const reset=cardMenuNode.querySelector('[data-menu="cal-reset"]');if(reset)reset.disabled=!calChanged(cal);
 if(cal.mode==='test')sendCalibration();else calApplyLive();
});
cardMenuNode.addEventListener('change',e=>{if(!cal||!e.target.matches('.cal-ref'))return;cal.reference=e.target.value;render();sendCalibration()});
window.addEventListener('keydown',e=>{if(e.key==='Escape'&&cal){e.preventDefault();e.stopImmediatePropagation();closeCalibrate(false)}},true);
window.addEventListener('pagehide',()=>{if(cal?.mode==='test')calPost('/api/calibration/end',{})});
// A resized scene moves the object; the card follows it.
window.addEventListener('resize',placeOpenMenu);
// A new window size changes the scene scale: labels are drawn again at their type size.
function rescaleLabels(){if(!drag&&Math.abs(stageUnit()/labelUnit-1)>.01)renderScene()}
if(typeof ResizeObserver==='function')new ResizeObserver(()=>{rescaleLabels();placeOpenMenu()}).observe($('#stage'));
// A double click on a lamp opens its card.
$('#stage').addEventListener('dblclick',e=>{
 const node=e.target.closest('[data-lamp]');if(!node||cal||menuOpen)return;
 requestAnimationFrame(()=>{const chip=$('#stage').querySelector('[data-chip-action="more"]');if(chip&&selected===node.dataset.lamp&&!menuOpen)openObjectMenu(chip)});
});

// ---------------------------------------------------------- lamp panel --
let panelKind=null,confirmRemove=null,pendingDevice=null;
let discovery=null,discoveryTimer=null,discoverySession=0,discoveryLoading=null;
function catalogRows(){return discovery&&discovery.connected===true&&Array.isArray(discovery.devices)?discovery.devices.filter(d=>d&&realDevice(d.id)):[]}
// Discovered lamps that are not in the layout. A migrated lamp is found under its
// registry id (layout_id) and placed with that id; a removed row returns through Found too.
function catalogEntries(){
 return catalogRows().map(d=>{const m=MIGRATED.find(s=>s.id===d.layout_id);return m?{...d,id:m.id,shape:m.shape,zones:m.zones,selectable:true}:d})
  .filter(d=>(!d.layout_id||MIGRATED.some(s=>s.id===d.id))&&!lamps.some(l=>l.id===d.id)&&!elsewhere(d.id))}
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
 const link=linkState(l);if(link)return '<span class="lp-status">'+(link==='connect'?'Connecting…':'Disconnecting…')+'</span>';
 if(l.unbound)return linkable(l.id)?btn('Connect',{'data-connect':l.id},'sm'):'<span class="lp-status">Not connected</span>';
 const b=lampBlocker(l);if(b)return '<span class="lp-status warn" style="--h-color:'+levelColor[blockerLevel(b)]+'">'+esc(capital(blockerText(b)))+'</span>';
 if(mode==='sync'&&syncRole(l)==='own')return '<span class="lp-status">Own light</span>';
 if(mode==='sync'&&syncRole(l)==='off')return '<span class="lp-status">Off during Sync</span>';
 if(mode==='sync'&&!syncSummary(l).active)return '<span class="lp-status">Outside Sync</span>';
 return corr(l)!==100?'<span class="lp-status">'+corr(l)+'%</span>':'';
}
function roomRows(){
 return activeLamps().map(l=>confirmRemove===l.id
  ?confirmHtml({text:'Remove '+esc(l.name)+'?',sub:'⌘Z brings it back',cancel:{'data-remove-cancel':true},ok:{'data-remove-ok':l.id},okLabel:'Remove',cls:'lp-row lamp-row',label:'Confirm removal'})
  :'<div class="lp-row lamp-row'+(selected===l.id?' active':'')+'" role="button" tabindex="0" data-list-lamp="'+l.id+'">'+lampSwatch(l)+'<span><span class="lp-name">'+esc(l.name)+'</span><span class="lp-sub">'+esc(lampInfo(l))+'</span></span>'+(lampRowStatus(l)||'<span></span>')+iconBtn(trashIcon,'Remove '+l.name,{'data-remove':l.id,'data-tooltip':'Remove from layout'},'danger lp-remove')+'</div>').join('')
  +'<div class="lp-divider"></div>'
  +'<button class="lp-row'+(selected==='desk'?' active':'')+'" data-list-lamp="desk"><span class="lp-glyph">'+glyph.desk+'</span><span><span class="lp-name">Desk</span><span class="lp-sub">'+environment.width+' × '+environment.height+' cm</span></span></button>'
  +'<button class="lp-row'+(selected==='monitor'?' active':'')+'" data-list-lamp="monitor"><span class="lp-glyph">'+glyph.monitor+'</span><span><span class="lp-name">Monitor</span><span class="lp-sub">'+environment.inches+'″</span></span></button>'
  +boxHereRow();
}
function foundRows(){
 const back=removedLamps().map(l=>'<div class="lp-row found"><span class="lp-glyph">'+glyph[l.shape]+'</span><span><span class="lp-name">'+esc(l.name)+'</span><span class="lp-sub">'+esc(lampInfo(l))+'</span></span>'+btn(plusIcon,{'data-restore':l.id,'aria-label':'Return '+l.name,'data-tooltip':'Return to its place'},'sm square')+'</div>').join('');
 const items=catalogEntries();
 if(!items.length&&!back){
  if(!discovery)return '<div class="lp-empty">Searching for lamps…</div>';
  if(discovery.connected!==true)return '<div class="lp-empty">Lamp search is unavailable</div>';
  return '<div class="lp-empty">Every found lamp is in the room</div>';
 }
 return back+items.map(e=>{const reason=catalogReason(e);return '<div class="lp-row found'+(reason?' unavailable':'')+'"><span class="lp-glyph">'+(glyph[e.shape]||glyph.bar)+'</span><span><span class="lp-name">'+esc(e.name)+'</span><span class="lp-sub">'+esc(e.model)+(Number.isInteger(e.zones)?' · '+zonesText(e.zones):'')+'</span></span>'+(reason?'<span class="lp-status">'+reason+'</span>':btn(plusIcon,{'data-place':e.id,'aria-label':'Place '+e.name,'data-tooltip':'Place in the room'},'sm square'))+'</div>'}).join('');
}
// The physical Hue bridge: Light Studio pairs it after its link button is pressed.
// The keys stay in the core's private state; the page only sees name and address.
let hueBridge={state:'unknown'},hueFlow=false,hueFound=null,hueAddress='',hueError='',hueTimer=null,hueBusy=false;
const HUE_PAIR_S=30;
const hueGlyph='<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="3.5" y="3.5" width="13" height="13" rx="3.5"/><circle cx="10" cy="10" r="2.6"/></svg>';
const HUE_RESULTS={timeout:'The button was not pressed in time.',failed:'The bridge did not accept the connection.'};
async function hueCall(path,body){
 try{const r=await fetch(path,body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return {status:r.status,body:await r.json().catch(()=>({}))}}
 catch{return {status:0,body:{}}}
}
async function refreshHueBridge(){
 const r=await hueCall('/api/hue-bridge');
 const before=hueBridge.state;
 if(r.status===200&&typeof r.body.state==='string')hueBridge=r.body;
 // A new pairing restarts the core with the bridge: look for its lamps again.
 if(before==='pairing'&&hueBridge.state==='paired'){hueFlow=false;startDiscovery()}
 if(hueBridge.state!=='pairing'&&hueBridge.result&&HUE_RESULTS[hueBridge.result])hueError=HUE_RESULTS[hueBridge.result];
 clearTimeout(hueTimer);hueTimer=hueBridge.state==='pairing'&&panelKind==='list'?setTimeout(()=>refreshHueBridge().then(paintPanel),1000):null;
}
async function findHueBridges(){
 hueFlow=true;hueFound=null;hueError='';paintPanel();
 const r=await hueCall('/api/hue-bridge/find',{});
 hueFound=r.status===200&&Array.isArray(r.body.bridges)?r.body.bridges:[];paintPanel();
 if(!hueFound.length)$('#lamp-panel [data-hue-input]')?.focus();
}
async function pairHueBridge(ip){
 if(hueBusy)return;hueBusy=true;hueError='';
 const r=await hueCall('/api/hue-bridge/pair',{ip});hueBusy=false;
 if(r.status===200){hueBridge=r.body;hueFlow=false;await refreshHueBridge()}
 else hueError=r.status===400?'Enter an address on this network.':r.status===409?'Another Hue bridge has connected lamps. Remove them first.':'No Hue bridge answers at this address.';
 paintPanel();
}
function hueRows(){
 const b=hueBridge;if(b.state==='unknown')return '';
 if(b.state==='configured')return '<div class="lp-row found"><span class="lp-glyph">'+hueGlyph+'</span><span><span class="lp-name">Hue bridge</span><span class="lp-sub">Set in the service configuration</span></span><span class="lp-status">Connected</span></div>';
 const error=hueError?'<p class="note bad lp-hue-error" role="alert">'+boxSvg('alert')+'<span>'+esc(hueError)+'</span></p>':'';
 if(b.state==='pairing'){
  const left=Math.max(0,Math.min(HUE_PAIR_S,b.left|0)),share=left/HUE_PAIR_S;
  return '<div class="lp-row hue-pairing"><div class="box-ring"><svg viewBox="0 0 44 44" aria-hidden="true"><circle class="box-track" cx="22" cy="22" r="19"/><circle class="box-arc" cx="22" cy="22" r="19" style="stroke-dashoffset:'+(BOX_RING*(1-share)).toFixed(1)+'"/></svg><b>'+left+'</b></div><span><span class="lp-name">Press the button on the bridge</span><span class="lp-sub">'+esc(b.name)+' · '+esc(b.ip)+'</span></span>'+btn('Cancel',{'data-hue':'cancel'},'sm')+'</div>';
 }
 // Another bridge, or the same one at a new address, is paired through the same steps;
 // the core refuses another bridge while its Hue lamps are connected.
 if(b.state==='paired'&&!hueFlow)return '<div class="lp-row found"><span class="lp-glyph">'+hueGlyph+'</span><span><span class="lp-name">'+esc(b.name)+'</span><span class="lp-sub">'+esc(b.ip)+'</span></span>'+btn('Change',{'data-hue':'find','data-tooltip':'Pair a bridge again'},'sm')+'</div>'+error;
 if(!hueFlow)return '<div class="lp-row found"><span class="lp-glyph">'+hueGlyph+'</span><span><span class="lp-name">Hue bridge</span><span class="lp-sub">Connect it to add Hue lamps</span></span>'+btn('Connect',{'data-hue':'find'},'sm')+'</div>'+error;
 const rows=(hueFound||[]).map(f=>'<button class="lp-row found" data-hue-ip="'+esc(f.ip)+'"><span class="lp-glyph">'+hueGlyph+'</span><span><span class="lp-name">'+esc(f.name)+'</span><span class="lp-sub">'+esc(f.ip)+'</span></span><span class="lp-status">Pair</span></button>').join('');
 const hint=hueFound===null?'Looking on this network…':hueFound.length?'Found on this network:':'None found. Enter its IP address:';
 return '<div class="lp-empty">'+hint+'</div>'+rows
  +'<div class="lp-hue-address">'+ipField({'data-hue-input':true,placeholder:hueFound?.length?'Or an IP address':'IP address','aria-label':'Hue bridge IP address',value:hueAddress})+btn('Pair',{'data-hue':'address'},'primary')+btn('Cancel',{'data-hue':'close'})+'</div>'+error;
}
function hueSection(){
 if(hueBridge.state==='unknown')return '';
 return '<div class="lp-section"><span>Hue bridge</span><span>'+(['paired','configured'].includes(hueBridge.state)?'Connected':hueBridge.state==='pairing'?'Pairing':'Not connected')+'</span></div>'+hueRows();
}
function hueClick(e){
 const ip=e.target.closest('[data-hue-ip]');if(ip){e.stopPropagation();pairHueBridge(ip.dataset.hueIp);return true}
 const b=e.target.closest('[data-hue]');if(!b)return false;e.stopPropagation();
 const action=b.dataset.hue;
 if(action==='find')findHueBridges();
 else if(action==='address')pairHueBridge(hueAddress.trim());
 else if(action==='close'){hueFlow=false;hueError='';paintPanel()}
 else if(action==='cancel')hueCall('/api/hue-bridge/cancel',{}).then(r=>{if(r.status===200)hueBridge=r.body;hueError='';paintPanel()});
 return true;
}
// The Sync Box is a device of one room. Another room adds it, or moves it in after a
// confirm; Sync, its settings and its Start/Stop then follow it. Moving it never asks for a
// new pairing in the Hue app: the stream keeps its group, only the driven lamps change.
let boxRoomAsk=false,boxRoomBusy=false;
function boxRoomName(){return roomsInfo?.rooms.find(r=>r.id===roomsInfo.sync)?.name||''}
function boxRow(sub,action,cls='lp-row found'){
 const name=box.state!=='none'?esc(box.name||'Sync Box'):'Sync Box';
 return '<div class="'+cls+'" data-box-row><span class="lp-glyph">'+boxSvg('box')+'</span><span><span class="lp-name">'+name+'</span><span class="lp-sub">'+sub+'</span></span>'+(action||'<span></span>')+'</div>';
}
// In its own room the box is listed with the desk and the monitor.
function boxHereRow(){
 if(!roomsInfo||box.state==='unknown'||roomsInfo.sync!==currentRoom)return '';
 if(box.state==='none')return boxRow('Not connected',btn('Connect',{'data-box-room':'connect'},'sm'),'lp-row');
 if(box.state==='unauthorized')return boxRow('Needs connecting again',btn('Reconnect',{'data-box-room':'connect'},'sm'),'lp-row');
 return boxRow('Syncs this room with the screen','','lp-row');
}
function boxSection(){
 if(!roomsInfo||box.state==='unknown'||roomsInfo.sync===currentRoom)return '';
 const paired=box.state!=='none',elsewhere=boxRoomName(),row=boxRow;
 let body;
 if(boxRoomAsk)body=confirmHtml({text:(elsewhere?'Move':'Add')+' the Sync Box here?',sub:elsewhere?'Sync leaves '+esc(elsewhere)+' and drives the lamps of this room':'Sync drives the lamps of this room',
  cancel:{'data-box-room':'cancel'},ok:{'data-box-room':'place'},okLabel:elsewhere?'Move here':'Add here',cls:'lp-row',label:'Confirm the Sync Box room',tone:'primary'});
 else if(elsewhere)body=row('In '+esc(elsewhere)+(paired?'':' · not connected'),btn('Move here',{'data-box-room':'ask'},'sm'));
 else body=row(paired?'In no room':'Syncs a room with the screen',btn(paired?'Add here':'Add Sync Box',{'data-box-room':'ask'},'sm'));
 return '<div class="lp-section"><span>Sync Box</span><span>'+(elsewhere?'In another room':'Not in a room')+'</span></div>'+body;
}
async function placeBoxHere(){
 if(boxRoomBusy)return;boxRoomBusy=true;
 const moved=!!roomsInfo?.sync;
 const ok=await roomChange('sync-box',{room:currentRoom});
 boxRoomBusy=false;boxRoomAsk=false;
 if(!ok){paintPanel();return}
 if(box.state==='none'||box.state==='unauthorized'){closePanel();openBoxCard('connect');return}
 paintPanel();tell(moved?'Sync Box moved here':'Sync Box added here');
}
function boxRoomClick(e){
 const b=e.target.closest('[data-box-room]');if(!b)return false;e.stopPropagation();
 const action=b.dataset.boxRoom;
 if(action==='ask'){boxRoomAsk=true;paintPanel();$('#lamp-panel [data-box-room="cancel"]')?.focus()}
 else if(action==='cancel'){boxRoomAsk=false;paintPanel()}
 else if(action==='place')placeBoxHere();
 else if(action==='connect'){closePanel();openBoxCard('connect')}
 return true;
}
function foundCount(){return catalogEntries().filter(e=>!catalogReason(e)).length+removedLamps().length}
const PANEL_FOCUS=['data-box-room','data-hue-input','data-hue','data-hue-ip','data-connect','data-list-lamp','data-remove','data-remove-cancel','data-remove-ok','data-restore','data-place','data-close'];
function paintPanel(){
 const p=$('#lamp-panel');if(!p||p.hidden)return;const top=p.scrollTop;
 const active=p.contains(document.activeElement)?document.activeElement:null;
 const focusAttr=active&&PANEL_FOCUS.find(a=>active.hasAttribute(a)),focusValue=focusAttr&&active.getAttribute(focusAttr);
 p.innerHTML='<div class="lp-head"><h2>Lamps</h2>'+iconBtn(closeIcon,'Close',{'data-close':true,'data-tooltip':'Close · Esc'})+'</div>'
  +'<div class="lp-section"><span>In the room</span><span>'+activeLamps().length+'</span></div>'+roomRows()
  +'<div class="lp-section" id="lp-found"><span>Found</span><span>'+foundCount()+' available</span></div>'+foundRows()
  +boxSection()+hueSection();
 p.scrollTop=top;
 const caret=active?.selectionStart;
 if(focusAttr){const next=p.querySelector('['+focusAttr+(focusValue?'="'+focusValue+'"':'')+']');next?.focus({preventScroll:true});if(next&&Number.isInteger(caret))next.setSelectionRange(caret,caret)}
}
function placePanel(){
 const p=$('#lamp-panel'),a=$('#app').getBoundingClientRect(),r=$('#list-toggle').getBoundingClientRect();
 p.style.left=(r.left-a.left)+'px';p.style.bottom=(a.bottom-r.top+8)+'px';
}
function markLamp(id){$('#stage').querySelectorAll('.lamp.list-hover').forEach(n=>n.classList.remove('list-hover'));if(id)$('#stage').querySelector('[data-lamp="'+id+'"]')?.classList.add('list-hover')}
function openPanel(kind){
 cancelPlacement();closeRooms();panelKind=kind;refreshRooms().then(paintPanel);const p=$('#lamp-panel');p.hidden=false;p.scrollTop=0;paintPanel();p.scrollTop=0;placePanel();
 $('#list-toggle').setAttribute('aria-expanded','true');$('#list-toggle').classList.add('active');
 startDiscovery();
}
function closePanel(){
 confirmRemove=null;boxRoomAsk=false;$('#lamp-panel').hidden=true;panelKind=null;markLamp(null);stopDiscovery();
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
function startDiscovery(){discoverySession++;clearTimeout(discoveryTimer);refreshDiscovery(discoverySession);refreshHueBridge().then(paintPanel)}
function stopDiscovery(){discoverySession++;clearTimeout(discoveryTimer);discoveryTimer=null;clearTimeout(hueTimer);hueTimer=null}
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
 ghost.innerHTML=lampSvg(lamp)+atScreenSize(f.x,f.top-10,'<text class="ghost-label" x="'+f.x+'" y="'+(f.top-10)+'">'+esc(pendingDevice.name)+'<tspan class="sub"> · '+off+'</tspan></text>');
}
function placeDevice(entry,position){
 if(lamps.length>=MAX_ROWS||lamps.some(l=>l.id===entry.id))return;
 if(roomsInfo&&roomOf(entry.id)!==currentRoom){
  const room=currentRoom;
  roomChange('move',{light:entry.id,room}).then(ok=>{if(ok&&room===currentRoom&&roomOf(entry.id)===room)placeDevice(entry,position);else cancelPlacement()});
  return;
 }
 remember();
 const m=MIGRATED.find(s=>s.id===entry.id);
 // The monitor strip wraps the screen, so it takes the screen's place.
 // A migrated lamp is connected unless the core lists its lights without it.
 const listed=liveSnapshot?.connected===true&&Array.isArray(liveSnapshot.lights);
 const l=m?{...migratedLamp(m),...(m.id===MONITOR?{x:environment.screen.x,h:environment.screen.h}:{x:position.x,h:position.h}),unbound:listed&&!enrolled.has(m.id)}
  :{id:entry.id,name:entry.name,model:entry.model,shape:entry.shape,form:entry.form,zones:entry.zones,x:position.x,h:position.h,scale:80,sceneScale:80,vertical:false,enabled:true,syncRole:'sync',unbound:!enrolled.has(entry.id)};
 clampFixture(l);lamps.push(l);selected=l.id;
 cancelPlacement();render();
 // A lamp the core already drives needs no prompt.
 if(!l.unbound){tell('Added to layout');return}
 // The prompt opens beside the new lamp; Not now leaves it in the layout, not connected.
 connectPrompt=l.id;requestAnimationFrame(()=>{const chip=$('#stage').querySelector('[data-chip-action="more"]');if(chip&&selected===l.id&&!menuOpen)openObjectMenu(chip);else connectPrompt=null});
}

// ----------------------------------------------------------- persistence --
let serverReady=false,revision=null,pendingLayout=null,saving=false,saveTimer,retryTimer,offlineNotice=false,editGeneration=0,flushDone=Promise.resolve(),roomGeneration=0;
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
 // Files before version 7 carry the screen as monitorInches and the monitor row.
 const screenOf=l=>l.screen??{positionCm:clone(l.devices.find(d=>d.id===MONITOR).positionCm),inches:l.monitorInches};
 result.screen=screenOf(remote);delete result.monitorInches;
 field(result,'room',base.room,local.room);
 field(result,'screen',screenOf(base),screenOf(local));
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
 saving=true;let finish;flushDone=new Promise(resolve=>finish=resolve);
 try{
  let conflicts=0;
  while(pendingLayout){
   const payload=pendingLayout,base=clone(serverBaseline);pendingLayout=null;
   const response=await fetch(layoutUrl(),{method:'POST',headers:{'Content-Type':'application/json','If-Match':'"'+(revision||'')+'"'},body:JSON.stringify(payload)});
   if(response.status===409&&conflicts++<2){
    const current=await fetch(layoutUrl(),{cache:'no-store'});if(!current.ok)throw new Error('offline');
    reconcile(await current.json(),base);pendingLayout=pack(committedModel());continue;
   }
   if(!response.ok){pendingLayout=pendingLayout||payload;throw new Error('save');}
   acceptBaseline(await response.json());if(!pendingLayout)store.remove(KEY+'-pending');offlineNotice=false;
  }
  setDraftState('saved');
 }catch{
  serverReady=false;setDraftState('error');if(!offlineNotice)tell('Changes are not saved yet. They are kept in this browser.');offlineNotice=true;
  clearTimeout(retryTimer);retryTimer=setTimeout(connectLayout,RETRY_MS);
 }finally{saving=false;finish();}
}
async function connectLayout(retried=false){
 const generation=roomGeneration;
 try{
  if(!await refreshRooms(false))throw new Error('offline');
  if(generation!==roomGeneration)return;
  const first=roomsInfo.rooms[0].id;
  if(!currentRoom&&draftKey(first)===KEY){currentRoom=first;applyRoomChrome()}
  // A room deleted elsewhere: this browser edits the first room instead.
  else if(!roomsInfo.rooms.some(r=>r.id===currentRoom)){enterRoom(first);return connectLayout(retried)}
  const response=await fetch(layoutUrl(),{cache:'no-store'});
  if(generation!==roomGeneration)return;
  if(response.status===404&&!retried)return connectLayout(true);
  if(!response.ok)throw new Error('offline');
  const saved=await response.json(),hasLocalEdits=editGeneration>0||!!store.get(KEY+'-pending');
  if(generation!==roomGeneration)return;
  revision=saved.revision;
  if(saved.layout){
   if(hasLocalEdits)reconcile(saved);
   else{const m=fromLayout(saved.layout);if(m)applyModel(m);acceptBaseline(saved);pendingLayout=null;store.set(KEY,JSON.stringify(pack()));render();}
  }
  serverReady=true;offlineNotice=false;
  // The first load imports this browser's draft when the server has no layout yet.
  if(!saved.layout||hasLocalEdits)save();else setDraftState('saved');
 }catch{
  if(generation!==roomGeneration)return;
  serverReady=false;setDraftState('error');if(!offlineNotice)tell('Light Studio server is unavailable. The layout is kept in this browser.');offlineNotice=true;
  clearTimeout(retryTimer);retryTimer=setTimeout(connectLayout,RETRY_MS);
 }
}

// -------------------------------------------------------------- live poll --
// A device row is connected while the core lists its light; an unreachable core keeps
// the last known state.
function applyEnrollment(){
 const list=liveSnapshot?.connected===true&&Array.isArray(liveSnapshot.lights)?liveSnapshot.lights:null;
 if(!list)return;
 const next=new Map(list.filter(r=>r&&typeof r.id==='string'&&['govee','hue'].includes(r.provider)).map(r=>[r.id,r]));
 let changed=false;
 for(const l of lamps)if(linkable(l.id)){const unbound=!next.has(l.id);if(l.unbound!==unbound){l.unbound=unbound;changed=true}}
 enrolled=next;
 if(changed&&!drag){render();paintPanel()}
 settleLinks();
}
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
  liveSnapshot=snapshot&&typeof snapshot==='object'?snapshot:null;liveAt=performance.now();applyEnrollment();watchLive();
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
 if(node.id==='sync-status')return cornerStatus().detail;
 // Every control in the box card says what it does in its own words.
 if(node.closest('#box-card'))return '';
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
 const grip=e.target.closest('[data-handle]');
 // The lamp card stays open until its chip or Esc closes it: a stray click must not lose
 // a setting. Another lamp takes the card over, except during a calibration.
 if(menuOpen&&!grip&&!keepsCard(e)){e.preventDefault();skipClick=true;return}
 if(grip){e.preventDefault();skipClick=true;drag={handle:grip.dataset.handle,before:snapshot(),moved:false};$('#app').classList.add('is-dragging');stage.setPointerCapture(e.pointerId);return}
 const node=e.target.closest('[data-lamp]')||e.target.closest('[data-select="monitor"]');
 if(!node){if(e.target.closest('[data-select]'))return;deselect();return}
 selected=node.dataset.select==='monitor'?'monitor':node.dataset.lamp;
 const l=movableFixture();if(!l)return;
 drag={start:point(e),x:l.x,h:l.h,before:snapshot(),moved:false};$('#app').classList.add('is-dragging');stage.setPointerCapture(e.pointerId);renderInspector();renderList();
 stage.querySelectorAll('[data-lamp],[data-select="monitor"]').forEach(n=>n.classList.toggle('chosen',n.dataset.lamp===selected||selected==='monitor'&&n.dataset.select==='monitor'));
});
function keepsCard(e){
 const node=e.target.closest('[data-lamp]'),id=node?.dataset.lamp;
 if(id===selected)return true;
 if(!id||cal||!lamps.some(l=>l.id===id))return false;
 selected=id;render();refreshCard();return false;
}
stage.addEventListener('pointermove',e=>{
 if(!drag)return;if(drag.handle){dragHandle(drag.handle,point(e));placeOpenMenu();return}
 const l=movableFixture();if(!l)return;const p=point(e),oldX=l.x,oldH=l.h;
 l.x=drag.x+(p.x-drag.start.x)/3.3;l.h=drag.h-(p.y-drag.start.y)/3.3;clampFixture(l);
 if(l.x===oldX&&l.h===oldH)return;
 drag.moved=l.x!==drag.x||l.h!==drag.h;
 renderScene();refreshHandles();renderInspector();placeOpenMenu();
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
 if(hueClick(e)||boxRoomClick(e))return;
 const rm=e.target.closest('[data-remove]');if(rm){e.stopPropagation();confirmRemove=rm.dataset.remove;paintPanel();panel.querySelector('[data-remove-cancel]')?.focus();return}
 if(e.target.closest('[data-remove-cancel]')){e.stopPropagation();const id=confirmRemove;confirmRemove=null;paintPanel();panel.querySelector('[data-list-lamp="'+id+'"]')?.focus();return}
 const ok=e.target.closest('[data-remove-ok]');if(ok){e.stopPropagation();confirmRemove=null;removeLampById(ok.dataset.removeOk);return}
 const back=e.target.closest('[data-restore]');if(back){e.stopPropagation();restoreLamp(back.dataset.restore);return}
 const add=e.target.closest('[data-place]');if(add){e.stopPropagation();beginPlacement(add.dataset.place);return}
 const link=e.target.closest('[data-connect]');if(link){e.stopPropagation();const l=lamps.find(l=>l.id===link.dataset.connect);if(l)changeLink(l,'connect');return}
 const row=e.target.closest('[data-list-lamp]');if(row){e.stopPropagation();select(row.dataset.listLamp);paintPanel()}
});
panel.addEventListener('input',e=>{if(e.target.matches('[data-hue-input]')){hueAddress=e.target.value;if(hueError){hueError='';e.target.closest('.lp-hue-address')?.nextElementSibling?.remove()}}});
panel.addEventListener('keydown',e=>{if(e.target.matches('[data-hue-input]')&&e.key==='Enter'){e.preventDefault();e.stopPropagation();pairHueBridge(hueAddress.trim())}},true);
panel.addEventListener('mouseover',e=>{const row=e.target.closest('[data-list-lamp]');markLamp(row?.dataset.listLamp)});
panel.addEventListener('mouseleave',()=>markLamp(null));
panel.addEventListener('keydown',e=>{const row=e.target.closest('[data-list-lamp]');if(row&&e.target===row&&(e.key==='Enter'||e.key===' ')){e.preventDefault();select(row.dataset.listLamp);paintPanel()}if(row&&e.target===row&&['Delete','Backspace'].includes(e.key)){e.preventDefault();e.stopPropagation();askRemove(row.dataset.listLamp)}});
$('#list-toggle').addEventListener('click',toggleList);
$('#sync-tool').addEventListener('click',()=>setMode(mode==='sync'?'placement':'sync'));
$('#scene-toggle').addEventListener('click',toggleDarkScene);
$('#undo').addEventListener('click',undo);
$('#redo').addEventListener('click',redo);
$('#empty-add').addEventListener('click',e=>{e.stopPropagation();openAdd()});
$('#sync-status').addEventListener('click',()=>{const st=cornerStatus();if(st.lamp)select(st.lamp);else if(['ready','offline','unauthorized'].includes(box.state))boxCard==='info'?closeBoxCard():openBoxCard('info')});
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

// ---------------------------------------------------------------- Sync Box --
// The server owns the box and its token. The page sees public states only:
// none, connecting, timeout, failed, ready, offline, unauthorized (unknown until the first answer).
// Everything about the box, messages included, happens inside the corner block.
const BOX_POLL_MS=3000,BOX_FAST_MS=1000,BOX_PAIR_S=30,BOX_RING=119.4,BOX_SAY_MS=4000;
const boxIcons={
 box:'<rect x="3" y="7" width="14" height="6.5" rx="1.6"/><path d="M6 10.2h.01M14 10.2h2"/>',
 link:'<path d="M8.5 11.5l3-3M7 9l-1.8 1.8a2.6 2.6 0 0 0 3.7 3.7L10.7 13M13 11l1.8-1.8a2.6 2.6 0 0 0-3.7-3.7L9.3 7"/>',
 play:'<path d="M7 5.2v9.6L15 10z" fill="currentColor"/>',
 stop:'<rect x="6" y="6" width="8" height="8" rx="1.3" fill="currentColor"/>',
 check:'<path d="m5 10.5 3.2 3L15 6.5"/>',
 alert:'<circle cx="10" cy="10" r="7"/><path d="M10 6.5v4.2M10 13.4h.01"/>'};
const boxSvg=id=>'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+boxIcons[id]+'</svg>';
let box={state:'unknown'},boxTimer=null,boxGeneration=0,boxBusy=null,boxCard=null,boxFound=null,boxForget=false,boxHtml='',boxAddress='',boxError='',boxSaid=null,boxSayTimer=null;
const boxPairing=()=>box.state==='connecting'||box.state==='timeout'||box.state==='failed';
async function boxPost(action,body={}){
 try{const r=await fetch('/api/sync-box/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return {status:r.status,body:await r.json().catch(()=>({}))}}
 catch{return {status:0,body:{}}}
}
function scheduleBox(){clearTimeout(boxTimer);if(!document.hidden)boxTimer=setTimeout(refreshBox,box.state==='connecting'?BOX_FAST_MS:BOX_POLL_MS)}
async function refreshBox(){
 clearTimeout(boxTimer);const generation=++boxGeneration;
 let next={state:'unknown'};
 try{const r=await fetch('/api/sync-box',{cache:'no-store'});if(r.ok)next=await r.json()}catch{}
 if(generation!==boxGeneration)return;
 setBox(next);scheduleBox();
}
// An action answer is newer than any poll still on its way.
function applyBox(next){++boxGeneration;if(next&&typeof next.state==='string')setBox(next);scheduleBox()}
function setBox(next){
 const was=box.state,wasSyncing=box.syncing===true,wasRecovery=box.recovery;box=next&&typeof next.state==='string'?next:{state:'unknown'};
 // Sync that starts while this page watches gets time for its first frames.
 if(box.syncing===true&&!wasSyncing&&was!=='unknown')markSyncStart();
 if(box.syncing!==true&&box.changing!==true&&boxBusy!==true)syncStartedAt=null;
 // Sync that ends without a Stop from Light Studio (the box gave up, or the Hue app) is news.
 if(wasSyncing&&box.state==='ready'&&box.syncing===false&&boxBusy===null&&box.changing==null&&!box.recovery&&!wasRecovery)boxSay('Sync stopped on the Sync Box','bad');
 if(box.recovery==='failed'&&wasRecovery!=='failed')boxSay('Sync did not come back. Stop and start it again','bad');
 if(was==='connecting'&&box.state==='ready'){closeBoxCard(false);boxSay('Connected','ok')}
 paintBox();if(was!==box.state&&panelKind)paintPanel();
}
// A short line inside the corner block; it replaces the page toast for the box.
function boxSay(text,kind='ok'){
 clearTimeout(boxSayTimer);boxSaid={text,kind};paintBox();
 boxSayTimer=setTimeout(()=>{boxSaid=null;paintBox()},BOX_SAY_MS);
}
function boxAction(action,label,icon,kind=''){return '<button type="button" class="box-action'+(kind?' '+kind:'')+'" data-box="'+action+'">'+boxSvg(icon)+'<span>'+label+'</span></button>'}
// The one action beside the status; while the box switches, the action it is
// switching away from stays visible but disabled.
function boxBar(){
 const s=box.state;
 if(s==='none')return boxAction('connect','Connect Sync Box','link','box-connect');
 if(s==='unauthorized')return boxAction('connect','Reconnect','link','box-connect');
 if(s!=='ready')return '';
 const busy=boxBusy??box.changing??null,on=busy===null?box.syncing:!busy;
 const html=on?boxAction('toggle','Stop Sync','stop','box-stop'):boxAction('toggle','Start Sync','play','box-start');
 return busy===null?html:html.replace('<button ','<button disabled ');
}
function paintBox(){
 const el=$('#sync-box');if(!el)return;paintStatus();
 const html=boxBar(),act=el.querySelector('.box-act'),said=el.querySelector('.box-said');
 el.classList.toggle('open',!!boxCard);
 if(html!==boxHtml){boxHtml=html;act.innerHTML=html}
 act.hidden=!html;
 $('#sync-status').setAttribute('aria-expanded',String(boxCard==='info'));
 act.querySelector('[data-box="connect"]')?.setAttribute('aria-expanded',String(boxCard==='connect'));
 const text=boxSaid?boxSvg(boxSaid.kind==='ok'?'check':'alert')+'<span>'+esc(boxSaid.text)+'</span>':'';
 said.hidden=!text;said.dataset.kind=boxSaid?.kind||'';if(said.innerHTML!==text)said.innerHTML=text;
 paintBoxCard();
}
function boxRing(left){const share=Math.max(0,Math.min(1,left/BOX_PAIR_S));return '<div class="box-ring"><svg viewBox="0 0 44 44" aria-hidden="true"><circle class="box-track" cx="22" cy="22" r="19"/><circle class="box-arc" cx="22" cy="22" r="19" style="stroke-dashoffset:'+(BOX_RING*(1-share)).toFixed(1)+'"/></svg><b>'+left+'</b></div>'}
function boxStep(n,state,title,body=''){return '<li class="box-step '+state+'"><i>'+(state==='done'?boxSvg('check'):n)+'</i><div><b>'+title+'</b>'+body+'</div></li>'}
function boxCardHtml(){
 if(boxCard==='info'){
  return '<div class="box-info">'+boxSvg('box')+'<div><b>'+esc(box.name||'Sync Box')+'</b><span>'+esc(box.ip||'')+'</span></div></div>'
   +'<div class="card-list card-end">'+(boxForget
    ?confirmHtml({text:'Forget this Sync Box?',cancel:{'data-box':'forget-cancel'},ok:{'data-box':'forget-yes'},okLabel:'Forget'})
    :'<button class="card-row danger" data-box="forget">'+iconSvg('remove')+'<span>Forget Sync Box</span></button>')+'</div>';
 }
 const error=boxError?'<p class="note bad" role="alert">'+boxSvg('alert')+'<span>'+esc(boxError)+'</span></p>':'';
 let first,second;
 if(boxPairing()){
  first=boxStep(1,'done','Choose your Sync Box','<span>'+esc(box.name+' · '+box.ip)+'</span>');
  second=box.state==='connecting'
   ?boxStep(2,'active','Hold the button on the box','<div class="box-hold">'+boxRing(box.remaining??BOX_PAIR_S)+'<span>Press and hold it for about 3 seconds, until its light blinks green.</span></div>')
   :boxStep(2,'active','Hold the button on the box','<p class="note bad" role="alert">'+boxSvg('alert')+'<span>'+(box.state==='failed'?'Light Studio could not save the connection.':'The button was not held in time.')+'</span></p>'+btn('Try again',{'data-box':'retry'}));
 }else{
  const rows=(boxFound||[]).map(b=>'<button class="card-row" data-box-ip="'+esc(b.ip)+'">'+boxSvg('box')+'<span>'+esc(b.name)+'</span><em>'+esc(b.ip)+'</em>'+iconSvg('next')+'</button>').join('');
  const hint=boxFound===null?'Looking on this network…':boxFound.length?'Found on this network:':'None found. Enter its IP address:';
  first=boxStep(1,'active','Choose your Sync Box','<span>'+hint+'</span>'+(rows?'<div class="card-list">'+rows+'</div>':'')
   +'<div class="box-address">'+ipField({id:'box-ip',placeholder:boxFound?.length?'Or an IP address':'IP address','aria-label':'Sync Box IP address',value:boxAddress})+btn('Connect',{'data-box':'address'},'primary')+'</div>'+error);
  second=boxStep(2,'todo','Hold the button on the box');
 }
 return '<ol class="box-steps">'+first+second+'</ol><div class="card-foot"><span class="card-gap"></span>'+btn('Cancel',{'data-box':'close'})+'</div>';
}
function paintBoxCard(){
 const card=$('#box-card');if(!card)return;
 if(boxCard==='info'&&!['ready','offline','unauthorized'].includes(box.state)){boxCard=null;boxForget=false}
 card.hidden=!boxCard;$('#sync-box').classList.toggle('open',!!boxCard);
 if(!boxCard){if(card.innerHTML)card.innerHTML='';return}
 const focused=document.activeElement?.id==='box-ip';
 const html=boxCardHtml();if(card.innerHTML!==html){card.innerHTML=html;if(focused)card.querySelector('#box-ip')?.focus()}
}
function openBoxCard(kind){
 boxCard=kind;boxForget=false;boxError='';
 if(kind==='connect'&&!boxPairing()){boxFound=null;findBoxes()}
 paintBox();
 const card=$('#box-card');card.classList.remove('opening');void card.offsetWidth;card.classList.add('opening');
 (card.querySelector('#box-ip')||card.querySelector('button'))?.focus();
}
function closeBoxCard(cancelPairing=true){
 if(!boxCard)return;
 if(cancelPairing&&boxPairing())boxPost('cancel').then(r=>applyBox(r.body));
 boxCard=null;boxForget=false;boxError='';paintBox();
}
async function findBoxes(){
 const r=await boxPost('find');if(boxCard!=='connect')return;
 boxFound=r.status===200&&Array.isArray(r.body.boxes)?r.body.boxes:[];paintBox();
}
async function pairBox(ip){
 boxError='';paintBox();
 const r=await boxPost('pair',{ip});
 if(r.status===200)return applyBox(r.body);
 boxError=r.status===400?'Enter an address on this network, like 192.168.1.20.':'No Sync Box answered at '+ip+'.';
 paintBox();
}
function submitAddress(){boxAddress=$('#box-ip').value.trim();if(boxAddress)pairBox(boxAddress)}
async function toggleSync(){
 if(box.state!=='ready'||boxBusy!==null)return;
 const active=!box.syncing;boxBusy=active;if(active)markSyncStart();paintBox();
 const r=await boxPost('sync',{active});boxBusy=null;
 applyBox(r.body);
 if(r.status!==200&&r.body?.state==='ready')boxSay('The Sync Box did not respond','bad');
}
function boxClick(e){
 const ip=e.target.closest('[data-box-ip]');if(ip){pairBox(ip.dataset.boxIp);return}
 const b=e.target.closest('[data-box]');if(!b||b.disabled)return;
 const id=b.dataset.box;
 if(id==='connect')boxCard==='connect'?closeBoxCard():openBoxCard('connect');
 else if(id==='toggle')toggleSync();
 else if(id==='close')closeBoxCard();
 else if(id==='retry')pairBox(box.ip);
 else if(id==='address')submitAddress();
 else if(id==='forget'){boxForget=true;paintBox();$('#box-card [data-box="forget-yes"]')?.focus()}
 else if(id==='forget-cancel'){boxForget=false;paintBox()}
 else if(id==='forget-yes'){closeBoxCard(false);boxPost('forget').then(r=>{applyBox(r.body);if(r.status===200)boxSay('Sync Box forgotten','ok')})}
}
$('#sync-box').addEventListener('click',boxClick);
$('#sync-box').addEventListener('input',e=>{if(e.target.id==='box-ip'){boxAddress=e.target.value;if(boxError){boxError='';$('#box-card .note.bad')?.remove()}}});
$('#sync-box').addEventListener('keydown',e=>{if(e.target.id==='box-ip'&&e.key==='Enter'){e.preventDefault();submitAddress()}});
// The connect card stays open while the owner pairs the box; Cancel or Esc closes it.
document.addEventListener('pointerdown',e=>{if(boxCard==='info'&&!e.target.closest('#sync-box'))closeBoxCard()},true);
window.addEventListener('keydown',e=>{if(e.key==='Escape'&&boxCard){e.preventDefault();e.stopImmediatePropagation();closeBoxCard();$('#sync-box .box-bar button')?.focus()}},true);
document.addEventListener('visibilitychange',()=>{++boxGeneration;clearTimeout(boxTimer);if(!document.hidden)refreshBox()});

// ----------------------------------------------------------------- rooms --
// The room name opens the room cards: each card draws its room small, the current one
// is marked, and the Sync Box's room shows the box. The page edits one room at a time;
// its layout, browser draft and undo history belong to that room.
const ROOM_REFUSED={invalid_name:'A name has 1 to 40 characters',room_limit:'A home has up to ten rooms',unknown_room:'This room no longer exists',unknown_light:'This lamp cannot change rooms',
 last_room:'The last room stays',has_sync_box:'The Sync Box is in this room',has_lights:'Move its lamps to another room first',layout_full:'That room holds up to '+MAX_ROWS+' lamps',
 sync_active:'Not while Sync is on',restarting:'The lamps are restarting; try again in a moment',unavailable:'Light Studio cannot reach the lamps now'};
const boxIcon='<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true"><rect x="3" y="7" width="14" height="6.5" rx="1.6"/><path d="M6 10.2h.01M14 10.2h2"/></svg>';
const roomIcons={pen:'<path d="m12.5 4.5 3 3-8 8H4.5v-3z"/>',trash:'<path d="M4.5 6h11M8 6V4.5h4V6m-6 0 .7 9.5h6.6L14 6"/>',plus:'<path d="M10 4v12M4 10h12"/>'};
const roomSvg=id=>'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+roomIcons[id]+'</svg>';
function layoutUrl(){return '/api/layout'+(currentRoom?'?room='+encodeURIComponent(currentRoom):'')}
const roomName=id=>roomsInfo?.rooms.find(r=>r.id===id)?.name||'Room';
// A light's room: its recorded room, otherwise the first room (as the core decides).
function roomOf(id){return roomsInfo?.assigned?.[id]||roomsInfo?.rooms[0]?.id}
// A light that another room holds: its row is there, or the core put it there.
function elsewhere(id){
 if(!roomsInfo)return false;const placed=roomsInfo.placed?.[id],assigned=roomsInfo.assigned?.[id];
 return !!(placed&&placed!==currentRoom||assigned&&assigned!==currentRoom);
}
// Sync settings belong to the Sync Box's room; until the rooms are known the page shows them.
function syncHere(){return !roomsInfo||roomsInfo.sync===currentRoom}
function validRooms(b){return !!b&&Array.isArray(b.rooms)&&b.rooms.length>0&&b.rooms.every(r=>r&&ROOM_ID.test(r.id)&&typeof r.name==='string')}
function takeRooms(b){roomsInfo={rooms:b.rooms,sync:typeof b.sync==='string'?b.sync:null,assigned:b.assigned||{},placed:b.placed||{}}}
// connectLayout handles a missing room itself; any other refresh leaves a room deleted
// elsewhere for the first room (its unsent draft stays under the deleted room's key).
async function refreshRooms(follow=true){
 try{
  const r=await fetch('/api/rooms',{cache:'no-store'});if(!r.ok)return false;
  const b=await r.json();if(!validRooms(b))return false;
  takeRooms(b);
  if(!currentRoom||roomsInfo.rooms.some(r=>r.id===currentRoom))applyRoomChrome();
  else if(follow){closeRooms();closePanel();closeObjectMenu();enterRoom(roomsInfo.rooms[0].id);tell('That room was deleted');connectLayout()}
  return true;
 }catch{return false}
}
function applyRoomChrome(){
 const here=syncHere();$('#app').classList.toggle('no-sync',!here);
 if(!here&&mode==='sync')applyMode('placement',false);
 else if(here&&mode!=='sync'&&store.get(VIEW_KEY)==='sync')applyMode('sync',false);
 paintTitle();if(roomsOpen)paintRooms();
}
// Loads a room's browser draft into the page; the server's layout follows from connectLayout.
function enterRoom(id){
 roomGeneration++;currentRoom=id;store.set(ROOM_KEY,id);KEY=draftKey(id);
 clearTimeout(retryTimer);clearTimeout(saveTimer);serverReady=false;revision=null;pendingLayout=null;editGeneration=0;offlineNotice=false;
 past=[];future=[];selected=null;
 const m=fromLayout(readJson(KEY));({room,environment,lamps}=m||{...defaults(),room:roomName(id)});
 serverBaseline=readJson(KEY+'-base')||pack();lastPersisted=JSON.stringify(pack());
 for(const l of lamps)if(linkable(l.id)&&liveSnapshot?.connected===true)l.unbound=!enrolled.has(l.id);
 applyRoomChrome();render();setDraftState('pending');
}
// Edits not yet on the server go first; what cannot be sent stays in the room's draft.
async function settleSave(){clearTimeout(saveTimer);if(serverReady&&pendingLayout&&!saving)flushLayout();await flushDone}
async function switchRoom(id){
 if(id===currentRoom){closeRooms();return}
 closeRooms();cancelPlacement();closePanel();if(cal)closeCalibrate(false);closeObjectMenu();
 await settleSave();
 enterRoom(id);connectLayout();
}
// Room changes go through the server; a refusal is said where it was asked.
async function roomChange(action,body,where=null){
 let status=0,answer={};
 try{const r=await fetch('/api/rooms/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});status=r.status;answer=await r.json().catch(()=>({}))}catch{}
 if(status===200&&validRooms(answer)){takeRooms(answer);applyRoomChrome();return true}
 const text=status===409||status===503?ROOM_REFUSED[answer.reason]||'Not changed now':status===400?ROOM_REFUSED.invalid_name:'Light Studio server is unavailable';
 if(roomsOpen){roomNote={room:where,text};paintRooms()}else tell(text);
 return false;
}
async function addRoom(name){
 if(!await roomChange('create',{name:String(name).trim()},'new'))return;
 roomAdding=false;switchRoom(roomsInfo.rooms[roomsInfo.rooms.length-1].id);
}
async function renameRoom(id,name){
 const value=String(name).trim();roomEditing=null;
 if(!value||value===roomName(id)){paintRooms();return}
 if(id===currentRoom)await settleSave();
 if(await roomChange('rename',{room:id,name:value},id)&&id===currentRoom)connectLayout();
 paintRooms();
}
async function deleteRoom(id){
 roomConfirm=null;
 if(!await roomChange('delete',{room:id},id))return;
 for(const suffix of ['','-base','-pending'])store.remove(draftKey(id)+suffix);
 delete roomLayouts[id];
 if(id===currentRoom){enterRoom(roomsInfo.rooms[0].id);connectLayout()}
 paintRooms();
}
async function reorderRooms(from,to){
 const ids=roomsInfo.rooms.map(r=>r.id),at=ids.indexOf(to);if(from===to||at<0)return;
 ids.splice(ids.indexOf(from),1);ids.splice(at,0,from);
 await roomChange('order',{rooms:ids});paintRooms();
}
async function openRooms(){
 closePanel();closeObjectMenu();cancelPlacement();
 roomsOpen=true;roomNote=null;roomEditing=null;roomAdding=false;roomConfirm=null;paintRooms();
 const known=await refreshRooms();if(!roomsOpen)return;
 if(!known||!roomsInfo){roomNote={room:'all',text:'Light Studio server is unavailable'};paintRooms();return}
 await Promise.all(roomsInfo.rooms.filter(r=>r.id!==currentRoom).map(async r=>{
  try{const a=await fetch('/api/layout?room='+encodeURIComponent(r.id),{cache:'no-store'});if(a.ok)roomLayouts[r.id]=(await a.json()).layout}catch{}
 }));
 if(roomsOpen)paintRooms();
}
function closeRooms(){
 if(!roomsOpen)return;roomsOpen=false;roomEditing=null;roomAdding=false;roomConfirm=null;roomNote=null;
 $('#room-cards').hidden=true;$('#room-switch').setAttribute('aria-expanded','false');
}
// The room drawn small: floor, desk, screen and a dot per lamp, in the room's own centimetres.
function roomThumb(layout){
 if(!layout?.desk||!layout.screen)return '<svg class="rc-thumb" viewBox="0 0 200 110" aria-hidden="true"></svg>';
 const X=x=>20+(x+40)*160/320,Y=h=>100-h*80/220,len=v=>v*80/220;
 const dw=layout.desk.width,dh=layout.desk.height,sw=layout.screen.inches*2.54*16/Math.hypot(16,9),sh=sw*9/16,sx=layout.screen.positionCm.x,sy=layout.screen.positionCm.height;
 let out='<line class="rc-floor" x1="8" y1="100" x2="192" y2="100"/>'
  +'<rect class="rc-desk" x="'+X(0)+'" y="'+Y(dh)+'" width="'+(X(dw)-X(0))+'" height="3"/>'
  +'<path class="rc-leg" d="M'+X(6)+' '+Y(dh)+'V100M'+X(dw-6)+' '+Y(dh)+'V100"/>'
  +'<rect class="rc-screen" x="'+X(sx-sw/2)+'" y="'+Y(sy+sh/2)+'" width="'+(X(sx+sw/2)-X(sx-sw/2))+'" height="'+len(sh)+'" rx="1"/>';
 for(const d of layout.devices||[])if(d.enabled!==false&&d.positionCm)out+='<circle class="rc-lamp" cx="'+X(d.positionCm.x)+'" cy="'+Y(d.positionCm.height)+'" r="4"/>';
 return '<svg class="rc-thumb" viewBox="0 0 200 110" aria-hidden="true">'+out+'</svg>';
}
function roomCount(id){
 const layout=id===currentRoom?pack():roomLayouts[id];
 const n=layout?(layout.devices||[]).filter(d=>d.enabled!==false).length:null;
 return n===null?'':n?n+(n===1?' lamp':' lamps'):'No lamps';
}
function roomNoteHtml(where){return roomNote&&roomNote.room===where?'<p class="note bad rc-note" role="alert">'+esc(roomNote.text)+'</p>':''}
function roomCard(r){
 const here=r.id===currentRoom,box=roomsInfo?.sync===r.id;
 if(roomConfirm===r.id)return '<div class="rc-card confirm">'+confirmHtml({text:'Delete '+esc(r.name)+'?',cancel:{'data-room-cancel':true},ok:{'data-room-delete-ok':r.id},okLabel:'Delete',cls:'stacked'})+roomNoteHtml(r.id)+'</div>';
 const name=roomEditing===r.id?field({'data-room-name':r.id,maxlength:40,value:r.name,'aria-label':'Room name'},'sm'):'<span class="rc-name">'+esc(r.name)+'</span>';
 return '<div class="rc-card'+(here?' current':'')+'" draggable="'+(roomEditing?'false':'true')+'" data-room-card="'+r.id+'">'
  +'<button class="rc-pick" data-room-go="'+r.id+'" aria-label="'+esc(r.name)+(here?' (this room)':'')+'"'+(here?' aria-current="true"':'')+'>'+roomThumb(here?pack():roomLayouts[r.id])+'</button>'
  +'<div class="rc-foot">'+(box?'<span class="rc-box" data-tooltip="Sync Box">'+boxIcon+'</span>':'')+'<div class="rc-text">'+name+'<span class="rc-sub">'+roomCount(r.id)+'</span></div></div>'
  +'<span class="rc-acts">'+iconBtn(roomSvg('pen'),'Rename '+r.name,{'data-room-rename':r.id,'data-tooltip':'Rename'})+iconBtn(roomSvg('trash'),'Delete '+r.name,{'data-room-delete':r.id,'data-tooltip':'Delete'},'danger')+'</span>'
  +roomNoteHtml(r.id)+'</div>';
}
function paintRooms(){
 const node=$('#room-cards');if(!roomsOpen){node.hidden=true;return}
 $('#room-switch').setAttribute('aria-expanded','true');
 const list=roomsInfo?roomsInfo.rooms:[{id:currentRoom||FIRST_ROOM,name:room}];
 const add=roomAdding?'<div class="rc-card rc-add editing">'+field({'data-room-new':true,maxlength:40,placeholder:'Room name','aria-label':'New room name'},'sm')+btn('Add',{'data-room-create':true},'sm primary')+roomNoteHtml('new')+'</div>'
  :list.length<10?'<button class="rc-card rc-add" data-room-add>'+roomSvg('plus')+'<span>Add room</span></button>':'';
 node.innerHTML='<div class="rc-grid">'+list.map(roomCard).join('')+(roomsInfo?add:'')+'</div>'+roomNoteHtml('all')+(roomsInfo&&list.length>1?'<p class="rc-hint">Drag a card to change the order</p>':'');
 node.hidden=false;
 const a=$('#app').getBoundingClientRect(),r=$('#room-switch').getBoundingClientRect();
 node.style.left=Math.max(12,r.left-a.left)+'px';node.style.top=(r.bottom-a.top+8)+'px';
 const input=node.querySelector('[data-room-name],[data-room-new]');if(input&&document.activeElement!==input){input.focus();input.select?.()}
}
$('#room-cards').addEventListener('click',e=>{
 e.stopPropagation();
 const t=e.target.closest('[data-room-go],[data-room-add],[data-room-create],[data-room-rename],[data-room-delete],[data-room-cancel],[data-room-delete-ok]');if(!t)return;
 const d=t.dataset;roomNote=null;
 if('roomGo' in d){if(!roomEditing)switchRoom(d.roomGo);return}
 if('roomAdd' in d){roomAdding=true;roomEditing=null;roomConfirm=null;paintRooms();return}
 if('roomCreate' in d){addRoom($('#room-cards [data-room-new]').value);return}
 if('roomRename' in d){roomEditing=d.roomRename;roomAdding=false;roomConfirm=null;paintRooms();return}
 if('roomDelete' in d){roomConfirm=d.roomDelete;roomEditing=null;paintRooms();$('#room-cards [data-room-cancel]')?.focus();return}
 if('roomCancel' in d){roomConfirm=null;paintRooms();return}
 if('roomDeleteOk' in d)deleteRoom(d.roomDeleteOk);
});
$('#room-cards').addEventListener('keydown',e=>{
 const t=e.target;
 if(t.matches('[data-room-name]')){if(e.key==='Enter'){e.preventDefault();renameRoom(t.dataset.roomName,t.value)}else if(e.key==='Escape'){e.preventDefault();roomEditing=null;paintRooms()}e.stopPropagation();return}
 if(t.matches('[data-room-new]')){if(e.key==='Enter'){e.preventDefault();addRoom(t.value)}else if(e.key==='Escape'){e.preventDefault();roomAdding=false;roomNote=null;paintRooms()}e.stopPropagation();return}
 if(e.key==='Escape'){e.stopPropagation();closeRooms();$('#room-switch').focus()}
});
$('#room-cards').addEventListener('focusout',e=>{const t=e.target;if(t.matches?.('[data-room-name]'))setTimeout(()=>{if(roomEditing===t.dataset.roomName&&document.activeElement!==t)renameRoom(t.dataset.roomName,t.value)},0)});
$('#room-cards').addEventListener('dragstart',e=>{const c=e.target.closest?.('[data-room-card]');if(!c)return;roomDragged=c.dataset.roomCard;if(e.dataTransfer)e.dataTransfer.effectAllowed='move';c.classList.add('dragging')});
$('#room-cards').addEventListener('dragover',e=>{const c=e.target.closest?.('[data-room-card]');if(!c||!roomDragged)return;e.preventDefault();$('#room-cards').querySelectorAll('.drop').forEach(n=>n.classList.remove('drop'));if(c.dataset.roomCard!==roomDragged)c.classList.add('drop')});
$('#room-cards').addEventListener('drop',e=>{const c=e.target.closest?.('[data-room-card]');if(!c||!roomDragged)return;e.preventDefault();const from=roomDragged;roomDragged=null;reorderRooms(from,c.dataset.roomCard)});
$('#room-cards').addEventListener('dragend',()=>{roomDragged=null;$('#room-cards').querySelectorAll('.drop,.dragging').forEach(n=>n.classList.remove('drop','dragging'))});
document.addEventListener('pointerdown',e=>{if(roomsOpen&&!e.target.closest('#room-cards,#room-switch'))closeRooms()},true);
// Inside the cards Esc first leaves a name field; the cards' own handler decides.
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&roomsOpen&&!e.target.closest?.('#room-cards')){e.preventDefault();e.stopImmediatePropagation();closeRooms()}},true);

// A connected or found lamp moves to another room with its corrections; layout-only
// drawings stay in their room.
function movable(l){return !!roomsInfo&&roomsInfo.rooms.length>1&&linkable(l.id)&&l.enabled!==false}
function movePageHtml(l){
 const rows=roomsInfo.rooms.filter(r=>r.id!==currentRoom).map(r=>'<button class="card-row" data-menu="move-to-'+r.id+'">'+iconSvg('room')+'<span>'+esc(r.name)+'</span>'+(roomsInfo.sync===r.id?'<em>Sync Box</em>':'')+'</button>').join('');
 return '<div class="card-back">'+iconBtn(iconSvg('back'),'Back',{'data-menu':'move-back'})+'<div><b>Move to room</b><div class="card-sub">'+esc(l.name)+'</div></div></div>'
  +'<div class="card-list">'+rows+'</div><p class="note bad card-refusal" data-move-note role="alert" hidden></p>';
}
function openMovePage(l){
 const menu=$('#object-menu'),pages=menu.querySelector('.card-pages');if(!pages||!movable(l))return;
 const [main,page]=pages.children;page.innerHTML=movePageHtml(l);moveOpen=true;
 menu.style.height=main.offsetHeight+'px';
 requestAnimationFrame(()=>{pages.classList.add('at-cal');menu.style.height=page.offsetHeight+'px';placeOpenMenu();page.querySelector('[data-menu="move-back"]')?.focus?.({preventScroll:true})});
}
function closeMovePage(){
 const menu=$('#object-menu'),pages=menu.querySelector('.card-pages');moveOpen=false;if(!pages)return;
 const [main,page]=pages.children;pages.classList.remove('at-cal');menu.style.height=main.offsetHeight+'px';placeOpenMenu();
 setTimeout(()=>{if(!moveOpen&&!cal){page.innerHTML='';menu.style.height=''}},300);
}
// The row moves on the server with its corrections, so the page's edits are saved first
// and the room's layout is read again afterwards; undo cannot bring the row back here.
async function moveLamp(l,target){
 if(!roomsInfo?.rooms.some(r=>r.id===target))return;
 await settleSave();
 if(!serverReady){tell('Light Studio server is unavailable');return}
 const note=$('#object-menu [data-move-note]');
 let status=0,answer={};
 try{const r=await fetch('/api/rooms/move',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({light:l.id,room:target})});status=r.status;answer=await r.json().catch(()=>({}))}catch{}
 if(!(status===200&&validRooms(answer))){
  const text=ROOM_REFUSED[answer.reason]||'Light Studio server is unavailable';
  if(note){note.textContent=text;note.hidden=false;const menu=$('#object-menu'),page=note.closest('.card-page');if(page)menu.style.height=page.offsetHeight+'px';placeOpenMenu()}else tell(text);return;
 }
 takeRooms(answer);
 moveOpen=false;closeObjectMenu();selected=null;past=[];future=[];
 lamps=lamps.filter(x=>x.id!==l.id);lastPersisted=JSON.stringify(pack());render();
 tell(l.name+' moved to '+roomName(target));
 connectLayout();
}

// ----------------------------------------------------------------- start --
$('#room-switch').addEventListener('click',e=>{e.stopPropagation();roomsOpen?closeRooms():openRooms()});
applyDarkScene();applyMode(mode);render();annotateTooltips();
connectLayout();
if(!document.hidden){watchLive();refreshLive();refreshBox();}
