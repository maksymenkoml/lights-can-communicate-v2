const $ = selector => document.querySelector(selector);
const clone = value => structuredClone(value);
const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clamp = (v,min,max) => Math.min(max,Math.max(min,Number(v)||0));
const KEY='light-studio-grid-v3';
const GRID=50, STEP_CM=10, COLS=20;
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
const palette=['#809bc4','#a98bbd','#cb9cae','#dab493','#cfc49b'].map(hex=>[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16)));
function defaults(){const data={version:3,room:'Mykola Office',desk:clone(deskDefaults),monitorInches:32,devices:clone(seed)};data.devices.forEach(d=>autoSnap(d,data.desk,data.monitorInches));return data;}
function load(){
 try{
  const saved=JSON.parse(localStorage.getItem(KEY)||localStorage.getItem('light-studio-simple-v2')||localStorage.getItem('light-studio-layout-v1'));
  if(![1,2,3].includes(saved?.version)||!Array.isArray(saved.devices))return defaults();
  const data=defaults();data.desk={width:clamp(saved.desk?.width??160,80,240),height:clamp(saved.desk?.height??85,50,120)};data.monitorInches=clamp(saved.monitorInches??32,19,55);data.room=String(saved.room||data.room).slice(0,40);
  data.devices=data.devices.map(d=>{const s=saved.devices.find(x=>x.id===d.id);if(s?.positionCm)s.pos=positionFromCm(s.positionCm);return s?{...d,scale:clamp(s.scale??d.scale,0,100),orientation:s.orientation==='vertical'?'vertical':'horizontal',pos:{x:clamp(s.pos?.x??d.pos.x,-100,200),y:clamp(s.pos?.y??d.pos.y,12,88)}}:d;});
  for(const s of saved.devices.filter(x=>/^virtual-[a-z0-9]+$/.test(x.id||'')).slice(0,6)){
   const shape=Object.hasOwn(typeNames,s.shape)?s.shape:'bar';if(s.positionCm)s.pos=positionFromCm(s.positionCm);
   data.devices.push({id:s.id,name:String(s.name||'Нова лампа').slice(0,35),short:'Нова лампа',model:'Макет',shape,zones:shape==='floor'?7:shape==='strip'?9:1,scale:clamp(s.scale??80,0,100),rate:0,orientation:s.orientation==='vertical'?'vertical':'horizontal',pos:{x:clamp(s.pos?.x??50,-100,200),y:clamp(s.pos?.y??70,12,88)}});
  }
  data.devices.forEach(d=>autoSnap(d,data.desk,data.monitorInches));return data;
 }catch{return defaults();}
}
let layout=load(),selected=null,dragging=null,history=[],toastTimer;
const stage=$('#stage');
const current=()=>layout.devices.find(d=>d.id===selected);
const screen=()=>layout.devices.find(d=>d.id==='hue-monitor')||seed[2];
function remember(){history.push(clone(layout));if(history.length>25)history.shift();}
function dirty(){$('#save-state').textContent='Є незбережені зміни. На фізичне світло вони поки не впливають.';}
function notify(text){$('#toast').textContent=text;$('#toast').classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.remove('visible'),3000);}
function point(event){return new DOMPoint(event.clientX,event.clientY).matrixTransform(stage.getScreenCTM().inverse());}
function color(d,index){
 const relative=d.pos.x/100*.52+(1-d.pos.y/100)*.3+index/Math.max(1,d.zones-1)*.17;
 const p=((relative%1)+1)%1*(palette.length-1),j=Math.floor(p),t=p-j;
 const rgb=palette[j].map((v,k)=>Math.round((v+(palette[Math.min(j+1,palette.length-1)][k]-v)*t)*(.3+.7*d.scale/100)));
 return `rgb(${rgb.join(',')})`;
}
function room(){
 const monitor=screen(),cx=monitor.pos.x*10,cy=stageY(monitor),{width:w,height:h}=monitorSize();
 const DESK_WIDTH=layout.desk.width*UNITS_PER_CM,DESK_Y=deskTop(layout.desk);
 const left=500-DESK_WIDTH/2,right=500+DESK_WIDTH/2;
 return `<path class="detail-line" d="M-1000 ${FLOOR_Y}H2000"/>
 <g class="desk-target" data-desk role="button" tabindex="0" aria-label="Розміри столу: ${layout.desk.width} на ${layout.desk.height} см"><rect class="desk-hit" x="${left-12}" y="${DESK_Y-12}" width="${DESK_WIDTH+24}" height="85"/><path class="desk" d="M${left} ${DESK_Y}H${right}M${left+20} ${DESK_Y+5}V${FLOOR_Y}M${right-20} ${DESK_Y+5}V${FLOOR_Y}"/>
 <path class="dimension" d="M${left} ${DESK_Y+40}H${right}M${left} ${DESK_Y+33}v14M${right} ${DESK_Y+33}v14M${right+35} ${DESK_Y}V${FLOOR_Y}M${right+28} ${DESK_Y}h14M${right+28} ${FLOOR_Y}h14"/>
 <text class="measurement" x="500" y="${DESK_Y+65}">${layout.desk.width} см · ✎</text>
 <text class="measurement" x="${right+52}" y="${(DESK_Y+FLOOR_Y)/2}" transform="rotate(-90 ${right+52} ${(DESK_Y+FLOOR_Y)/2})">${layout.desk.height} см</text></g>
 <g data-monitor class="monitor-target" role="button" tabindex="0" aria-label="Розмір монітора: ${layout.monitorInches} дюйми"><rect class="monitor" x="${cx-w/2}" y="${cy-h/2}" width="${w}" height="${h}" rx="7"/>
 <rect x="${cx-w/2+7}" y="${cy-h/2+7}" width="${w-14}" height="${h-14}" rx="4" fill="#1c282c"/>
 <text class="screen-copy" x="${cx}" y="${cy+4}">${layout.monitorInches}″ · 16:9 · ✎</text></g>
 <path class="desk" d="M${cx} ${cy+h/2+2}V${DESK_Y-3}M${cx-35} ${DESK_Y-3}h70"/>`;
}
function lamp(d){
 const x=d.pos.x*10,y=stageY(d);
 const attached=d.id==='hue-monitor';
 const vertical=d.shape==='floor'||(d.shape==='bar'&&d.orientation==='vertical');
 let body='',light='',box,nameY;
 if(d.shape==='floor'){
  const base=300,top=base-GOVEE_HEIGHT_CM*UNITS_PER_CM,lightBottom=270,lightLength=lightBottom-top-5;
  body=`<path d="M0 ${top}V279" stroke="#51644f" stroke-width="11" stroke-linecap="butt"/><path d="M0 279v19" stroke="#71826a" stroke-width="2"/><ellipse class="fixture" cx="0" cy="300" rx="21" ry="5"/>`;
  light=Array.from({length:d.zones},(_,i)=>`<path class="light" d="M0 ${lightBottom-i*lightLength/d.zones}v${-lightLength/d.zones-1}" stroke="${color(d,i)}" stroke-width="5" stroke-linecap="round"/>`).join('');
  box=[-37,top-15,74,base-top+35];nameY=345;
 }else if(attached){
  const size=monitorSize(),w=size.width/2+9,h=size.height/2+9;
  const points=[[-w,h],[-w,0],[-w,-h],[-w/2,-h],[0,-h],[w/2,-h],[w,-h],[w,0],[w,h]];
  const path=`M${-w} ${h}V${-h}H${w}V${h}`;
  body=`<path class="strip-back" d="${path}"/>`;
  light=`<path class="light-path" d="${path}" stroke="${color(d,4)}"/>`+points.map(([px,py],i)=>`<circle class="light" cx="${px}" cy="${py}" r="4" fill="${color(d,i)}"/>`).join('');
  box=[-w-15,-h-15,w*2+30,h*2+30];nameY=-h-29;
 }else{
  const length=d.shape==='strip'?220:PLAY_LENGTH_CM*UNITS_PER_CM;
  body=`<rect class="fixture" x="${vertical?-9:-length/2}" y="${vertical?-length/2:-9}" width="${vertical?18:length}" height="${vertical?length:18}" rx="8"/>`;
  light=`<path class="light" d="${vertical?`M0 ${-length/2+7}v${length-14}`:`M${-length/2+7} 0h${length-14}`}" stroke="${color(d,0)}" stroke-width="6" stroke-linecap="round"/>`;
  box=vertical?[-27,-length/2-15,54,length+30]:[-length/2-14,-26,length+28,52];nameY=vertical?length/2+35:39;
 }
 return `<g class="lamp ${selected===d.id?'selected':''}" data-lamp="${d.id}" role="button" tabindex="0" aria-label="${esc(d.name)}" aria-pressed="${selected===d.id}" transform="translate(${x},${y})"><rect class="hit ${attached?'strip-hit':''}" x="${box[0]-18}" y="${box[1]-12}" width="${box[2]+36}" height="${box[3]+24}" rx="12"/><rect class="halo" x="${box[0]}" y="${box[1]}" width="${box[2]}" height="${box[3]}" rx="12"/>${body}${light}<text class="lamp-name long-name" x="0" y="${nameY}">${esc(d.name)}</text><text class="lamp-name short-name" x="0" y="${nameY}">${esc(d.short)}</text></g>`;
}
function renderStage(){
 const top=Math.min(0,stageY(screen())-monitorSize().height/2-70);
 const deskHalf=layout.desk.width*UNITS_PER_CM/2;
 const left=Math.min(-150,500-deskHalf-100,...layout.devices.map(d=>d.pos.x*10-(d.id==='hue-monitor'?monitorSize().width/2+70:100)));
 const right=Math.max(1150,500+deskHalf+100,...layout.devices.map(d=>d.pos.x*10+(d.id==='hue-monitor'?monitorSize().width/2+70:100)));
 if(!dragging)stage.setAttribute('viewBox',`${left} ${top} ${right-left} ${STAGE_HEIGHT-top}`);
 stage.innerHTML=`<defs><pattern id="grid" width="${GRID}" height="${GRID}" patternUnits="userSpaceOnUse"><path d="M${GRID} 0H0V${GRID}" fill="none" stroke="#3b4c40" stroke-width="1"/><circle cx="0" cy="0" r="1.5" fill="#607858"/></pattern></defs><rect x="${left-200}" y="${top}" width="${right-left+400}" height="${STAGE_HEIGHT-top}" fill="url(#grid)" opacity=".45"/>`+room()+layout.devices.map(lamp).join('');
}
function renderSelection(){
 const d=current(),panel=$('#selection-panel');panel.hidden=!d&&!['desk','monitor'].includes(selected);
 if(selected==='monitor'){panel.innerHTML=`<button class="close" data-action="deselect" aria-label="Закрити розмір монітора">×</button><h2>Розмір монітора</h2><p>Формат 16:9. Стрічка підлаштується.</p><label class="dimension-input">Діагональ, дюйми<input type="number" id="monitor-inches" min="19" max="55" step="0.1" value="${layout.monitorInches}"></label>`;return;}
 if(selected==='desk'){
  panel.innerHTML=`<button class="close" data-action="deselect" aria-label="Закрити розміри столу">×</button><h2>Розміри столу</h2><p>Схема масштабується автоматично.</p><label class="dimension-input">Ширина, см<input type="number" id="desk-width" min="80" max="240" step="1" value="${layout.desk.width}"></label><label class="dimension-input">Висота стільниці, см<input type="number" id="desk-height" min="50" max="120" step="1" value="${layout.desk.height}"></label>`;return;
 }
 if(!d)return;
 const offset=Math.round(d.pos.x/100*COLS)*STEP_CM-COLS*STEP_CM/2;
 const horizontal=offset===0?'По центру':`${Math.abs(offset)} см ${offset<0?'ліворуч':'праворуч'} від центру`;
 const elevation=heightCm(d)-(d.shape==='floor'?60:0);
 panel.innerHTML=`<button class="close" data-action="deselect" aria-label="Закрити параметри лампи">×</button><h2>${esc(d.name)}</h2><p>${esc(d.model)} · ${typeNames[d.shape]}${d.shape==='floor'?` · ${GOVEE_HEIGHT_CM} см`:d.shape==='bar'?` · ${PLAY_LENGTH_CM} см`:''}</p><div class="automatic"><i>✓</i>${horizontal} · ${elevation} см від підлоги</div><div class="brightness"><label for="brightness">Яскравість <output id="brightness-value">${Math.round(d.scale)}%</output></label><input id="brightness" type="range" min="0" max="100" step="1" value="${d.scale}" aria-label="Яскравість вибраної лампи"></div>${d.shape==='bar'?`<fieldset class="orientation"><legend>Розташування</legend><label><input type="radio" name="orientation" value="horizontal" ${d.orientation!=='vertical'?'checked':''}>Горизонтально</label><label><input type="radio" name="orientation" value="vertical" ${d.orientation==='vertical'?'checked':''}>Вертикально</label></fieldset>`:''}${d.id.startsWith('virtual-')?'<button class="remove" data-action="remove">Прибрати з макета</button>':''}`;
}
function render(){renderStage();renderSelection();$('.room').textContent=layout.room;}
function choose(id){selected=id;render();}
function autoSnap(d,desk=layout.desk,inches=layout.monitorInches){
 const size=monitorSize(inches);
 const attached=d.id==='hue-monitor',floor=d.shape==='floor';
 const monitorMaxY=Math.floor((deskTop(desk)-size.height/2-5)/GRID);
 d.pos.x=clamp(Math.round(d.pos.x*10/GRID),-COLS,COLS*2)*GRID/10;
 d.pos.y=clamp(Math.round(stageY(d)/GRID),attached?Math.min(monitorMaxY,Math.ceil((size.height/2+40)/GRID)):floor?Math.ceil((GOVEE_HEIGHT_CM*UNITS_PER_CM-300+20)/GRID):1,floor?11:attached?monitorMaxY:16)*GRID/STAGE_HEIGHT*100;
}

function save(){try{const saved={...layout,version:3,gridStepCm:STEP_CM,devices:layout.devices.map(({pos,...d})=>({...d,positionCm:{x:Math.round(pos.x/100*COLS)*STEP_CM,height:heightCm({pos})}}))};localStorage.setItem(KEY,JSON.stringify(saved));$('#save-state').textContent='Макет збережено в цьому браузері. Фізичне світло не змінювалось.';notify('Розташування збережено');}catch{notify('Браузер не дозволив зберегти макет');}}
function add(){
 if(layout.devices.length>=12){notify('У цьому макеті вже 12 ламп');return;}
 $('#modal-content').innerHTML=`<h2>Яку лампу додати?</h2><p>Обери форму. Решту макет підбере автоматично.</p><div class="types"><button class="type" data-add="floor"><svg viewBox="0 0 40 50"><path d="M20 4v36M10 44h20"/></svg>Торшер</button><button class="type" data-add="strip"><svg viewBox="0 0 40 50"><path d="M5 42V9h30v33"/></svg>Стрічка</button><button class="type" data-add="bar"><svg viewBox="0 0 40 50"><rect x="7" y="16" width="26" height="14" rx="6"/></svg>Панель</button></div><div class="top-help">Новий об’єкт додається лише до макета.</div>`;$('#modal').showModal();
}
const actions={save,add,'close-modal':()=>$('#modal').close(),deselect:()=>{selected=null;render();},reset:()=>{remember();layout=defaults();selected=null;dirty();render();notify('Початкове розташування відновлено');},remove:()=>{if(!current()?.id.startsWith('virtual-'))return;remember();layout.devices=layout.devices.filter(d=>d.id!==selected);selected=null;dirty();render();}};
document.addEventListener('click',event=>{
 const button=event.target.closest('button');if(!button)return;
 if(button.dataset.action)actions[button.dataset.action]?.();
 if(button.dataset.add){const shape=button.dataset.add;remember();const d={id:'virtual-'+Date.now().toString(36),name:`${typeNames[shape]} ${layout.devices.filter(x=>x.shape===shape).length+1}`,short:typeNames[shape],model:'Макет',shape,zones:shape==='floor'?7:shape==='strip'?9:1,scale:80,rate:0,orientation:'horizontal',pos:{x:50,y:72}};autoSnap(d);layout.devices.push(d);selected=d.id;dirty();render();$('#modal').close();}
});
document.addEventListener('change',event=>{
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
 if(event.target.closest('[data-monitor]')){selected='monitor';renderSelection();return;}
 if(event.target.closest('[data-desk]')){selected='desk';renderSelection();return;}
 const node=event.target.closest('[data-lamp]');
 if(!node){selected=null;render();return;}
 selected=node.dataset.lamp;const d=current();remember();dragging={id:d.id,start:point(event),pos:clone(d.pos),moved:false};stage.setPointerCapture(event.pointerId);render();
});
stage.addEventListener('pointermove',event=>{
 if(!dragging)return;const p=point(event),d=current();const dx=(p.x-dragging.start.x)/10,dy=(p.y-dragging.start.y)/STAGE_HEIGHT*100;
 d.pos={x:dragging.pos.x+dx,y:dragging.pos.y+dy};autoSnap(d);dragging.moved=d.pos.x!==dragging.pos.x||d.pos.y!==dragging.pos.y;renderStage();
});
function finishDrag(){if(!dragging)return;if(dragging.moved)dirty();else history.pop();dragging=null;renderStage();renderSelection();stage.querySelector(`[data-lamp="${selected}"]`)?.focus({preventScroll:true});}
stage.addEventListener('pointerup',finishDrag);stage.addEventListener('pointercancel',finishDrag);
stage.addEventListener('click',event=>{const node=event.target.closest('[data-lamp]');if(node&&selected!==node.dataset.lamp)choose(node.dataset.lamp);});
stage.addEventListener('keydown',event=>{
 if(event.target.closest('[data-monitor]')&&(event.key==='Enter'||event.key===' ')){event.preventDefault();selected='monitor';renderSelection();return;}
 if(event.target.closest('[data-desk]')&&(event.key==='Enter'||event.key===' ')){event.preventDefault();selected='desk';renderSelection();return;}
 const node=event.target.closest('[data-lamp]');if(!node)return;
 if(event.key==='Enter'||event.key===' '){event.preventDefault();choose(node.dataset.lamp);return;}
 if(!event.key.startsWith('Arrow'))return;event.preventDefault();selected=node.dataset.lamp;remember();const d=current(),stepX=GRID/10,stepY=GRID/STAGE_HEIGHT*100;
 if(event.key==='ArrowLeft')d.pos.x-=stepX;if(event.key==='ArrowRight')d.pos.x+=stepX;if(event.key==='ArrowUp')d.pos.y-=stepY;if(event.key==='ArrowDown')d.pos.y+=stepY;autoSnap(d);dirty();render();stage.querySelector(`[data-lamp="${selected}"]`)?.focus({preventScroll:true});
});
document.addEventListener('keydown',event=>{if(event.key==='Escape'){selected=null;render();}if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='z'&&history.length&&document.activeElement?.tagName!=='INPUT'){event.preventDefault();layout=history.pop();if(!['desk','monitor'].includes(selected)&&!layout.devices.some(d=>d.id===selected))selected=null;dirty();render();}});
render();
try{if(localStorage.getItem(KEY))$('#save-state').textContent='Збережений макет. Фізичне світло працює незалежно від нього.';}catch{}
