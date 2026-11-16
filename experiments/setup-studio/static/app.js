const $ = selector => document.querySelector(selector);
const clone = value => structuredClone(value);
const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clamp = (v,min,max) => Math.min(max,Math.max(min,Number(v)||0));
const KEY='light-studio-grid-v3';
const VIEW_SYNC_KEY='light-studio-view-sync-bounds-v1';
let showSyncBounds=false;
try{showSyncBounds=localStorage.getItem(VIEW_SYNC_KEY)==='true';}catch{}
const GRID=25, STEP_CM=5, COLS=40;
const STAGE_HEIGHT=900, FLOOR_Y=850, UNITS_PER_CM=GRID/STEP_CM;
const deskDefaults={width:160,height:85};
const GOVEE_HEIGHT_CM=136, PLAY_LENGTH_CM=25;
const deskTop=desk=>FLOOR_Y-desk.height*UNITS_PER_CM;
// 32-inch diagonal, assuming a 16:9 display; dimensions exclude its bezel.
const monitorSize=(inches=layout.monitorInches)=>{const width=inches*2.54*16/Math.hypot(16,9)*UNITS_PER_CM;return {width,height:width*9/16};};
const stageY=d=>d.pos.y/100*STAGE_HEIGHT;
const heightCm=d=>Math.round((FLOOR_Y-stageY(d))/GRID)*STEP_CM;
const positionFromCm=position=>({x:position.x/(COLS*STEP_CM)*100,y:(FLOOR_Y-position.height*UNITS_PER_CM)/STAGE_HEIGHT*100});
const seed=[
 {id:'govee-left',name:'Govee Left',short:'Govee L',model:'H6076',shape:'floor',zones:7,scale:65,rate:0,pos:{x:10,y:61}},
 {id:'govee-right',name:'Govee Right',short:'Govee R',model:'H6076',shape:'floor',zones:7,scale:65,rate:10,pos:{x:90,y:61}},
 {id:'hue-monitor',name:'Hue Monitor Strip',short:'Hue Strip',model:'LCX006',shape:'strip',zones:9,scale:100,rate:0,pos:{x:50,y:33.333333}},
 {id:'play-left',name:'Hue Play Left',short:'Play L',model:'Hue Play',shape:'bar',zones:1,scale:100,rate:0,orientation:'horizontal',pos:{x:35,y:72.222222}},
 {id:'play-center',name:'Hue Play Center',short:'Play C',model:'Hue Play',shape:'bar',zones:1,scale:50,rate:0,orientation:'horizontal',pos:{x:50,y:72.222222}},
 {id:'play-right',name:'Hue Play Right',short:'Play R',model:'Hue Play',shape:'bar',zones:1,scale:100,rate:0,orientation:'horizontal',pos:{x:65,y:72.222222}}
];
const typeNames={floor:'Торшер RGBIC',strip:'Стрічка',bar:'Світлова панель'};
const realDevice=id=>/^device-[a-f0-9]{32}$/.test(id||'');
const enrolled=id=>seed.some(d=>d.id===id);
const enabled=d=>d.enabled!==false;
const removable=id=>enrolled(id)||id?.startsWith('virtual-')||realDevice(id);
function defaults(){const data={version:3,room:'Mykola Office',desk:clone(deskDefaults),monitorInches:32,devices:clone(seed)};data.devices.forEach(d=>autoSnap(d,data.desk,data.monitorInches));return data;}
function load(savedOverride){
 try{
  const saved=savedOverride||JSON.parse(localStorage.getItem(KEY)||localStorage.getItem('light-studio-simple-v2')||localStorage.getItem('light-studio-layout-v1'));
  if(![1,2,3].includes(saved?.version)||!Array.isArray(saved.devices))return defaults();
  const data=defaults();data.desk={width:clamp(saved.desk?.width??160,80,240),height:clamp(saved.desk?.height??85,50,120)};data.monitorInches=clamp(saved.monitorInches??32,19,55);data.room=String(saved.room||data.room).slice(0,40);
  data.devices=data.devices.map(d=>{const s=saved.devices.find(x=>x.id===d.id);if(s?.positionCm)s.pos=positionFromCm(s.positionCm);return s?{...d,enabled:s.enabled!==false,scale:clamp(s.scale??d.scale,0,100),orientation:s.orientation==='vertical'?'vertical':'horizontal',pos:{x:clamp(s.pos?.x??d.pos.x,-100,200),y:clamp(s.pos?.y??d.pos.y,d.id==='hue-monitor'||d.shape==='floor'?-100:0,100)}}:d;});
  for(const s of saved.devices.filter(x=>/^virtual-[a-z0-9]+$/.test(x.id||'')||realDevice(x.id)).slice(0,6)){
   const shape=Object.hasOwn(typeNames,s.shape)?s.shape:'bar';if(s.positionCm)s.pos=positionFromCm(s.positionCm);
   const real=realDevice(s.id),name=String(s.name||'Нова лампа').slice(0,real?80:35);
   data.devices.push({id:s.id,name,short:real?name:'Нова лампа',model:real?String(s.model||'').slice(0,64):'Макет',shape,enabled:s.enabled!==false,zones:real?clamp(s.zones,1,32):shape==='floor'?7:shape==='strip'?9:1,scale:clamp(s.scale??80,0,100),rate:0,orientation:s.orientation==='vertical'?'vertical':'horizontal',pos:{x:clamp(s.pos?.x??50,-100,200),y:clamp(s.pos?.y??70,shape==='floor'?-100:0,100)}});
  }
  data.devices.forEach(d=>autoSnap(d,data.desk,data.monitorInches));return data;
 }catch{return defaults();}
}
let layout=load(),selected=null,dragging=null,history=[],toastTimer;
const stage=$('#stage');
const current=()=>layout.devices.find(d=>d.id===(selected==='monitor'?'hue-monitor':selected)&&(selected==='monitor'||enabled(d)));
const screen=()=>layout.devices.find(d=>d.id==='hue-monitor')||seed[2];
function remember(){history.push(clone(layout));if(history.length>25)history.shift();}
let editGeneration=0;
function dirty(){editGeneration++;save();}
function notify(text){$('#toast').textContent=text;$('#toast').classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.remove('visible'),3000);}
function point(event){return new DOMPoint(event.clientX,event.clientY).matrixTransform(stage.getScreenCTM().inverse());}
const PREVIEW_POLL_MS=200,PREVIEW_DEADLINE_MS=1000,PREVIEW_FRESH_MS=1500;
const syncLabels={active:'активний',waiting:'очікування',off:'неактивний',foreign:'зовнішній',unknown:'невідомо',paused:'пауза'};
const previewFixtureIds=new Set(seed.map(d=>d.id));
let previewSnapshot=null,previewReceivedAt=-Infinity,previewRequest=null,previewGeneration=0,previewTimer,previewWatchdog;
function previewFresh(){return !document.hidden&&performance.now()-previewReceivedAt<PREVIEW_FRESH_MS;}
function zonePaint(id,index){
 const neutral={color:'#52635a',opacity:'.35',state:'neutral'};
 if(!previewFresh()||!previewSnapshot?.connected||!['scene','sync','waiting','unknown'].includes(previewSnapshot.mode)||!previewFixtureIds.has(id))return neutral;
 const lamp=previewSnapshot.lamps?.[id];
 if(!lamp||lamp.online!==true)return neutral;
 if(lamp.on===false)return {color:neutral.color,opacity:'0',state:'off'};
 if(lamp.on!==true||lamp.stale!==false||!['scene-output','sync-output'].includes(lamp.source)||!Array.isArray(lamp.colors))return neutral;
 const rgb=lamp.colors[lamp.colors.length===1?0:index];
 if(!Array.isArray(rgb)||rgb.length!==3||!rgb.every(value=>Number.isInteger(value)&&value>=0&&value<=255))return neutral;
 return {color:`rgb(${rgb.join(',')})`,opacity:'1',state:'active'};
}
function previewOpacity(paint,glow){
 return glow&&(paint.state!=='active'||paint.color==='rgb(0,0,0)')?'0':paint.opacity;
}
function zoneAttributes(id,index,attribute,glow=false){
 const paint=zonePaint(id,index);
 const opacity=previewOpacity(paint,glow);
 return `data-preview-lamp="${id}" ${glow?'data-preview-glow="" ':''}data-zone="${index}" data-color-attribute="${attribute}" data-output="${paint.state}" ${attribute}="${paint.color}" opacity="${opacity}"`;
}
function paintPreview(){
 // Telemetry changes only existing color attributes, never layout or focused controls.
 for(const node of stage.querySelectorAll('[data-preview-lamp]')){
  const paint=zonePaint(node.dataset.previewLamp,Number(node.dataset.zone));
  const opacity=previewOpacity(paint,node.hasAttribute('data-preview-glow'));
  for(const [key,value]of [[node.dataset.colorAttribute,paint.color],['opacity',opacity],['data-output',paint.state]])if(node.getAttribute(key)!==value)node.setAttribute(key,value);
 }
 const state=previewFresh()&&previewSnapshot&&(previewSnapshot.connected||previewSnapshot.sync?.state==='paused')?previewSnapshot.sync?.state:'unknown';
 const safeState=Object.hasOwn(syncLabels,state)?state:'unknown',badge=$('#sync-status'),label=`Sync: ${syncLabels[safeState]}`;
 if(badge.textContent!==label)badge.textContent=label;
 if(badge.dataset.state!==safeState)badge.setAttribute('data-state',safeState);
}
function expirePreview(){previewSnapshot=null;previewReceivedAt=-Infinity;paintPreview();}
function watchPreview(){
 clearTimeout(previewWatchdog);
 if(document.hidden)return;
 paintPreview();
 const remaining=PREVIEW_FRESH_MS-(performance.now()-previewReceivedAt);
 if(remaining>0)previewWatchdog=setTimeout(watchPreview,remaining);
}
async function refreshPreview(){
 if(document.hidden||previewRequest)return;
 const generation=++previewGeneration,controller=new AbortController();
 let deadline,rejectCancelled;
 const cancelled=new Promise((_,reject)=>{rejectCancelled=reject;});
 const request={controller,cancel:()=>{controller.abort();rejectCancelled(new Error('preview cancelled'));}};
 previewRequest=request;
 deadline=setTimeout(request.cancel,PREVIEW_DEADLINE_MS);
 try{
  const snapshot=await Promise.race([fetch('/api/pilot',{signal:controller.signal,cache:'no-store'}).then(async response=>{if(!response.ok)throw new Error('preview unavailable');return response.json();}),cancelled]);
  if(generation!==previewGeneration||document.hidden)return;
  previewSnapshot=snapshot;previewReceivedAt=performance.now();watchPreview();
 }catch{if(generation===previewGeneration)expirePreview();}
 finally{
  clearTimeout(deadline);
  if(previewRequest===request)previewRequest=null;
  if(generation===previewGeneration&&!document.hidden)previewTimer=setTimeout(refreshPreview,PREVIEW_POLL_MS);
 }
}
document.addEventListener('visibilitychange',()=>{
 ++previewGeneration;clearTimeout(previewTimer);clearTimeout(previewWatchdog);
 previewRequest?.cancel();previewRequest=null;expirePreview();
 if(!document.hidden){watchPreview();refreshPreview();}
});
function room(){
 const monitor=screen(),cx=monitor.pos.x*10,cy=stageY(monitor),{width:w,height:h}=monitorSize();
 const DESK_WIDTH=layout.desk.width*UNITS_PER_CM,DESK_Y=deskTop(layout.desk);
 const left=500-DESK_WIDTH/2,right=500+DESK_WIDTH/2;
 return `<path class="detail-line" d="M-1000 ${FLOOR_Y}H2000"/>
 <g class="desk-target" data-desk role="button" tabindex="0" aria-label="Розміри столу: ${layout.desk.width} на ${layout.desk.height} см">
 <rect class="desk-hit" x="${left-12}" y="${DESK_Y-12}" width="${DESK_WIDTH+24}" height="85"/>
 <rect class="desk-shape" x="${left+7.5}" y="${DESK_Y+25}" width="25" height="${FLOOR_Y-DESK_Y-25}" rx="3"/>
 <rect class="desk-shape" x="${right-32.5}" y="${DESK_Y+25}" width="25" height="${FLOOR_Y-DESK_Y-25}" rx="3"/>
 <rect class="desk-shape" x="${left}" y="${DESK_Y}" width="${DESK_WIDTH}" height="25" rx="4"/>
 <text class="desk-measurement" x="500" y="${DESK_Y+12.5}">${layout.desk.width} см</text>
 <text class="desk-measurement" x="${right-20}" y="${(DESK_Y+25+FLOOR_Y)/2}" transform="rotate(-90 ${right-20} ${(DESK_Y+25+FLOOR_Y)/2})">${layout.desk.height} см</text></g>
 <g data-monitor class="monitor-target ${selected==='monitor'?'selected':''}" role="button" tabindex="0" aria-label="Монітор: ${layout.monitorInches} дюйми, центр ${heightCm(monitor)} см від підлоги"><rect class="monitor" x="${cx-w/2}" y="${cy-h/2}" width="${w}" height="${h}" rx="7"/>
 <rect x="${cx-w/2+7}" y="${cy-h/2+7}" width="${w-14}" height="${h-14}" rx="4" fill="#1c282c"/>
 <text class="screen-copy" x="${cx}" y="${cy+4}">${layout.monitorInches}″ · 16:9 · ✎</text></g>
 <path class="desk" d="M${cx} ${cy+h/2+2}V${DESK_Y-3}M${cx-35} ${DESK_Y-3}h70"/>`;
}
function lamp(d,glow=false){
 const x=d.pos.x*10,y=stageY(d);
 const attached=d.id==='hue-monitor';
 const vertical=d.shape==='floor'||(d.shape==='bar'&&d.orientation==='vertical');
 let body='',light='',dividers='',box,nameY;
 if(d.shape==='floor'){
  const base=300,top=base-GOVEE_HEIGHT_CM*UNITS_PER_CM,lightBottom=270,lightLength=lightBottom-top-10;
  body=`<path d="M0 ${top}V279" stroke="#51644f" stroke-width="20" stroke-linecap="butt"/><path d="M0 279v19" stroke="#71826a" stroke-width="2"/><ellipse class="fixture" cx="0" cy="300" rx="21" ry="5"/>`;
  light=Array.from({length:d.zones},(_,i)=>{
   const bottom=lightBottom-i*lightLength/d.zones,length=lightLength/d.zones;
   const gap=glow||d.zones===1?0:5;
   return `<path class="light" d="M0 ${bottom-gap}v${-length+(glow?-1:gap*2)}" ${zoneAttributes(d.id,i,'stroke',glow)} stroke-width="14" stroke-linecap="round"/>`;
  }).join('');
  if(!glow)dividers=Array.from({length:d.zones-1},(_,i)=>`<path class="zone-divider" d="M-6 ${lightBottom-(i+1)*lightLength/d.zones}h12"/>`).join('');
  box=[-37,top-15,74,base-top+35];nameY=345;
 }else if(attached){
  const size=monitorSize(),w=size.width/2+14,h=size.height/2+14;
  const points=[[-w,h],[-w,0],[-w,-h],[-w/2,-h],[0,-h],[w/2,-h],[w,-h],[w,0],[w,h]];
  const path=`M${-w} ${h}V${-h}H${w}V${h}`;
  body=`<path class="strip-back" d="${path}"/>`;
  light=points.map(([px,py],i)=>{
   const previous=points[Math.max(0,i-1)],next=points[Math.min(points.length-1,i+1)];
   // Each anchor owns the U-path up to the midpoint toward its neighbours.
   return `<path class="light-path" d="M${(previous[0]+px)/2} ${(previous[1]+py)/2}L${px} ${py}L${(next[0]+px)/2} ${(next[1]+py)/2}" ${zoneAttributes(d.id,i,'stroke',glow)}/>`;
  }).join('');
  if(!glow)dividers=points.slice(1).map(([nx,ny],i)=>{
   const [px,py]=points[i],mx=(px+nx)/2,my=(py+ny)/2;
   return `<path class="zone-divider" d="${px===nx?`M${mx-6} ${my}h12`:`M${mx} ${my-6}v12`}"/>`;
  }).join('');
  box=[-w-15,-h-15,w*2+30,h*2+30];nameY=-h-29;
 }else{
  const length=d.shape==='strip'?220:PLAY_LENGTH_CM*UNITS_PER_CM,inset=10;
  body=`<rect class="fixture" x="${vertical?-14:-length/2}" y="${vertical?-length/2:-14}" width="${vertical?28:length}" height="${vertical?length:28}" rx="10"/>`;
  light=Array.from({length:d.zones},(_,i)=>{
   const span=(length-inset*2)/d.zones,start=-length/2+inset+i*span;
   const gap=glow||d.zones===1?0:Math.min(5,span/4);
   const path=vertical?`M0 ${start+gap}v${span-gap*2}`:`M${start+gap} 0h${span-gap*2}`;
   return `<path class="light" d="${path}" ${zoneAttributes(d.id,i,'stroke',glow)} stroke-width="18" stroke-linecap="round"/>`;
  }).join('');
  if(!glow)dividers=Array.from({length:d.zones-1},(_,i)=>{
   const boundary=-length/2+inset+(i+1)*(length-inset*2)/d.zones;
   return `<path class="zone-divider" d="${vertical?`M-6 ${boundary}h12`:`M${boundary} -6v12`}"/>`;
  }).join('');
  box=vertical?[-27,-length/2-15,54,length+30]:[-length/2-14,-26,length+28,52];nameY=vertical?length/2+35:39;
 }
 if(glow)return `<g class="wall-glow" transform="translate(${x},${y})">${light}</g>`;
 return `<g class="lamp ${selected===d.id?'selected':''}" data-lamp="${d.id}" role="button" tabindex="0" aria-label="${esc(d.name)}" aria-pressed="${selected===d.id}" transform="translate(${x},${y})"><rect class="hit ${attached?'strip-hit':''}" x="${box[0]-18}" y="${box[1]-12}" width="${box[2]+36}" height="${box[3]+24}" rx="12"/><rect class="halo" x="${box[0]}" y="${box[1]}" width="${box[2]}" height="${box[3]}" rx="12"/>${body}${light}${dividers}<text class="lamp-name long-name" x="0" y="${nameY}">${esc(d.name)}</text><text class="lamp-name short-name" x="0" y="${nameY}">${esc(d.short)}</text></g>`;
}
function syncFadeY(x){
 const outside=Math.max(0,Math.abs(x-500)-layout.desk.width*UNITS_PER_CM/2);
 return deskTop(layout.desk)+15*UNITS_PER_CM+outside;
}
function syncBounds(left,right){
 if(!showSyncBounds)return '';
 const tableY=deskTop(layout.desk),half=layout.desk.width*UNITS_PER_CM/2;
 const edgeLeft=500-half,edgeRight=500+half,near=syncFadeY(500);
 const lower=`M${left} ${syncFadeY(left)}L${edgeLeft} ${near}H${edgeRight}L${right} ${syncFadeY(right)}`;
 return `<defs><clipPath id="sync-bounds-clip"><rect x="${left}" y="${tableY-4}" width="${right-left}" height="${FLOOR_Y-tableY+4}"/></clipPath></defs>
 <g class="sync-functional" clip-path="url(#sync-bounds-clip)" aria-hidden="true" pointer-events="none">
 <path class="sync-functional-band" d="${lower}V${tableY}H${left}Z"/>
 <path class="sync-functional-top" d="M${left} ${tableY}H${right}"/>
 <path class="sync-functional-limit" d="${lower}"/>
 <text class="sync-functional-label" x="500" y="${near+18}">Згасання Sync · 15 см</text></g>`;
}
function renderStage(){
 const deskHalf=layout.desk.width*UNITS_PER_CM/2;
 const fixtures=layout.devices.filter(enabled),bounds=layout.devices.filter(d=>enabled(d)||d.id==='hue-monitor');
 const top=Math.min(0,stageY(screen())-monitorSize().height/2-70,...fixtures.filter(d=>d.shape==='floor').map(d=>stageY(d)-380-70));
 const left=Math.min(-150,500-deskHalf-100,...bounds.map(d=>d.pos.x*10-(d.id==='hue-monitor'?monitorSize().width/2+70:100)));
 const right=Math.max(1150,500+deskHalf+100,...bounds.map(d=>d.pos.x*10+(d.id==='hue-monitor'?monitorSize().width/2+70:100)));
 const cx=screen().pos.x*10,cy=stageY(screen()),tableY=deskTop(layout.desk);
 const guides=`<g class="reference-guides" aria-hidden="true" pointer-events="none">
 <path class="table-center-axis" d="M500 ${top}V${STAGE_HEIGHT}"/>
 <path class="monitor-axis" d="M${cx} ${top}V${STAGE_HEIGHT}M${left} ${cy}H${right}"/>
 <path class="table-baseline" d="M${left} ${tableY}H${right}"/></g>`;
 if(!dragging)stage.setAttribute('viewBox',`${left} ${top} ${right-left} ${STAGE_HEIGHT-top}`);
 $('#show-sync-bounds').checked=showSyncBounds;
 stage.innerHTML=`<defs><pattern id="grid" width="${GRID}" height="${GRID}" patternUnits="userSpaceOnUse"><path d="M${GRID} 0H0V${GRID}" fill="none" stroke="#3b4c40" stroke-width="1"/><circle cx="0" cy="0" r="1.5" fill="#607858"/></pattern><pattern id="grid-major" width="${GRID*2}" height="${GRID*2}" patternUnits="userSpaceOnUse"><path d="M${GRID*2} 0H0V${GRID*2}" fill="none" stroke="#718878" stroke-width="1.2"/></pattern><filter id="wall-soft" filterUnits="userSpaceOnUse" x="${left-200}" y="${top-200}" width="${right-left+400}" height="${STAGE_HEIGHT-top+400}" color-interpolation-filters="sRGB"><feGaussianBlur stdDeviation="32"/></filter></defs><rect x="${left-200}" y="${top}" width="${right-left+400}" height="${STAGE_HEIGHT-top}" fill="url(#grid)" opacity=".45"/><rect x="${left-200}" y="${top}" width="${right-left+400}" height="${STAGE_HEIGHT-top}" fill="url(#grid-major)" opacity=".45"/><g class="wall-glow-layer" filter="url(#wall-soft)" aria-hidden="true" pointer-events="none">${fixtures.map(d=>lamp(d,true)).join('')}</g>`+guides+syncBounds(left,right)+room()+fixtures.map(d=>lamp(d)).join('');
}
function renderSelection(){
 const d=current(),panel=$('#selection-panel');panel.hidden=!d&&!['desk','monitor'].includes(selected);
 if(selected==='monitor'){panel.innerHTML=`<button class="close" data-action="deselect" aria-label="Закрити параметри монітора">×</button><h2>Монітор</h2><p>Формат 16:9</p><label class="dimension-input">Діагональ, дюйми<input type="number" id="monitor-inches" min="19" max="55" step="0.1" value="${layout.monitorInches}"></label><label class="dimension-input">Центр від підлоги, см<input type="number" id="monitor-height" step="5" max="250" value="${heightCm(screen())}"></label><label class="dimension-input">Зсув від центру, см<input type="number" id="monitor-offset" min="-300" max="300" step="5" value="${Math.round(screen().pos.x/100*COLS)*STEP_CM-100}"></label><p class="position-hint">Мінус — ліворуч, плюс — праворуч.</p>`;return;}
 if(selected==='desk'){
  panel.innerHTML=`<button class="close" data-action="deselect" aria-label="Закрити розміри столу">×</button><h2>Розміри столу</h2><p>Схема масштабується автоматично.</p><label class="dimension-input">Ширина, см<input type="number" id="desk-width" min="80" max="240" step="1" value="${layout.desk.width}"></label><label class="dimension-input">Висота стільниці, см<input type="number" id="desk-height" min="50" max="120" step="1" value="${layout.desk.height}"></label>`;return;
 }
 if(!d)return;
 const offset=Math.round(d.pos.x/100*COLS)*STEP_CM-COLS*STEP_CM/2;
 const horizontal=offset===0?'По центру':`${Math.abs(offset)} см ${offset<0?'ліворуч':'праворуч'} від центру`;
 const elevation=heightCm(d)-(d.shape==='floor'?60:0);
 panel.innerHTML=`<button class="close" data-action="deselect" aria-label="Закрити параметри лампи">×</button><h2>${esc(d.name)}</h2><p>${esc(d.model)} · ${typeNames[d.shape]}${d.shape==='floor'?` · ${GOVEE_HEIGHT_CM} см`:d.shape==='bar'?` · ${PLAY_LENGTH_CM} см`:''}</p><div class="automatic"><i>✓</i>${horizontal} · ${elevation} см від підлоги</div><div class="brightness"><label for="brightness">Яскравість <output id="brightness-value">${Math.round(d.scale)}%</output></label><input id="brightness" type="range" min="0" max="100" step="1" value="${d.scale}" aria-label="Яскравість вибраної лампи"></div>${d.shape==='bar'?`<fieldset class="orientation"><legend>Розташування</legend><label><input type="radio" name="orientation" value="horizontal" ${d.orientation!=='vertical'?'checked':''}>Горизонтально</label><label><input type="radio" name="orientation" value="vertical" ${d.orientation==='vertical'?'checked':''}>Вертикально</label></fieldset>`:''}${realDevice(d.id)?'<p class="placement-note">Ця лампа ще не отримує кольори. Параметри стосуються розташування й калібрування.</p>':''}${removable(d.id)?'<button class="remove" data-action="remove">Прибрати з макета</button>':''}`;
}
function render(){if(selected&&!['desk','monitor'].includes(selected)&&!current()){selected=null;dragging=null;}renderStage();renderSelection();$('.room').textContent=layout.room;}
function choose(id){selected=id;render();}
function autoSnap(d,desk=layout.desk,inches=layout.monitorInches){
 const size=monitorSize(inches);
 const attached=d.id==='hue-monitor',floor=d.shape==='floor';
 const monitorMaxY=Math.floor((deskTop(desk)-size.height/2-5)/GRID);
 d.pos.x=clamp(Math.round(d.pos.x*10/GRID),-COLS,COLS*2)*GRID/10;
 d.pos.y=clamp(Math.round(stageY(d)/GRID),attached?Math.min(monitorMaxY,Math.floor((FLOOR_Y-250*UNITS_PER_CM)/GRID)):floor?(FLOOR_Y-300*UNITS_PER_CM)/GRID:1,floor?(FLOOR_Y-300)/GRID:attached?monitorMaxY:Math.floor((FLOOR_Y-10*UNITS_PER_CM)/GRID))*GRID/STAGE_HEIGHT*100;
}

function packLayout(data=layout){
 return {version:3,gridStepCm:STEP_CM,room:data.room,desk:clone(data.desk),monitorInches:data.monitorInches,devices:data.devices.map(d=>({
  id:d.id,scale:d.scale,orientation:d.orientation||'horizontal',
  ...(!enabled(d)?{enabled:false}:{}),
  positionCm:d.positionCm?clone(d.positionCm):{x:Math.round(d.pos.x/100*COLS)*STEP_CM,height:heightCm(d)},
  ...(realDevice(d.id)?{shape:d.shape,name:d.name,model:d.model,zones:d.zones}:d.id.startsWith('virtual-')?{shape:d.shape,name:d.name}:{})
 }))};
}
function mergeLayouts(base,local,remote){
 const result=clone(remote),equal=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
 const field=(out,key,b,l)=>{if(!equal(b,l)){if(l===undefined)delete out[key];else out[key]=clone(l);}};
 for(const key of ['room','monitorInches'])field(result,key,base[key],local[key]);
 for(const key of ['width','height'])field(result.desk,key,base.desk[key],local.desk[key]);
 const old=new Map(base.devices.map(d=>[d.id,d])),mine=new Map(local.devices.map(d=>[d.id,d]));
 for(const [id,previous]of old)if(!mine.has(id))result.devices=result.devices.filter(d=>d.id!==id);
 for(const [id,current]of mine){
  const previous=old.get(id),target=result.devices.find(d=>d.id===id);
  if(!previous){if(!target)result.devices.push(clone(current));continue;}
  if(!target){if(!equal(previous,current))result.devices.push(clone(current));continue;}
  for(const key of ['scale','orientation','name','shape','model','zones'])field(target,key,previous[key],current[key]);
  field(target,'enabled',enabled(previous)?undefined:false,enabled(current)?undefined:false);
  for(const key of ['x','height'])field(target.positionCm,key,previous.positionCm[key],current.positionCm[key]);
 }
 return result;
}
function readServerBase(){try{return JSON.parse(localStorage.getItem(KEY+'-base'));}catch{return null;}}
let serverReady=false,revision=null,pendingLayout=null,saving=false,saveTimer,retryTimer,offlineNotice=false;
let serverBaseline=readServerBase()||packLayout();
function save(){try{const saved=packLayout();localStorage.setItem(KEY,JSON.stringify(saved));if(editGeneration>0)localStorage.setItem(KEY+'-pending','true');pendingLayout=saved;clearTimeout(saveTimer);if(serverReady)saveTimer=setTimeout(flushLayout,300);}catch{notify('Браузер не дозволив зберегти макет');}}
function acceptBaseline(saved){serverBaseline=clone(saved.layout);revision=saved.revision;localStorage.setItem(KEY+'-base',JSON.stringify(serverBaseline));}
function reconcile(saved,base=serverBaseline){
 const merged=mergeLayouts(base,packLayout(),saved.layout);
 history=history.map(item=>load(mergeLayouts(base,packLayout(item),saved.layout)));
 layout=load(merged);acceptBaseline(saved);localStorage.setItem(KEY,JSON.stringify(packLayout()));render();
}
async function flushLayout(){
 if(saving||!serverReady)return;saving=true;
 try{
  let conflicts=0;
  while(pendingLayout){
   const payload=pendingLayout,base=clone(serverBaseline);pendingLayout=null;
   const response=await fetch('/api/layout',{method:'POST',headers:{'Content-Type':'application/json','If-Match':`"${revision||''}"`},body:JSON.stringify(payload)});
   if(response.status===409&&conflicts++<2){const current=await fetch('/api/layout');if(!current.ok)throw new Error('offline');const remote=await current.json();reconcile(remote,base);pendingLayout=packLayout();continue;}
   if(!response.ok){pendingLayout=pendingLayout||payload;throw new Error('save');}
   acceptBaseline(await response.json());if(!pendingLayout)localStorage.removeItem(KEY+'-pending');offlineNotice=false;
  }
 }catch{
  serverReady=false;if(!offlineNotice)notify('Не вдалося застосувати зміни. Макет збережений локально.');offlineNotice=true;
  clearTimeout(retryTimer);retryTimer=setTimeout(connectLayout,3000);
 }finally{saving=false;}
}
async function connectLayout(){
 try{
  const response=await fetch('/api/layout');if(!response.ok)throw new Error('offline');
  const saved=await response.json(),hasLocalEdits=editGeneration>0||!!localStorage.getItem(KEY+'-pending');revision=saved.revision;
  if(saved.layout){if(hasLocalEdits)reconcile(saved);else{layout=load(saved.layout);acceptBaseline(saved);pendingLayout=null;localStorage.setItem(KEY,JSON.stringify(packLayout()));render();}}
  serverReady=true;offlineNotice=false;
  if(!saved.layout||hasLocalEdits)save();
 }catch{
  serverReady=false;if(!offlineNotice)notify('Пілот недоступний. Макет збережений локально.');offlineNotice=true;
  clearTimeout(retryTimer);retryTimer=setTimeout(connectLayout,3000);
 }
}

let discoverySnapshot=null,discoveryChoices=new Set(),discoveryTimer,discoveryLoading=null,discoverySession=0;
const discoveryPlacement=d=>layout.devices.find(row=>row.id===d.layout_id||row.id===d.id);
function discoveryReason(d){
 const placement=discoveryPlacement(d);
 if(placement&&enabled(placement))return 'Уже на макеті';
 if(d.ambiguous)return 'Неоднозначна ідентичність';
 if(!d.supported||!Object.hasOwn(typeNames,d.shape))return 'Не підтримується';
 if(!d.online)return 'Не в мережі';
 if(!d.selectable&&!(placement&&enrolled(placement.id)&&placement.id===d.layout_id))return 'Недоступна для додавання';
 return '';
}
function renderDiscovery(){
 const content=$('#modal-content'),active=document.activeElement;
 const focusSelector=content.contains(active)?realDevice(active.dataset?.discovery)?`[data-discovery="${active.dataset.discovery}"]`:active.dataset?.action==='add-selected'?'[data-action="add-selected"]':null:null;
 const devices=discoverySnapshot?.devices||[],available=discoverySnapshot?.connected===true;
 const count=devices.filter(d=>!discoveryReason(d)&&discoveryChoices.has(d.id)).length;
 const additions=devices.filter(d=>!discoveryReason(d)&&discoveryChoices.has(d.id)&&!discoveryPlacement(d)).length;
 const providerStates=Object.values(discoverySnapshot?.providers||{});
 const providerIssue=available&&providerStates.some(status=>['unavailable','stale'].includes(status));
 const searching=available&&providerStates.includes('discovering');
 content.innerHTML=`<h2>Додати лампи</h2><p>Обери знайдені лампи для розташування на макеті.</p><div class="discovery-status" role="status">${!discoverySnapshot?'Шукаємо лампи…':!available?'Пошук ламп недоступний. Спробуємо знову автоматично.':providerIssue?'Частина джерел пошуку недоступна. Список оновлюється автоматично.':searching?'Шукаємо лампи… Список оновлюється автоматично.':!devices.length?'Поки немає знайдених ламп. Список оновлюється автоматично.':'Знайдені лампи'}</div><div class="discovery-list">${devices.map(d=>{const reason=discoveryReason(d),disabled=reason||!available;return `<label class="discovery-item ${disabled?'unavailable':''}"><input type="checkbox" data-discovery="${esc(d.id)}" ${disabled?'disabled':''} ${discoveryChoices.has(d.id)?'checked':''}><span><strong>${esc(d.name)}</strong><small>${esc(d.model)} · ${esc(typeNames[d.shape]||'Пристрій')}${reason?` · ${esc(reason)}`:''}</small></span></label>`;}).join('')}</div><div class="discovery-footer"><span>${layout.devices.length}/12 збережених ламп</span><button class="button primary" data-action="add-selected" ${!available||!count||additions>12-layout.devices.length?'disabled':''}>Додати вибрані${count?` (${count})`:''}</button></div><div class="top-help">Нова лампа ще не отримує кольори. Тут налаштовується лише розташування й калібрування.</div>`;
 if(focusSelector)content.querySelector(focusSelector+':not(:disabled)')?.focus({preventScroll:true});
}
async function refreshDiscovery(session=discoverySession){
 if(!$('#modal').open||session!==discoverySession||discoveryLoading===session)return;
 discoveryLoading=session;
 try{const response=await fetch('/api/discovery');if(!response.ok)throw new Error('discovery');const snapshot=await response.json();if(session===discoverySession)discoverySnapshot=snapshot;}
 catch{if(session===discoverySession)discoverySnapshot={connected:false,devices:[],providers:{}};}
 finally{if(discoveryLoading===session)discoveryLoading=null;if($('#modal').open&&session===discoverySession){renderDiscovery();clearTimeout(discoveryTimer);discoveryTimer=setTimeout(()=>refreshDiscovery(session),3000);}}
}
function closeDiscovery(){discoverySession++;clearTimeout(discoveryTimer);$('#modal').close();}
function add(){
 discoveryChoices.clear();discoverySnapshot=null;discoverySession++;renderDiscovery();$('#modal').showModal();refreshDiscovery();
}
function addSelected(){
 if(!discoverySnapshot?.connected)return;
 const devices=discoverySnapshot.devices.filter(d=>realDevice(d.id)&&discoveryChoices.has(d.id)&&!discoveryReason(d));
 if(!devices.length)return;if(layout.devices.length+devices.filter(d=>!discoveryPlacement(d)).length>12){notify('У цьому макеті може бути до 12 ламп');return;}
 remember();
 for(const item of devices){const placement=discoveryPlacement(item);if(placement){placement.enabled=true;selected=placement.id;continue;}const d={id:item.id,name:item.name,short:item.name,model:item.model,shape:item.shape,zones:item.zones,scale:80,rate:0,orientation:'horizontal',pos:{x:50,y:72}};autoSnap(d);layout.devices.push(d);selected=d.id;}
 dirty();render();closeDiscovery();
}
const actions={add,'add-selected':addSelected,'close-modal':closeDiscovery,deselect:()=>{selected=null;render();},remove:()=>{const d=current();if(selected==='monitor'||!removable(d?.id))return;remember();if(enrolled(d.id))d.enabled=false;else layout.devices=layout.devices.filter(row=>row.id!==d.id);selected=null;dirty();render();}};
document.addEventListener('click',event=>{
 const button=event.target.closest('button');if(!button)return;
 if(button.dataset.action)actions[button.dataset.action]?.();
});
document.addEventListener('change',event=>{
 if(event.target.id==='show-sync-bounds'){
  showSyncBounds=event.target.checked;
  try{localStorage.setItem(VIEW_SYNC_KEY,String(showSyncBounds));}catch{}
  renderStage();return;
 }
 if(event.target.dataset?.discovery){const id=event.target.dataset.discovery;if(event.target.checked)discoveryChoices.add(id);else discoveryChoices.delete(id);renderDiscovery();return;}
 if(['monitor-height','monitor-offset'].includes(event.target.id)){
  if(!event.target.value||!Number.isFinite(event.target.valueAsNumber)){renderSelection();return;}
  remember();const monitor=screen();
  if(event.target.id==='monitor-height')monitor.pos.y=(FLOOR_Y-event.target.valueAsNumber*UNITS_PER_CM)/STAGE_HEIGHT*100;
  else monitor.pos.x=(event.target.valueAsNumber+100)/(COLS*STEP_CM)*100;
  autoSnap(monitor);dirty();render();return;
 }
 if(event.target.name==='orientation'&&current()?.shape==='bar'){remember();current().orientation=event.target.value==='vertical'?'vertical':'horizontal';dirty();renderStage();return;}
 if(event.target.id==='monitor-inches'){if(!event.target.value||!Number.isFinite(event.target.valueAsNumber)){renderSelection();return;}remember();layout.monitorInches=Math.round(clamp(event.target.value,19,55)*10)/10;autoSnap(screen());dirty();render();return;}
 if(!['desk-width','desk-height'].includes(event.target.id))return;
 const height=event.target.id==='desk-height',key=height?'height':'width';
 if(!event.target.value||!Number.isFinite(event.target.valueAsNumber)){renderSelection();return;}
 remember();const previous=layout.desk.height;
 layout.desk[key]=Math.round(clamp(event.target.value,height?50:80,height?120:240));
 if(height){const monitor=screen();monitor.pos.y-=(layout.desk.height-previous)*UNITS_PER_CM/STAGE_HEIGHT*100;autoSnap(monitor);}
 dirty();render();
});
document.addEventListener('pointerdown',event=>{if(event.target.id==='brightness')remember();});
document.addEventListener('input',event=>{if(event.target.id!=='brightness'||!current())return;current().scale=clamp(event.target.value,0,100);$('#brightness-value').textContent=Math.round(current().scale)+'%';dirty();renderStage();});
stage.addEventListener('pointerdown',event=>{
 if(event.target.closest('[data-desk]')){selected='desk';renderSelection();return;}
 const node=event.target.closest('[data-lamp],[data-monitor]');
 if(!node){selected=null;render();return;}
 selected=node.hasAttribute('data-monitor')?'monitor':node.dataset.lamp;const d=current();remember();dragging={id:d.id,start:point(event),pos:clone(d.pos),moved:false};stage.setPointerCapture(event.pointerId);if(selected==='monitor'){stage.querySelectorAll('.selected').forEach(item=>item.classList.remove('selected'));node.classList.add('selected');renderSelection();}else render();
});
stage.addEventListener('pointermove',event=>{
 if(!dragging)return;const p=point(event),d=current();const dx=(p.x-dragging.start.x)/10,dy=(p.y-dragging.start.y)/STAGE_HEIGHT*100;
 d.pos={x:dragging.pos.x+dx,y:dragging.pos.y+dy};autoSnap(d);dragging.moved=d.pos.x!==dragging.pos.x||d.pos.y!==dragging.pos.y;renderStage();
});
function finishDrag(){if(!dragging)return;const moved=dragging.moved;if(moved)dirty();else history.pop();dragging=null;if(moved)renderStage();renderSelection();stage.querySelector(selected==='monitor'?'[data-monitor]':`[data-lamp="${selected}"]`)?.focus({preventScroll:true});}
stage.addEventListener('pointerup',finishDrag);stage.addEventListener('pointercancel',finishDrag);
stage.addEventListener('click',event=>{if(event.target.closest('[data-monitor]')){selected='monitor';renderSelection();stage.querySelector('[data-monitor]')?.focus({preventScroll:true});return;}const node=event.target.closest('[data-lamp]');if(node&&selected!==node.dataset.lamp)choose(node.dataset.lamp);});
stage.addEventListener('keydown',event=>{
 if(event.target.closest('[data-desk]')&&(event.key==='Enter'||event.key===' ')){event.preventDefault();selected='desk';renderSelection();return;}
 const node=event.target.closest('[data-lamp],[data-monitor]');if(!node)return;
 const id=node.hasAttribute('data-monitor')?'monitor':node.dataset.lamp;
 if(event.key==='Enter'||event.key===' '){event.preventDefault();choose(id);return;}
 if(!event.key.startsWith('Arrow'))return;event.preventDefault();selected=id;remember();const d=current(),stepX=GRID/10,stepY=GRID/STAGE_HEIGHT*100;
 if(event.key==='ArrowLeft')d.pos.x-=stepX;if(event.key==='ArrowRight')d.pos.x+=stepX;if(event.key==='ArrowUp')d.pos.y-=stepY;if(event.key==='ArrowDown')d.pos.y+=stepY;autoSnap(d);dirty();render();stage.querySelector(selected==='monitor'?'[data-monitor]':`[data-lamp="${selected}"]`)?.focus({preventScroll:true});
});
document.addEventListener('keydown',event=>{if(event.key==='Escape'){selected=null;render();}if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='z'&&history.length&&document.activeElement?.tagName!=='INPUT'){event.preventDefault();layout=history.pop();if(!['desk','monitor'].includes(selected)&&!layout.devices.some(d=>d.id===selected))selected=null;dirty();render();}});
render();
save();
connectLayout();
watchPreview();
refreshPreview();
