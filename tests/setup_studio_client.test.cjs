// Light Studio client contracts. The real index.html and app.js run in a vm with a
// small DOM (HTML/SVG parser, selector engine, events) and a fake clock and server.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const STATIC=path.join(__dirname,'../studio/static');
const code=fs.readFileSync(path.join(STATIC,'app.js'),'utf8');
const page=fs.readFileSync(path.join(STATIC,'index.html'),'utf8');
const SVG_NS='http://www.w3.org/2000/svg';
const VOID=new Set(['input','br','img','meta','link','hr']);
const ENTITIES={amp:'&',lt:'<',gt:'>',quot:'"','#39':"'",apos:"'",nbsp:' '};
const decode=text=>text.replace(/&(#x?[0-9a-f]+|[a-z]+|#39);/gi,(m,name)=>name in ENTITIES?ENTITIES[name]:name[0]==='#'?String.fromCodePoint(name[1]==='x'?parseInt(name.slice(2),16):+name.slice(1)):m);
const escAttr=value=>String(value).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
const escText=value=>String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

// ---------------------------------------------------------------- mini DOM --
function createDom(clock){
 let document;
 class Text{constructor(text){this.nodeType=3;this.data=text;this.parentNode=null;}get textContent(){return this.data}set textContent(v){this.data=String(v)}remove(){detach(this)}}
 const detach=node=>{if(node.parentNode){const list=node.parentNode.childNodes;list.splice(list.indexOf(node),1);node.parentNode=null;}};
 const adopt=(parent,node,index)=>{detach(node);node.parentNode=parent;parent.childNodes.splice(index??parent.childNodes.length,0,node);};
 const styleObject=()=>{const props=new Map();return {setProperty(k,v){props.set(k,String(v))},getPropertyValue(k){return props.get(k)??''},removeProperty(k){props.delete(k)},_props:props};};
 class Element{
  constructor(tag,ns){this.nodeType=1;this.namespaceURI=ns;this.localName=ns===SVG_NS?tag:tag.toLowerCase();this.attrs=new Map();this.childNodes=[];this.parentNode=null;this.listeners=[];this.style=styleObject();this.writes=0;this.scrollTop=0;this.offsetWidth=0;this.offsetHeight=0;this.inert=false;}
  get tagName(){return this.namespaceURI===SVG_NS?this.localName:this.localName.toUpperCase()}
  getAttribute(k){return this.attrs.has(k)?this.attrs.get(k):null}
  hasAttribute(k){return this.attrs.has(k)}
  setAttribute(k,v){v=String(v);this.attrs.set(k,v);if(k==='style')for(const part of v.split(';')){const i=part.indexOf(':');if(i>0)this.style.setProperty(part.slice(0,i).trim(),part.slice(i+1).trim())}if(k==='value')this._value=v;}
  removeAttribute(k){this.attrs.delete(k)}
  get attributes(){return [...this.attrs].map(([name,value])=>({name,value}))}
  get id(){return this.getAttribute('id')||''}set id(v){this.setAttribute('id',v)}
  get className(){return this.getAttribute('class')||''}set className(v){this.setAttribute('class',v)}
  get title(){return this.getAttribute('title')||''}set title(v){this.setAttribute('title',v)}
  get hidden(){return this.hasAttribute('hidden')}set hidden(v){v?this.setAttribute('hidden',''):this.removeAttribute('hidden')}
  get disabled(){return this.hasAttribute('disabled')}set disabled(v){v?this.setAttribute('disabled',''):this.removeAttribute('disabled')}
  get readOnly(){return this.hasAttribute('readonly')}
  get value(){return this._value??this.getAttribute('value')??''}set value(v){this._value=String(v)}
  get valueAsNumber(){return this.value===''?NaN:Number(this.value)}
  get classList(){const el=this,list=()=>el.className.split(/\s+/).filter(Boolean);return {contains:c=>list().includes(c),add:(...cs)=>el.className=[...new Set([...list(),...cs])].join(' '),remove:(...cs)=>el.className=list().filter(c=>!cs.includes(c)).join(' '),toggle(c,force){const on=force===undefined?!list().includes(c):!!force;on?this.add(c):this.remove(c);return on}};}
  get dataset(){const el=this,attr=k=>'data-'+k.replace(/[A-Z]/g,c=>'-'+c.toLowerCase());return new Proxy({},{get:(_,k)=>typeof k==='string'&&el.hasAttribute(attr(k))?el.getAttribute(attr(k)):undefined,set:(_,k,v)=>{el.setAttribute(attr(k),v);return true},deleteProperty:(_,k)=>{el.removeAttribute(attr(k));return true},has:(_,k)=>el.hasAttribute(attr(k))});}
  get children(){return this.childNodes.filter(n=>n.nodeType===1)}
  get firstElementChild(){return this.children[0]||null}
  get textContent(){return this.childNodes.map(n=>n.textContent).join('')}
  set textContent(v){this.childNodes.forEach(n=>n.parentNode=null);this.childNodes=[];if(v!=='')adopt(this,new Text(String(v)));}
  get innerHTML(){return this.childNodes.map(serialize).join('')}
  set innerHTML(html){this.writes++;this.childNodes.forEach(n=>n.parentNode=null);this.childNodes=[];for(const node of parse(html,this.namespaceURI===SVG_NS&&this.localName!=='foreignObject'?SVG_NS:null))adopt(this,node);}
  get outerHTML(){return serialize(this)}
  set outerHTML(html){const parent=this.parentNode;if(!parent)return;const index=parent.childNodes.indexOf(this);detach(this);parse(html,this.namespaceURI).forEach((node,i)=>adopt(parent,node,index+i));}
  insertAdjacentHTML(where,html){const nodes=parse(html,this.namespaceURI);if(where==='beforeend')nodes.forEach(n=>adopt(this,n));else if(where==='afterbegin')nodes.reverse().forEach(n=>adopt(this,n,0));else if(where==='afterend')this.after(...nodes);else this.before(...nodes);}
  append(...nodes){nodes.forEach(n=>adopt(this,typeof n==='string'?new Text(n):n))}
  prepend(...nodes){nodes.reverse().forEach(n=>adopt(this,n,0))}
  after(...nodes){const p=this.parentNode;nodes.forEach((n,i)=>{detach(n);adopt(p,n,p.childNodes.indexOf(this)+1+i)})}
  before(...nodes){const p=this.parentNode;nodes.forEach(n=>{detach(n);adopt(p,n,p.childNodes.indexOf(this))})}
  remove(){detach(this)}
  replaceWith(node){const p=this.parentNode;if(!p)return;const i=p.childNodes.indexOf(this);detach(this);adopt(p,node,i)}
  contains(node){for(let n=node;n;n=n.parentNode)if(n===this)return true;return false}
  get isConnected(){let n=this;while(n.parentNode)n=n.parentNode;return n===document.documentElement}
  descendants(){const out=[];const walk=n=>{for(const c of n.children){out.push(c);walk(c)}};walk(this);return out}
  querySelectorAll(selector){const list=parseSelector(selector);return this.descendants().filter(el=>list.some(s=>matchComplex(el,s,this)))}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null}
  matches(selector){return parseSelector(selector).some(s=>matchComplex(this,s,null))}
  closest(selector){const list=parseSelector(selector);for(let n=this;n&&n.nodeType===1;n=n.parentNode)if(list.some(s=>matchComplex(n,s,null)))return n;return null}
  cloneNode(){return parse(serialize(this),this.namespaceURI)[0]}
  focus(){if(!this.disabled)document.activeElement=this}
  blur(){if(document.activeElement===this)document.activeElement=document.body}
  select(){}
  scrollIntoView(){}
  getBoundingClientRect(){return {left:0,top:0,right:0,bottom:0,width:0,height:0}}
  getClientRects(){return []}
  getScreenCTM(){return {inverse:()=>({})}}
  setPointerCapture(id){this.capture=id}
  getBBox(){return {x:0,y:0,width:0,height:0}}
  animate(_frames,options){const animation={cancel(){},play(){},updatePlaybackRate(){},onfinish:null};clock.setTimeout(()=>animation.onfinish?.(),options?.duration||0);return animation}
  addEventListener(type,fn,options){this.listeners.push({type,fn,capture:options===true||!!options?.capture})}
  removeEventListener(type,fn){this.listeners=this.listeners.filter(l=>l.type!==type||l.fn!==fn)}
 }
 function serialize(node){
  if(node.nodeType===3)return escText(node.data);
  const attrs=[...node.attrs].map(([k,v])=>v===''&&k!=='style'?' '+k:' '+k+'="'+escAttr(v)+'"').join('');
  if(node.namespaceURI!==SVG_NS&&VOID.has(node.localName))return '<'+node.localName+attrs+'>';
  return '<'+node.localName+attrs+'>'+node.childNodes.map(serialize).join('')+'</'+node.localName+'>';
 }
 function parse(html,ns){
  const root=new Element('root',ns),stack=[root],tag=/<!--[\s\S]*?-->|<\/([a-zA-Z][\w:-]*)\s*>|<([a-zA-Z][\w:-]*)((?:\s+[^\s=/>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>/g;
  let last=0,m;
  const text=t=>{if(t)adopt(stack.at(-1),new Text(decode(t)))};
  while((m=tag.exec(html))){
   text(html.slice(last,m.index));last=tag.lastIndex;
   if(m[0].startsWith('<!--'))continue;
   if(m[1]){for(let i=stack.length-1;i>0;i--)if(stack[i].localName.toLowerCase()===m[1].toLowerCase()){stack.length=i;break}continue}
   const parent=stack.at(-1),childNs=m[2].toLowerCase()==='svg'?SVG_NS:parent.namespaceURI===SVG_NS?SVG_NS:null;
   const el=new Element(m[2],childNs);
   for(const a of m[3].matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g))el.setAttribute(a[1],decode(a[2]??a[3]??a[4]??''));
   adopt(parent,el);
   if(!m[4]&&!(childNs!==SVG_NS&&VOID.has(el.localName)))stack.push(el);
  }
  text(html.slice(last));
  const nodes=[...root.childNodes];nodes.forEach(n=>n.parentNode=null);return nodes;
 }
 // Selectors: compounds of tag, #id, .class, [attr], [attr="v"], [attr*="v"], :not(), :disabled, :scope; ' ' and '>' combinators.
 const selectorCache=new Map();
 function split(text,separator){const out=[];let depth=0,quote=null,start=0;for(let i=0;i<text.length;i++){const c=text[i];if(quote){if(c===quote)quote=null;continue}if(c==='"'||c==="'")quote=c;else if(c==='['||c==='(')depth++;else if(c===']'||c===')')depth--;else if(!depth&&separator(c,i)){out.push(text.slice(start,i));start=i+1}}out.push(text.slice(start));return out}
 function parseCompound(text){
  const parts=[],re=/^(\*|[a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:([*^]?=)"([^"]*)")?\]|:not\(([^)]*\)?)\)|:(disabled|scope)/gy;let m;re.lastIndex=0;
  while(re.lastIndex<text.length&&(m=re.exec(text))){
   const g=m;
   if(g[1])parts.push(el=>g[1]==='*'||el.localName.toLowerCase()===g[1].toLowerCase());
   else if(g[2])parts.push(el=>el.id===g[2]);
   else if(g[3])parts.push(el=>el.classList.contains(g[3]));
   else if(g[4])parts.push(el=>el.hasAttribute(g[4])&&(g[5]===undefined||(g[5]==='*='?el.getAttribute(g[4]).includes(g[6]):g[5]==='^='?el.getAttribute(g[4]).startsWith(g[6]):el.getAttribute(g[4])===g[6])));
   else if(g[7]!==undefined){const inner=parseCompound(g[7]);parts.push((el,scope)=>!inner(el,scope))}
   else if(g[8]==='disabled')parts.push(el=>el.disabled);
   else if(g[8]==='scope')parts.push((el,scope)=>el===scope);
  }
  if(re.lastIndex!==text.length&&text.length)throw new Error('Unsupported selector: '+text);
  return (el,scope)=>parts.every(p=>p(el,scope));
 }
 function parseSelector(selector){
  if(selectorCache.has(selector))return selectorCache.get(selector);
  const list=split(selector,c=>c===',').map(s=>{
   const tokens=s.trim().replace(/\s*>\s*/g,' > ').split(/\s+/).filter(Boolean),steps=[];let combinator=' ';
   for(const t of tokens){if(t==='>'){combinator='>';continue}steps.push({match:parseCompound(t),combinator});combinator=' '}
   return steps;
  });
  selectorCache.set(selector,list);return list;
 }
 function matchComplex(el,steps,scope,index=steps.length-1){
  if(!steps[index].match(el,scope))return false;
  if(index===0)return true;
  const {combinator}=steps[index];
  if(combinator==='>'){const p=el.parentNode;return !!p&&p.nodeType===1&&matchComplex(p,steps,scope,index-1)}
  for(let p=el.parentNode;p&&p.nodeType===1;p=p.parentNode)if(matchComplex(p,steps,scope,index-1))return true;
  return false;
 }
 const html=new Element('html',null);
 const body=new Element('body',null);adopt(html,body);
 const markup=page.slice(page.indexOf('<body>')+6,page.indexOf('<script'));
 parse(markup,null).forEach(n=>adopt(body,n));
 const target={listeners:[],addEventListener:Element.prototype.addEventListener,removeEventListener:Element.prototype.removeEventListener};
 document=Object.assign(Object.create(target),{
  listeners:[],documentElement:html,body,activeElement:body,hidden:false,
  querySelector:s=>html.querySelector(s)||(html.matches(s)?html:null),querySelectorAll:s=>html.querySelectorAll(s),
  createElement:t=>new Element(t,null),createElementNS:(ns,t)=>new Element(t,ns),elementFromPoint:()=>null
 });
 html.parentNode=document;
 return {document,Element};
}

// ------------------------------------------------------------- harness --
function saved(){return {version:3,gridStepCm:5,room:'Office',desk:{width:160,height:85},monitorInches:32,devices:[
 ['govee-left',20,60,65],['govee-right',180,60,65],['hue-monitor',100,110,100],
 ['play-left',70,40,100],['play-center',100,40,50],['play-right',130,40,100]
].map(([id,x,height,scale])=>({id,scale,positionCm:{x,height},orientation:'horizontal'}))};}
function canonical(layout){
 // What the page sends: version 7 with the screen object and both configurations' zones and fade switches.
 const result=structuredClone(layout);result.version=7;
 // Older files carry the screen as monitorInches and the monitor row.
 result.screen??={positionCm:structuredClone(result.devices.find(row=>row.id==='hue-monitor').positionCm),inches:result.monitorInches};
 delete result.monitorInches;
 result.syncZone??=null;result.sceneZone??=null;result.syncFalloff??=true;result.sceneFalloff??=false;
 for(const row of result.devices){if(row.enabled!==false)delete row.enabled;if(row.syncRole!=='own')delete row.syncRole;row.orientation??='horizontal';row.sceneScale??=row.scale;}
 return result;
}
const discovered=(overrides={})=>({id:'device-'+'a'.repeat(32),provider:'govee',name:'Third <lamp>',model:'H6076',shape:'floor',zones:7,supported:true,online:true,selectable:true,ambiguous:false,layout_id:null,height_cm:136,width_cm:null,...overrides});
const liveOk=(overrides={})=>({version:1,connected:true,mode:'sync',sync:{state:'active',reason:'frames_received',blockers:[]},lamps:{},...overrides});
const lit=(colors,extra={})=>({online:true,on:true,brightness:80,colors,source:'sync-output',stale:false,included:true,...extra});
const json=(app,source)=>JSON.parse(app.run('JSON.stringify('+source+')'));
const response=(status,body)=>({status,ok:status>=200&&status<300,json:async()=>structuredClone(body)});
async function settle(){for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));}

async function harness({rooms=null,roomPost=null,roomsDown=()=>false,layout=saved(),revision='r1',storage=new Map(),live=liveOk(),discovery={connected:true,devices:[discovered()],providers:{govee:'ready',hue:'ready'}},onPost,layoutGet,identify,calibrate,lights,syncBox=()=>({state:'none'}),hueBridge=null,hidden=false,reducedMotion=false}={}){
 let now=0,timerId=0;const timers=new Map();
 const clock={setTimeout:(fn,delay=0)=>{timers.set(++timerId,{fn,at:now+Math.max(0,delay),delay});return timerId},clearTimeout:id=>timers.delete(id)};
 const {document}=createDom(clock);document.hidden=hidden;
 const FIRST='room-00000001';
 const server={layout:layout&&canonical(layout),revision,rooms:{},index:rooms||{rooms:[{id:FIRST,name:'Office'}],sync:FIRST,lights:{},assigned:{},placed:{}}};
 for(const r of server.index.rooms)if(r.id!==FIRST)server.rooms[r.id]={layout:r.layout?canonical(r.layout):null,revision:'q1'};
 server.index={...server.index,rooms:server.index.rooms.map(({id,name})=>({id,name}))};
 const roomsAnswer=()=>structuredClone(server.index);
 const posts=[],requests={layout:0,discovery:[],live:[],box:[],hue:[],rooms:[],roomPosts:[],otherLayouts:[]};
 const context={structuredClone,AbortController,console,
  setTimeout:clock.setTimeout,clearTimeout:clock.clearTimeout,
  requestAnimationFrame:fn=>clock.setTimeout(()=>fn(now),16),
  performance:{now:()=>now},document,innerWidth:1400,innerHeight:900,
  localStorage:{getItem:k=>storage.has(k)?storage.get(k):null,setItem:(k,v)=>storage.set(k,String(v)),removeItem:k=>storage.delete(k)},
  matchMedia:()=>({matches:reducedMotion}),
  MutationObserver:class{observe(){}disconnect(){}},
  DOMPoint:class{constructor(x,y){this.x=x;this.y=y}matrixTransform(){return this}},
  listeners:[],addEventListener(type,fn,options){this.listeners.push({type,fn,capture:options===true||!!options?.capture})},
  fetch:async(url,options={})=>{
   if(url==='/api/live'){requests.live.push(now);const value=typeof live==='function'?live(requests.live.length):live;if(value instanceof Error)throw value;return response(200,await value)}
   if(url==='/api/discovery'){requests.discovery.push(now);const value=typeof discovery==='function'?discovery(requests.discovery.length):discovery;if(value instanceof Error)throw value;return response(200,value)}
   if(url.startsWith('/api/calibration')){assert.ok(calibrate,'unexpected calibration');const result=await calibrate(url,JSON.parse(options.body));return response(result.status,result.body)}
   if(url.startsWith('/api/sync-box')){const body=options.body?JSON.parse(options.body):null;requests.box.push({url,body,at:now});const result=await syncBox(url,body);return result&&'status' in result?response(result.status,result.body):response(200,result)}
   if(url.startsWith('/api/hue-bridge')){const body=options.body?JSON.parse(options.body):null;requests.hue.push({url,body,at:now});if(!hueBridge)return response(404,{});const result=await hueBridge(url,body);return result&&'status' in result?response(result.status,result.body):response(200,result)}
   if(url.startsWith('/api/lights/')){assert.ok(lights,'unexpected light change');const result=await lights(url,JSON.parse(options.body));return response(result.status,result.body)}
   if(url==='/api/identify'){assert.ok(identify,'unexpected Blink');const result=await identify(JSON.parse(options.body));return response(result.status,result.body)}
   if(url==='/api/rooms'){requests.rooms.push(now);if(roomsDown(requests.rooms.length))return response(503,{});return response(200,roomsAnswer())}
   if(url.startsWith('/api/rooms/')){const body=JSON.parse(options.body);requests.roomPosts.push({url,body});const result=roomPost?await roomPost(url.slice(11),body,server):null;return result?response(result.status,result.body):response(200,roomsAnswer())}
   const [route,query]=url.split('?');assert.equal(route,'/api/layout');
   const asked=query?decodeURIComponent(query.replace(/^room=/,'')):FIRST;
   if(asked!==FIRST){
    if(options.method!=='POST'){requests.otherLayouts.push(asked);const value=server.rooms[asked];return value?response(200,{layout:value.layout,revision:value.revision}):response(404,{error:'unknown room'})}
    const body=JSON.parse(options.body);posts.push({body,ifMatch:options.headers['If-Match'],room:asked});const value=server.rooms[asked];
    if(options.headers['If-Match']!=='"'+(value.layout?value.revision:'')+'"')return response(409,{error:'layout changed'});
    value.layout=canonical(body);value.revision='r'+(posts.length+1);return response(200,{layout:value.layout,revision:value.revision});
   }
   if(options.method!=='POST'){requests.layout++;if(layoutGet)return layoutGet(server);return response(200,{layout:server.layout,revision:server.layout?server.revision:null})}
   const body=JSON.parse(options.body);posts.push({body,ifMatch:options.headers['If-Match']});
   if(onPost){const result=await onPost(body,server,options);if(result)return result}
   if(options.headers['If-Match']!=='"'+(server.layout?server.revision:'')+'"')return response(409,{error:'layout changed'});
   server.layout=canonical(body);server.revision='r'+(posts.length+1);
   return response(200,{layout:server.layout,revision:server.revision});
  }};
 context.window=context;
 vm.createContext(context);
 vm.runInContext(code,context,{filename:'app.js'});
 await settle();
 const $=s=>document.querySelector(s),stage=$('#stage');
 function dispatch(target,type,extra={}){
  const event={type,target,defaultPrevented:false,stopped:false,immediate:false,clientX:0,clientY:0,pointerId:1,buttons:0,key:'',metaKey:false,ctrlKey:false,shiftKey:false,...extra,
   preventDefault(){this.defaultPrevented=true},stopPropagation(){this.stopped=true},stopImmediatePropagation(){this.stopped=this.immediate=true}};
  const pathNodes=[];for(let n=target;n;n=n.parentNode)pathNodes.push(n);pathNodes.push(context);
  const run=(node,capture,atTarget)=>{for(const l of [...node.listeners]){if(l.type!==type||(!atTarget&&l.capture!==capture))continue;if(atTarget&&l.capture!==capture)continue;l.fn.call(node,event);if(event.immediate)return}};
  for(let i=pathNodes.length-1;i>0&&!event.stopped;i--)run(pathNodes[i],true,false);
  if(!event.stopped)run(target,true,true);
  if(!event.stopped)run(target,false,true);
  for(let i=1;i<pathNodes.length&&!event.stopped;i++)run(pathNodes[i],false,false);
  return event;
 }
 async function advance(ms){
  const end=now+ms;
  for(;;){const next=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at||a[0]-b[0])[0];if(!next)break;timers.delete(next[0]);now=next[1].at;next[1].fn();await settle()}
  now=end;await settle();
 }
 return {document,context,$,stage,posts,requests,server,storage,timers,dispatch,advance,settle,
  now:()=>now,
  run:source=>vm.runInContext(source,context),
  click:async el=>{assert.ok(el,'click target');dispatch(el,'pointerdown');dispatch(el,'pointerup');dispatch(el,'click');await settle()},
  press:async(el,key,extra={})=>{dispatch(el,'keydown',{key,...extra});await settle()},
  lamp:id=>stage.querySelector('[data-lamp="'+id+'"]'),
  async visible(visible){document.hidden=!visible;dispatch(document,'visibilitychange');await settle()}};
}

// ----------------------------------------------------------------- tests --
test('server layout is authoritative and maps layout rows to room coordinates',async()=>{
 const remote=saved();remote.devices[0].positionCm={x:50,height:80};remote.devices[2].positionCm={x:105,height:115};
 const app=await harness({layout:remote});await app.advance(1000);
 assert.deepEqual(json(app,'lamps.map(l=>[l.id,l.x,l.h])'),[['govee-left',-50,20],['govee-right',80,0],['hue-monitor',5,115],['play-left',-30,40],['play-center',0,40],['play-right',30,40]]);
 assert.equal(app.posts.length,0);
 assert.equal(app.$('#draft-status').dataset.state,'saved');
 assert.deepEqual(JSON.parse(app.run('JSON.stringify(pack())')),canonical(remote));
 assert.equal(app.$('#count').textContent,'6');
});

test('version 4 rows keep own light, removal, device bindings and a custom zone through a round trip',async()=>{
 const remote=saved();remote.version=4;remote.devices[3].syncRole='own';remote.devices[4].enabled=false;remote.devices[1].orientation='horizontal';
 remote.devices.push({id:discovered().id,name:'Third <lamp>',model:'H6076',shape:'floor',zones:7,scale:45,positionCm:{x:150,height:60},orientation:'horizontal'});
 remote.syncZone={version:1,points:[{u:-2,h:0},{u:2,h:0},{u:2,h:-65},{u:1,h:-15},{u:-1,h:-15},{u:-2,h:-65}]};
 const app=await harness({layout:remote});
 const packed=JSON.parse(app.run('JSON.stringify(pack())'));
 assert.deepEqual(packed,canonical(remote));
 assert.equal(app.run('lamps.find(l=>l.id==="'+discovered().id+'").unbound'),true);
 assert.equal(app.run('syncProjection(lamps[3]).length'),0,'own light takes no screen colour');
 assert.equal(app.run('syncProjection(lamps[5]).length'),1);
});

test('edits autosave once after 300 ms with If-Match and version 7, undo saves too',async()=>{
 const app=await harness();
 app.lamp('play-left').focus();await app.press(app.lamp('play-left'),'ArrowRight');
 assert.equal(app.$('#draft-status').dataset.state,'pending');
 await app.advance(299);assert.equal(app.posts.length,0);
 await app.advance(1);assert.equal(app.posts.length,1);
 assert.equal(app.posts[0].ifMatch,'"r1"');assert.equal(app.posts[0].body.version,7);
 assert.equal(app.posts[0].body.monitorInches,undefined);
 assert.deepEqual(app.posts[0].body.screen,{positionCm:{x:100,height:110},inches:32});
 assert.deepEqual(app.posts[0].body.devices[3].positionCm,{x:75,height:40});
 assert.equal(app.$('#draft-status').dataset.state,'saved');
 // Rapid edits coalesce into one request.
 for(const key of ['ArrowUp','ArrowUp','ArrowUp'])await app.press(app.lamp('play-left'),key);
 await app.advance(300);assert.equal(app.posts.length,2);assert.equal(app.posts[1].ifMatch,'"r2"');
 assert.deepEqual(app.posts[1].body.devices[3].positionCm,{x:75,height:55});
 await app.click(app.$('#undo'));await app.advance(300);
 assert.equal(app.posts.length,3);assert.deepEqual(app.posts[2].body.devices[3].positionCm,{x:75,height:50},'undo reverts one step');
});

test('a stale revision refetches and merges field by field',async()=>{
 const app=await harness();
 // Another tab changed the desk and a different lamp.
 app.server.layout={...app.server.layout,desk:{width:180,height:85}};app.server.layout.devices[1]={...app.server.layout.devices[1],scale:30};app.server.revision='r9';
 await app.press(app.lamp('play-left'),'ArrowRight');await app.advance(300);
 assert.equal(app.posts.length,2);
 assert.equal(app.posts[0].ifMatch,'"r1"');assert.equal(app.posts[1].ifMatch,'"r9"');
 const merged=app.posts[1].body;
 assert.equal(merged.desk.width,180);assert.equal(merged.devices[1].scale,30);assert.deepEqual(merged.devices[3].positionCm,{x:75,height:40});
 assert.equal(app.run('environment.width'),180);
});

test('offline edits stay in the browser draft and are retried',async()=>{
 let offline=true;
 const app=await harness({onPost:()=>{if(offline)throw new Error('offline')}});
 await app.press(app.lamp('play-right'),'ArrowLeft');await app.advance(300);
 assert.equal(app.posts.length,1);assert.equal(app.$('#draft-status').dataset.state,'error');
 assert.equal(app.storage.get('light-studio-grid-v3-pending'),'true');
 assert.deepEqual(JSON.parse(app.storage.get('light-studio-grid-v3')).devices[5].positionCm,{x:125,height:40});
 offline=false;await app.advance(3000);await app.advance(300);
 assert.equal(app.posts.length,2);assert.deepEqual(app.posts[1].body.devices[5].positionCm,{x:125,height:40});
 assert.equal(app.storage.has('light-studio-grid-v3-pending'),false);assert.equal(app.$('#draft-status').dataset.state,'saved');
});

test('the first load imports a browser draft when the server has none',async()=>{
 const draft=saved();draft.devices[0].positionCm={x:-10,height:60};
 const storage=new Map([['light-studio-grid-v3',JSON.stringify(draft)]]);
 const app=await harness({layout:null,storage});
 assert.equal(app.run('lamps[0].x'),-110);
 await app.advance(300);
 assert.equal(app.posts.length,1);assert.equal(app.posts[0].ifMatch,'""');
 assert.deepEqual(app.posts[0].body,canonical(draft));
 // With a server layout and no pending edits, the server wins over the draft.
 const again=await harness({layout:saved(),storage:new Map([['light-studio-grid-v3',JSON.stringify(draft)]])});
 await again.advance(1000);assert.equal(again.posts.length,0);assert.equal(again.run('lamps[0].x'),-80);
});

test('discovery is polled every 3 s only while the Lamps panel is open',async()=>{
 const app=await harness();
 await app.advance(10000);assert.equal(app.requests.discovery.length,0);
 await app.click(app.$('#list-toggle'));
 assert.equal(app.$('#lamp-panel').hidden,false);assert.equal(app.requests.discovery.length,1);
 await app.advance(2999);assert.equal(app.requests.discovery.length,1);
 await app.advance(1);assert.equal(app.requests.discovery.length,2);
 const panel=app.$('#lamp-panel');
 assert.match(panel.textContent,/Third <lamp>/);assert.ok(panel.querySelector('[data-place="'+discovered().id+'"]'));
 await app.press(app.document.body,'Escape');
 assert.equal(panel.hidden,true);
 await app.advance(10000);assert.equal(app.requests.discovery.length,2);
 await app.advance(1000);assert.equal(app.posts.length,0);
});

test('the Lamps panel pairs the Hue bridge after its button is pressed, without keys in the page',async()=>{
 let state={state:'none'},polls=0;
 const app=await harness({hueBridge:(url,body)=>{
  if(url==='/api/hue-bridge/find')return {bridges:[{ip:'192.168.10.5',name:'Hue <Bridge>'}]};
  if(url==='/api/hue-bridge/pair'){assert.deepEqual(body,{ip:'192.168.10.5'});state={state:'pairing',name:'Hue <Bridge>',ip:'192.168.10.5',left:30};return state}
  if(url==='/api/hue-bridge'&&state.state==='pairing'&&++polls===2)state={state:'paired',name:'Hue <Bridge>',ip:'192.168.10.5',result:'paired'};
  return state}});
 await app.click(app.$('#list-toggle'));
 const panel=app.$('#lamp-panel');
 assert.match(panel.textContent,/Hue bridgeNot connected/);
 await app.click(panel.querySelector('[data-hue="find"]'));
 assert.match(panel.textContent,/Found on this network:/);
 await app.click(panel.querySelector('[data-hue-ip="192.168.10.5"]'));
 assert.match(panel.textContent,/Press the button on the bridge/);assert.match(panel.textContent,/Hue <Bridge> · 192\.168\.10\.5/);
 const looked=app.requests.discovery.length;
 await app.advance(1000);await app.advance(1000);
 // A connected bridge needs nothing from the owner, so the panel leaves it out.
 assert.doesNotMatch(panel.textContent,/Hue bridge/);
 // The core restarts with the new bridge: discovery looks for its lamps again.
 assert.ok(app.requests.discovery.length>looked);
});

test('a connected Hue bridge is left out of the Lamps panel',async()=>{
 for(const state of [{state:'configured'},{state:'paired',name:'Hue Bridge',ip:'192.168.10.5'}]){
  const app=await harness({hueBridge:()=>state});
  await app.click(app.$('#list-toggle'));
  const panel=app.$('#lamp-panel');
  assert.doesNotMatch(panel.textContent,/Hue bridge/i);
  assert.ok(!panel.querySelector('[data-hue]'));
 }
});

test('a Hue bridge address is typed when none is found, and errors stay readable',async()=>{
 const app=await harness({hueBridge:(url,body)=>{
  if(url==='/api/hue-bridge/find')return {bridges:[]};
  if(url==='/api/hue-bridge/pair')return body.ip==='192.168.10.9'?{status:409,body:{reason:'other_bridge'}}:{status:503,body:{}};
  return {state:'none',...(url==='/api/hue-bridge'?{}:{})}}});
 await app.click(app.$('#list-toggle'));
 const panel=app.$('#lamp-panel');
 await app.click(panel.querySelector('[data-hue="find"]'));
 assert.match(panel.textContent,/None found\. Enter its IP address:/);
 const input=panel.querySelector('[data-hue-input]');
 input.value='192.168.10.7';app.dispatch(input,'input');
 await app.press(input,'Enter');
 assert.match(panel.textContent,/No Hue bridge answers at this address\./);
 const again=panel.querySelector('[data-hue-input]');
 again.value='192.168.10.9';app.dispatch(again,'input');
 await app.click(panel.querySelector('[data-hue="address"]'));
 assert.match(panel.textContent,/Another Hue bridge has connected lamps\. Remove them first\./);
 assert.deepEqual(app.requests.hue.filter(r=>r.url==='/api/hue-bridge/pair').map(r=>r.body.ip),['192.168.10.7','192.168.10.9']);
 await app.click(panel.querySelector('[data-hue="close"]'));
 assert.ok(panel.querySelector('[data-hue="find"]'));
});

test('Found shows unavailable lamps after the lamps that can be placed, with their reason, and hides placed ones',async()=>{
 const id=n=>'device-'+String(n).repeat(32);
 const devices=[discovered({id:id(1),name:'Gone',online:false}),discovered({id:id(2),name:'Odd',supported:false,shape:null,zones:null}),
  discovered({id:id(3),name:'Twin',ambiguous:true}),discovered({id:id(4),name:'Role',layout_id:'govee-left'}),discovered({id:id(5),name:'Placed'})];
 const remote=saved();remote.devices.push({id:id(5),name:'Placed',model:'H6076',shape:'floor',zones:7,scale:80,positionCm:{x:200,height:60}});
 const app=await harness({layout:remote,discovery:{connected:true,devices,providers:{}}});
 await app.click(app.$('#list-toggle'));
 const rows=[...app.$('#lamp-panel').querySelectorAll('.lp-row.found')].map(r=>r.textContent);
 assert.deepEqual(rows,['GoneH6076 · 7 zonesOffline','TwinH6076 · 7 zonesAmbiguous','OddH6076Unsupported'],'closest to usable first');
 assert.equal(app.$('#lamp-panel').querySelectorAll('[data-place]').length,0);
 const down=await harness({discovery:new Error('down')});await down.click(down.$('#list-toggle'));
 assert.match(down.$('#lamp-panel').textContent,/Lamp search is unavailable/);
});

test('valid saved positions far from the desk load unchanged and widen the room',async()=>{
 const remote=saved();remote.devices[0].positionCm={x:-200,height:300};remote.devices[3].positionCm={x:400,height:300};
 const app=await harness({layout:remote});await app.advance(1000);
 assert.deepEqual(json(app,"lamps.filter(l=>['govee-left','play-left'].includes(l.id)).map(l=>[l.x,l.h])"),[[-300,240],[300,300]]);
 const [left,,width]=app.stage.getAttribute('viewBox').split(' ').map(Number);
 assert.ok(left<=500-300*3.3&&left+width>=500+300*3.3,'both lamps are inside the room view');
 await app.click(app.$('#sync-tool'));await app.click(app.$('#sync-tool'));await app.advance(300);
 assert.equal(app.posts.length,0,'loading and viewing never rewrite positions');
 app.lamp('play-center').focus();await app.press(app.lamp('play-center'),'ArrowRight');await app.advance(300);
 const body=app.posts.at(-1).body;
 assert.deepEqual(body.devices[4].positionCm,{x:105,height:40});
 assert.deepEqual(body.devices[0].positionCm,{x:-200,height:300},'another edit keeps the far lamp');
 assert.deepEqual(body.devices[3].positionCm,{x:400,height:300});
});

test('placing a found lamp adds a configuration-only device row with its canonical fields',async()=>{
 const app=await harness({live:liveOk({lamps:{'govee-left':lit([[255,0,0]])}})});
 await app.click(app.$('#list-toggle'));
 await app.click(app.$('#lamp-panel').querySelector('[data-place]'));
 assert.equal(app.$('#lamp-panel').hidden,true);assert.ok(app.$('#app').classList.contains('placing'));
 assert.match(app.$('#placement-hint').textContent,/Click in the room to place Third <lamp>/);
 app.dispatch(app.stage,'pointerdown',{clientX:500+100*3.3,clientY:600});await app.settle();
 assert.ok(!app.$('#app').classList.contains('placing'));
 await app.advance(300);
 const row=app.posts.at(-1).body.devices.at(-1);
 assert.deepEqual(row,{id:discovered().id,scale:80,sceneScale:80,positionCm:{x:200,height:60},orientation:'horizontal',shape:'floor',name:'Third <lamp>',model:'H6076',zones:7});
 const placed=app.lamp(discovered().id);
 assert.equal(placed.querySelector('[data-sync-power]').getAttribute('opacity'),'0','not connected rows stay neutral');
 await app.click(app.$('#list-toggle'));
 assert.ok(app.$('#lamp-panel').querySelector('[data-list-lamp="'+discovered().id+'"] [data-connect]'),'a lamp that is not connected offers Connect');
 assert.equal(app.$('#lamp-panel').querySelector('[data-place]'),null);
});

test('removing asks inline, keeps the place, and Found returns the lamp',async()=>{
 const app=await harness();
 await app.click(app.$('#list-toggle'));
 const panel=app.$('#lamp-panel');
 await app.click(panel.querySelector('[data-remove="play-center"]'));
 assert.match(panel.querySelector('.lp-row.confirm').textContent,/Remove Hue Play Center\?/);
 await app.click(panel.querySelector('[data-remove-cancel]'));
 assert.equal(panel.querySelector('.lp-row.confirm'),null);await app.advance(300);assert.equal(app.posts.length,0);
 await app.click(panel.querySelector('[data-remove="play-center"]'));
 await app.click(panel.querySelector('[data-remove-ok="play-center"]'));
 await app.advance(300);
 assert.deepEqual(app.posts.at(-1).body.devices[4],{id:'play-center',scale:50,sceneScale:50,positionCm:{x:100,height:40},orientation:'horizontal',enabled:false});
 assert.equal(app.lamp('play-center'),null);assert.equal(app.$('#count').textContent,'5');
 await app.click(app.$('#list-toggle'));
 await app.click(app.$('#lamp-panel').querySelector('[data-restore="play-center"]'));
 await app.advance(300);
 assert.equal(app.posts.at(-1).body.devices[4].enabled,undefined);assert.ok(app.lamp('play-center'));
});

test('removing a placed found lamp deletes its row so the slot is free again',async()=>{
 const remote=saved();remote.devices.push({id:discovered().id,scale:80,positionCm:{x:200,height:60},orientation:'horizontal',shape:'floor',name:'Third <lamp>',model:'H6076',zones:7});
 const app=await harness({layout:remote,discovery:{connected:true,devices:[discovered({layout_id:null})],providers:{govee:'ready',hue:'ready'}}});
 await app.click(app.$('#list-toggle'));
 const panel=app.$('#lamp-panel');
 await app.click(panel.querySelector('[data-remove="'+discovered().id+'"]'));
 await app.click(panel.querySelector('[data-remove-ok="'+discovered().id+'"]'));
 await app.advance(300);
 assert.equal(app.posts.at(-1).body.devices.length,6,'the optional row is gone');
 assert.equal(app.lamp(discovered().id),null);
 await app.click(app.$('#list-toggle'));
 assert.ok(app.$('#lamp-panel').querySelector('[data-place="'+discovered().id+'"]'),'the lamp is back in Found');
 assert.equal(app.$('#lamp-panel').querySelector('[data-restore="'+discovered().id+'"]'),null);
});

test('the lamp card removes with a confirmation and sets Own light during Sync',async()=>{
 const app=await harness();
 app.dispatch(app.lamp('play-right').querySelector('.hit'),'pointerdown',{clientX:599,clientY:608});app.dispatch(app.stage,'pointerup');await app.settle();
 assert.equal(app.run('selected'),'play-right');
 const more=app.stage.querySelector('[data-chip-action="more"]');assert.ok(more);
 app.dispatch(more,'pointerdown');await app.settle();
 const menu=app.$('#object-menu');assert.equal(menu.hidden,false);
 assert.equal(menu.querySelector('.card-name').readOnly,false,'desk lamps can be renamed');
 assert.equal(menu.querySelector('[data-menu="blink"]').disabled,false,'a connected lamp can blink');
 assert.ok(menu.querySelector('[data-menu="role-off"]'),'role lamps can be switched off during Sync');
 assert.equal(menu.querySelectorAll('.seg button').length,3,'Sync, Own light and Off');
 await app.click(menu.querySelector('[data-menu="role-own"]'));
 await app.advance(300);
 assert.equal(app.posts.at(-1).body.devices[5].syncRole,'own');
 assert.equal(app.$('#object-menu').querySelector('[data-menu="role-own"]').getAttribute('aria-pressed'),'true');
 await app.click(app.$('#object-menu').querySelector('[data-menu="role-sync"]'));await app.advance(300);
 assert.equal(app.posts.at(-1).body.devices[5].syncRole,undefined);
 await app.click(app.$('#object-menu').querySelector('[data-menu="remove"]'));
 assert.match(app.$('#object-menu').querySelector('.confirm').textContent,/Remove Hue Play Right\?/);
 await app.click(app.$('#object-menu').querySelector('[data-menu="remove-ok"]'));await app.advance(300);
 assert.equal(app.posts.at(-1).body.devices[5].enabled,false);
});

test('Own light is shown in Sync view and device rows cannot change their Sync role',async()=>{
 const remote=saved();remote.devices[3].syncRole='own';
 remote.devices.push({id:discovered().id,name:'Third <lamp>',model:'H6076',shape:'floor',zones:7,scale:45,positionCm:{x:150,height:60}});
 const app=await harness({layout:remote});
 await app.click(app.$('#sync-tool'));assert.equal(app.run('mode'),'sync');
 app.run('select("play-left")');await app.settle();
 assert.match(app.stage.querySelector('.obj-chip').textContent,/own light/);
 app.run('select("'+discovered().id+'")');await app.settle();
 app.dispatch(app.stage.querySelector('[data-chip-action="more"]'),'pointerdown');await app.settle();
 assert.ok([...app.$('#object-menu').querySelectorAll('.seg button')].every(b=>b.disabled));
 assert.match(app.stage.querySelector('.obj-chip').textContent,/not connected/);
 await app.advance(1000);assert.equal(app.posts.length,0);
});

test('live polling: 50 ms after each answer, 1000 ms deadline, 1500 ms freshness, paused while hidden',async()=>{
 let hang=false;
 const app=await harness({live:()=>hang?new Promise(()=>{}):liveOk()});
 assert.deepEqual(app.requests.live,[0]);
 await app.advance(49);assert.equal(app.requests.live.length,1);
 await app.advance(1);assert.deepEqual(app.requests.live,[0,50]);
 await app.advance(100);assert.deepEqual(app.requests.live,[0,50,100,150]);
 hang=true;await app.advance(50);assert.equal(app.requests.live.at(-1),200);
 assert.equal(app.$('#sync-status span').textContent,'Syncing');
 await app.advance(999);assert.equal(app.requests.live.length,5);
 await app.advance(1);assert.equal(app.$('#sync-status span').textContent,'Sync status unknown');
 await app.advance(50);assert.equal(app.requests.live.at(-1),1250);
 await app.visible(false);const count=app.requests.live.length;
 await app.advance(5000);assert.equal(app.requests.live.length,count);
 hang=false;await app.visible(true);assert.equal(app.requests.live.length,count+1);
 assert.equal(app.$('#sync-status span').textContent,'Syncing');
 const hiddenStart=await harness({hidden:true});await hiddenStart.advance(3000);assert.equal(hiddenStart.requests.live.length,0);
});

test('a fresh answer expires after 1500 ms even when the next poll is delayed',async()=>{
 const app=await harness();
 assert.equal(app.$('#sync-status span').textContent,'Syncing');
 // A throttled background timer: the next poll does not run, the freshness guard still does.
 for(const [id,timer] of app.timers)if(timer.delay===50)app.timers.delete(id);
 await app.advance(1499);assert.equal(app.$('#sync-status span').textContent,'Syncing');
 await app.advance(1);assert.equal(app.$('#sync-status span').textContent,'Sync status unknown');
 assert.equal(app.requests.live.length,1);
});

test('Sync states map to short labels and unknown reasons stay readable',async()=>{
 const cases=[
  [{state:'active',reason:'frames_received'},'Syncing','ok'],
  [{state:'waiting',reason:'grace_elapsed'},'Waiting for frames','wait'],
  [{state:'waiting',reason:'source_timeout'},'Waiting for frames','wait'],
  [{state:'waiting',reason:'dtls_closed'},'Waiting for frames','wait'],
  [{state:'waiting',reason:'native_unavailable'},'Hue not responding','wait'],
  [{state:'foreign',reason:'unknown'},'Hue busy','bad'],
  [{state:'off',reason:'explicit_stop'},'Not syncing','idle'],
  [{state:'unknown',reason:'source_reconnected'},'Sync status unknown','unknown'],
  [{state:'brand-new-state',reason:{}},'Sync status unknown','unknown'],
  [undefined,'Sync status unknown','unknown']
 ];
 for(const [sync,label,level] of cases){
  const app=await harness({live:liveOk({sync})});
  assert.equal(app.$('#sync-status span').textContent,label,JSON.stringify(sync));
  assert.equal(app.$('#sync-status').dataset.level,level);
 }
 const paused=await harness({live:{version:1,connected:false,mode:'paused',sync:{state:'paused'},lamps:{}}});
 assert.equal(paused.$('#sync-status span').textContent,'Not syncing');
 const disconnected=await harness({live:{version:1,connected:false,mode:'unknown',sync:{state:'unknown'},lamps:{}}});
 assert.equal(disconnected.$('#sync-status span').textContent,'Sync status unknown');
});

test('blockers name the lamp, badge it unlit, and the pill selects it',async()=>{
 const app=await harness({live:liveOk({sync:{state:'active',reason:'frames_received',blockers:[{role:'govee-left',code:'owner_off'}]},lamps:{'govee-left':lit(Array(7).fill([255,0,0]))}})});
 assert.equal(app.$('#sync-status span').textContent,'Govee Left off in HomeKit');
 const badge=app.lamp('govee-left').querySelector('[data-badge]');
 assert.equal(badge.getAttribute('visibility'),'visible');assert.equal(badge.dataset.tooltip,'Off in HomeKit');
 assert.equal(app.lamp('govee-left').querySelector('[data-sync-power]').getAttribute('opacity'),'0');
 await app.click(app.$('#sync-status'));assert.equal(app.run('selected'),'govee-left');
 const many=await harness({live:liveOk({sync:{state:'active',blockers:[{role:'play-left',code:'native_stale'},{role:'hue-monitor',code:'color_fenced'},{role:'nobody',code:'x'},{role:'govee-right',code:'future_code'}]}})});
 assert.equal(many.$('#sync-status span').textContent,'3 lamps not lit');
 assert.equal(many.lamp('govee-right').querySelector('[data-badge]').dataset.tooltip,'Not ready');
});

test('live colours repaint existing nodes without rebuilding the room or saving',async()=>{
 let answer=liveOk({lamps:{'play-left':lit([[255,0,0]]),'govee-left':lit([[0,0,255],[0,0,255],[0,0,255],[0,255,0],[0,255,0],[0,255,0],[255,255,255]]),
  'hue-monitor':lit(Array.from({length:9},(_,i)=>[i*20,0,0])),'play-center':lit([[9,9,9]],{on:false}),'play-right':lit([[9,9,9]],{stale:true}),'govee-right':lit(Array(7).fill([1,2,3]),{included:false})}});
 const unverified=liveOk({lamps:{'play-left':lit([[255,0,0]],{source:'unknown'})}});
 const app=await harness({live:()=>answer});
 await app.click(app.$('#sync-tool'));
 const writes=app.stage.writes,node=app.lamp('play-left');
 assert.equal(node.querySelector('[data-beam]').style.getPropertyValue('--beam'),'#ff0000');
 assert.equal(node.querySelector('[data-sync-power]').getAttribute('opacity'),'1');
 const floorStops=[...app.lamp('govee-left').querySelectorAll('[id="lens-govee-left"] stop')].map(s=>s.getAttribute('stop-color'));
 assert.equal(floorStops[0],'#ffffff');assert.equal(floorStops.at(-1),'#0000ff','zones run bottom to top');
 const washStops=[...app.lamp('govee-left').querySelectorAll('[id="wash-govee-left"] stop')].map(s=>s.getAttribute('stop-color'));
 assert.deepEqual(washStops,['#0000ff','#0000ff','#0000ff','#00ff00','#00ff00','#00ff00','#ffffff'],'the wall next to each zone takes its colour, foot first');
 for(const id of ['play-center','play-right','govee-right'])assert.equal(app.lamp(id).querySelector('[data-sync-power]').getAttribute('opacity'),'0',id);
 assert.equal(app.stage.querySelector('[data-screen-demo]').getAttribute('opacity'),'0','the live picture carries no demo label');
 answer=liveOk({lamps:{'play-left':lit([[0,128,0]])}});
 await app.advance(200);
 assert.equal(node.querySelector('[data-beam]').style.getPropertyValue('--beam'),'#00ff00');
 assert.equal(app.stage.querySelector('[data-wash-for="play-left"]').style.getPropertyValue('--beam'),'#00ff00');
 assert.equal(app.lamp('govee-left').querySelector('[data-sync-power]').getAttribute('opacity'),'0');
 assert.equal(app.stage.querySelector('[data-screen-demo]').getAttribute('opacity'),'1','without strip colours the screen shows the labelled demo picture');
 assert.equal(app.stage.writes,writes,'the stage markup was not rebuilt');
 assert.equal(app.lamp('play-left'),node);
 answer=liveOk({sync:{state:'off'},lamps:{'play-left':lit([[0,128,0]],{source:'scene-output'})}});
 await app.advance(200);
 assert.equal(app.$('#sync-status span').textContent,'Not syncing');
 assert.equal(node.querySelector('[data-sync-power]').getAttribute('opacity'),app.run('opacityText(lampLevel(demoColors(lamps.find(l=>l.id==="play-left"))))'),'the Sync view previews the demo picture instead of the scene');
 assert.equal(app.stage.writes,writes);
 answer=unverified;await app.advance(200);
 assert.equal(node.querySelector('[data-sync-power]').getAttribute('opacity'),'0','colours without a verified output source are not light');
 await app.advance(2000);assert.equal(app.posts.length,0);
});

test('a selected lamp shows rays in the hue of its emitted zones',async()=>{
 const hues=['#ff0000','#00ff00','#0000ff','#ffff00','#00ffff','#ff00ff','#ffffff'];
 const app=await harness({live:liveOk({lamps:{'govee-left':lit([[10,0,0],[0,20,0],[0,0,30],[40,40,0],[0,50,50],[60,0,60],[70,70,70]]),'hue-monitor':lit(Array(9).fill([9,9,9]))}})});
 await app.click(app.$('#sync-tool'));
 app.run('select("govee-left")');await app.settle();
 const rays=[...app.stage.querySelectorAll('#sync-links .ray')];
 assert.ok(rays.length>0&&rays.length<7,'zones below the Sync zone have no ray');
 for(const ray of rays){const i=+ray.dataset.k.split(':r')[1];assert.equal(ray.getAttribute('stroke'),hues[i])}
 app.run('select("monitor")');await app.settle();
 assert.equal(app.stage.querySelectorAll('#sync-links .ray').length,rays.length,'the monitor shows every lit lamp');
});

test('while Sync runs the view without Sync shows a demo scene instead of the Sync colours',async()=>{
 const remote=saved();remote.devices.find(r=>r.id==='play-left').sceneScale=50;
 const app=await harness({layout:remote,live:liveOk({lamps:{'play-left':lit([[255,0,0]]),'play-right':lit([[0,0,255]],{source:'scene-output'}),'play-center':lit([[0,255,0]],{on:false})}})});
 const beam=id=>app.lamp(id).querySelector('[data-beam]').style.getPropertyValue('--beam');
 assert.match(beam('play-left'),/^#ffd[56]a[9a]$/,'a Sync lamp shows the demo warm white, not its Sync red');
 assert.equal(app.lamp('play-left').querySelector('[data-sync-power]').getAttribute('opacity'),String(+Math.sqrt(128/255).toFixed(3)),'at its correction without Sync');
 assert.equal(beam('play-right'),'#0000ff','own light stays as it is');
 assert.equal(app.lamp('play-center').querySelector('[data-sync-power]').getAttribute('opacity'),'0','a lamp that is off stays dark');
 assert.equal(app.stage.querySelector('[data-scene-demo]').getAttribute('opacity'),'1','the screen says Demo');
 await app.click(app.$('#sync-tool'));
 assert.equal(beam('play-left'),'#ff0000','the Sync view shows the real Sync colours');
 await app.click(app.$('#sync-tool'));
 const off=await harness({live:liveOk({sync:{state:'off',reason:'explicit_stop'},lamps:{'play-right':lit([[0,0,255]],{source:'scene-output'})}})});
 assert.equal(off.stage.querySelector('[data-scene-demo]').getAttribute('opacity'),'0','without Sync the scene is real');
});

test('without running Sync the Sync view previews the demo picture on the screen and every Sync lamp',async()=>{
 const app=await harness({live:liveOk({sync:{state:'off'},lamps:{'govee-left':lit(Array(7).fill([0,255,0]),{source:'scene-output'}),'play-left':lit([[0,255,0]],{source:'scene-output',on:false})}})});
 assert.equal(app.lamp('govee-left').querySelector('[data-beam]').style.getPropertyValue('--beam'),'#00ff00','the view without Sync shows the scene');
 await app.click(app.$('#sync-tool'));
 assert.equal(app.stage.querySelector('#screen-demo-image').getAttribute('opacity'),'1');
 assert.equal(app.stage.querySelector('#screen-image').getAttribute('opacity'),'0');
 assert.equal(app.stage.querySelectorAll('#screen-demo-image path').length,90,'a colour wheel of wedges');
 assert.equal(app.stage.querySelector('[data-screen-demo]').textContent,'Demo');
 assert.equal(app.stage.querySelector('[data-screen-demo]').getAttribute('opacity'),'1');
 const left=json(app,'lamps.find(l=>l.id==="govee-left")');
 assert.deepEqual(json(app,'liveColors(lamps.find(l=>l.id==="govee-left"))'),json(app,'demoColors(lamps.find(l=>l.id==="govee-left"))'));
 assert.notEqual(app.lamp('govee-left').querySelector('[data-beam]').style.getPropertyValue('--beam'),'#00ff00','the lamp no longer shows its scene colour');
 assert.equal(app.run('demoColors(lamps.find(l=>l.id==="govee-left"))').length,left.zones);
 // Right is red, top yellow-green, left cyan, bottom violet: every edge point has its own hue.
 assert.equal(app.run('demoHue(1,.5)'),0);assert.equal(app.run('demoHue(.5,1)'),90);assert.equal(app.run('demoHue(0,.5)'),180);assert.equal(app.run('demoHue(.5,0)'),270);
 assert.equal(app.run('demoColor(1,.5)'),'#d34545');
 const points=json(app,'syncProjection(lamps.find(l=>l.id==="govee-left")).filter(r=>r.point).map(r=>r.point)');
 const hues=points.map(p=>Math.round(app.run('demoHue('+p.join()+')')));
 assert.ok(new Set(points.map(String)).size>1);
 assert.equal(new Set(hues).size,new Set(points.map(String)).size,'zones that sample different screen points get different hues');
 assert.equal(json(app,'demoColors(lamps.find(l=>l.id==="govee-left"))').at(-1).join(),json(app,'(()=>{const l=lamps.find(l=>l.id==="govee-left"),r=syncProjection(l).at(-1);return [1,3,5].map(k=>Math.round(parseInt(demoColor(...r.point).slice(k,k+2),16)*r.gain*l.scale/100))})()').join(),'a zone takes the picture colour where it samples, dimmed by gain and correction');
 app.run('select("govee-left")');await app.settle();
 const rays=[...app.stage.querySelectorAll('#sync-links .ray')];
 assert.ok(rays.length>0);
 for(const ray of rays){const i=+ray.dataset.k.split(':r')[1];assert.equal(ray.getAttribute('stroke'),app.run('zoneColor(lamps.find(l=>l.id==="govee-left"),'+i+')'))}
 const correction=app.run('lampLevel(demoColors(lamps.find(l=>l.id==="govee-left")))');
 app.run('lamps.find(l=>l.id==="govee-left").scale=40');
 assert.ok(app.run('lampLevel(demoColors(lamps.find(l=>l.id==="govee-left")))')<correction,'the correction dims the preview');
});

test('the monitor strip glows left to right in its side and top colours',async()=>{
 const colors=[[255,0,0],[255,0,0],[255,0,0],[0,255,0],[0,0,255],[255,255,0],[0,0,128],[0,0,128],[0,0,128]];
 const app=await harness({live:liveOk({lamps:{'hue-monitor':lit(colors,{source:'scene-output'})}})});
 const stops=()=>[...app.lamp('hue-monitor').querySelectorAll('stop')].map(s=>s.getAttribute('stop-color')+'/'+s.getAttribute('stop-opacity'));
 assert.deepEqual(stops(),['#ff0000/1','#00ff00/1','#0000ff/1','#ffff00/1','#0000ff/0.708']);
 assert.equal(app.lamp('hue-monitor').querySelector('linearGradient').getAttribute('gradientUnits'),'userSpaceOnUse');
 assert.match(app.lamp('hue-monitor').querySelector('.render-emission').getAttribute('style'),/url\(#lens-hue-monitor\)/);
});

test('a lamp wash takes its hue from the emitted colours weighted by brightness',async()=>{
 const app=await harness({live:liveOk({lamps:{'govee-left':lit([[255,0,0],[0,0,1],[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0]],{source:'scene-output'})}})});
 assert.equal(app.lamp('govee-left').querySelector('[data-beam]').style.getPropertyValue('--beam'),'#ff0001','a nearly-off blue zone does not turn the red lamp purple');
});

test('dim zones glow faintly in their hue and black zones emit no light',async()=>{
 const app=await harness({live:liveOk({lamps:{'govee-left':lit([[0,0,0],[0,0,0],[0,0,0],[64,32,0],[255,128,0],[0,0,0],[0,0,0]],{source:'scene-output'}),'play-left':lit([[0,0,64]],{source:'scene-output'})}})});
 const floor=app.lamp('govee-left'),stops=[...floor.querySelectorAll('stop')];
 // Stops run top to bottom in pairs; zone 4 (index 3 from the bottom) is dim, zone 5 bright.
 const zone=i=>stops[2*(6-i)];
 assert.equal(zone(4).getAttribute('stop-color'),'#ff8000');assert.equal(zone(4).getAttribute('stop-opacity'),'1');
 assert.equal(zone(3).getAttribute('stop-color'),'#ff8000','a dim zone keeps its hue');
 assert.equal(zone(3).getAttribute('stop-opacity'),'0.501','and glows at its brightness');
 assert.equal(zone(0).getAttribute('stop-opacity'),'0','a black zone emits nothing');
 assert.equal(floor.querySelector('[data-sync-power]').getAttribute('opacity'),'1');
 const play=app.lamp('play-left');
 assert.equal(play.querySelector('[data-beam]').style.getPropertyValue('--beam'),'#0000ff');
 assert.equal(play.querySelector('[data-sync-power]').getAttribute('opacity'),'0.501','a dim Play glows faintly');
});

test('the room view is a local preference and never part of the layout',async()=>{
 const storage=new Map();
 const app=await harness({storage});
 await app.click(app.$('#sync-tool'));await app.click(app.$('#scene-toggle'));
 assert.equal(storage.get('light-studio-view-v1'),'sync');assert.equal(storage.get('light-studio-dark-scene-v1'),'true');
 assert.ok(app.$('#app').classList.contains('dark-scene'));
 await app.advance(2000);assert.equal(app.posts.length,0);
 const again=await harness({storage});assert.equal(again.run('mode'),'sync');
});

test('each view edits its own correction, fade zone and fade switch',async()=>{
 const app=await harness();
 const left=()=>json(app,'lamps.find(l=>l.id==="govee-left")');
 assert.equal(app.run('view()'),'scene');
 app.run('select("govee-left")');await openCard(app);
 await app.click(app.$('#object-menu [data-menu="calibrate"]'));
 const bri=app.$('#object-menu [data-cal="correction"]');bri.value='40';app.dispatch(bri,'input');
 await app.click(app.$('#object-menu [data-menu="cal-save"]'));await app.advance(300);
 let body=app.posts.at(-1).body,row=body.devices.find(r=>r.id==='govee-left');
 assert.equal(row.sceneScale,40,'the room view edits the correction without Sync');assert.equal(row.scale,65,'the Sync correction stays');
 assert.equal(body.syncFalloff,true);assert.equal(body.sceneFalloff,false,'the fade without Sync is off by default');
 app.run('select("zone")');
 assert.ok(app.stage.querySelector('#sync-visibility.fade-off'),'a switched-off zone stays as a faint outline in the room view');
 assert.equal(app.stage.querySelectorAll('[data-handle^="zp-"]').length,0,'no corners while the fade is off');
 app.run('toggleFade()');await app.advance(300);
 assert.equal(app.posts.at(-1).body.sceneFalloff,true);
 app.run('drag={handle:"zp-3",before:snapshot(),moved:false};dragHandle("zp-3",{x:500+90*3.3,y:740-70*3.3});');
 app.dispatch(app.stage,'pointerup');await app.advance(300);
 body=app.posts.at(-1).body;
 assert.equal(body.sceneZone.points[3].u,90/80);assert.equal(body.syncZone,null,'the Sync zone stays automatic');
 await app.click(app.$('#sync-tool'));
 assert.equal(app.run('corr(lamps.find(l=>l.id==="govee-left"))'),65,'the Sync view shows the Sync correction');
 const gains=()=>json(app,'syncProjection(lamps.find(l=>l.id==="govee-left")).map(r=>r.gain)');
 assert.ok(gains().some(g=>g<1),'the Sync fade dims lower zones');
 app.run('select("zone")');app.run('toggleFade()');await app.advance(300);
 assert.equal(app.posts.at(-1).body.syncFalloff,false);
 assert.deepEqual(gains(),Array(7).fill(1),'without the fade every zone keeps full brightness');
 assert.equal(left().sceneScale,40);
});

async function openCard(app){app.dispatch(app.stage.querySelector('[data-chip-action="more"]'),'pointerdown');await app.settle()}
const SYNC_IDLE={sync:{state:'idle',reason:'no_source',blockers:[]}};

test('a lamp chip is one button that opens the card, and the scene has no correction rail',async()=>{
 const app=await harness();
 app.run('select("govee-left")');await app.settle();
 const chip=()=>app.stage.querySelector('.chip-main');
 assert.equal(chip().getAttribute('aria-expanded'),'false');
 assert.equal(app.stage.querySelector('[data-handle="scale"]'),null,'no correction rail');
 assert.equal(app.stage.querySelector('[data-edit="corr"]'),null,'no correction label');
 app.dispatch(chip(),'pointerdown');await app.settle();
 const menu=app.$('#object-menu');assert.equal(menu.hidden,false);
 assert.ok(menu.classList.contains('opening'),'the card grows in');
 assert.equal(chip().getAttribute('aria-expanded'),'true');
 assert.equal(menu.querySelector('[data-menu="reset"]'),null,'Reset correction moved to the Calibrate page');
 assert.deepEqual([...menu.querySelectorAll('.card-row')].map(b=>b.dataset.menu),['blink','calibrate','remove']);
 app.dispatch(chip(),'pointerdown');await app.settle();
 assert.ok(menu.classList.contains('closing'),'the card fades out first');
 await app.advance(200);assert.equal(menu.hidden,true,'the chip closes the card too');
 app.dispatch(app.lamp('govee-left'),'dblclick');await app.advance(20);
 assert.equal(menu.hidden,false,'a double click on the lamp opens its card');
});

test('calibrating a Govee lamp compares it with a reference and saves only on Save',async()=>{
 const calls=[];
 const app=await harness({live:liveOk(SYNC_IDLE),calibrate:(url,body)=>{calls.push([url,body]);return {status:200,body:{active:url==='/api/calibration'}}}});
 app.run('select("govee-left")');await openCard(app);
 const row=app.$('#object-menu [data-menu="calibrate"]');
 assert.equal(row.querySelector('em').textContent,'65%','the row shows the correction');
 await app.click(row);
 assert.deepEqual(calls[0],['/api/calibration',{lamp:'govee-left',reference:'play-left',level:'bright',view:'scene',correction:65,calibration:{warmth:0,tint:0,lowLight:0}}],'the nearest Hue Play is the reference');
 await app.advance(20);
 assert.ok(app.$('#object-menu .card-pages').classList.contains('at-cal'),'the card slides to the Calibrate page');
 assert.ok(app.$('#app').classList.contains('calibrating'));
 assert.ok(app.lamp('govee-left').classList.contains('cal-keep')&&app.lamp('play-left').classList.contains('cal-keep'));
 assert.ok(!app.lamp('govee-right').classList.contains('cal-keep'));
 assert.ok(app.stage.querySelector('#cal-ref-tag'),'the reference is labelled');
 const set=async(key,v)=>{const s=app.$('#object-menu [data-cal="'+key+'"]');s.value=String(v);app.dispatch(s,'input');await app.settle()};
 await set('warmth',40);await set('correction',50);
 assert.deepEqual(calls.at(-1)[1],{...calls[0][1],correction:50,calibration:{warmth:40,tint:0,lowLight:0}},'a slider shows on the lamp at once');
 assert.equal(app.posts.length,0,'nothing is saved while the sliders move');
 await app.advance(2000);assert.equal(calls.at(-1)[0],'/api/calibration','the session is renewed');
 await app.click(app.$('#object-menu [data-menu="cal-level-dim"]'));assert.equal(calls.at(-1)[1].level,'dim');
 await app.click(app.$('#object-menu [data-menu="cal-save"]'));
 assert.equal(calls.at(-1)[0],'/api/calibration/end');
 assert.ok(!app.$('#app').classList.contains('calibrating'));
 assert.ok(!app.$('#object-menu .card-pages').classList.contains('at-cal'),'back on the first page');
 assert.equal(app.$('#object-menu').hidden,false,'the card stays open');
 await app.advance(300);
 const saved=app.posts.at(-1).body.devices.find(r=>r.id==='govee-left');
 assert.deepEqual(saved.calibration,{warmth:40,tint:0,lowLight:0});assert.equal(saved.sceneScale,50);
 assert.equal(app.$('#object-menu [data-menu="calibrate"] em').textContent,'50% · color');
 const count=calls.length;await app.advance(5000);assert.equal(calls.length,count,'no renewal after the session');
});

test('a Hue lamp calibrates its brightness only; Reset and Esc save nothing',async()=>{
 const calls=[];
 const app=await harness({live:liveOk(SYNC_IDLE),calibrate:(url,body)=>{calls.push([url,body]);return {status:200,body:{active:true}}}});
 app.run('select("play-center")');await openCard(app);
 await app.click(app.$('#object-menu [data-menu="calibrate"]'));await app.advance(20);
 assert.equal(calls[0][1].calibration,undefined,'no colour for a Hue lamp');
 assert.equal(app.$('#object-menu [data-cal="warmth"]'),null);
 const s=app.$('#object-menu [data-cal="correction"]');s.value='30';app.dispatch(s,'input');await app.settle();
 assert.equal(calls.at(-1)[1].correction,30);
 await app.click(app.$('#object-menu [data-menu="cal-reset"]'));
 assert.equal(calls.at(-1)[1].correction,100,'the reset shows on the lamp at once');
 await app.advance(100);const mid=+app.$('#object-menu [data-cal="correction"]').value;
 assert.ok(mid>30&&mid<100,'the slider glides to its reset value');
 await app.advance(300);
 assert.equal(app.$('#object-menu [data-cal="correction"]').value,'100','Reset is unsaved: brightness back to 100%');
 assert.equal(app.$('#object-menu [data-menu="cal-reset"]').disabled,true);
 await app.press(app.$('#object-menu'),'Escape');
 assert.equal(calls.at(-1)[0],'/api/calibration/end');
 assert.equal(app.$('#object-menu').hidden,false,'Esc leaves the page, the card stays');
 await app.advance(300);assert.equal(app.posts.length,0);
});

test('during Sync the brightness changes the running Sync and Cancel restores it',async()=>{
 const app=await harness({calibrate:()=>assert.fail('no test light during Sync')});
 await app.click(app.$('#sync-tool'));
 app.run('select("govee-left")');await openCard(app);
 await app.click(app.$('#object-menu [data-menu="calibrate"]'));await app.advance(20);
 assert.equal(app.$('#object-menu select.cal-ref'),null,'no Compare with during Sync');
 assert.equal(app.$('#object-menu .card-note').textContent,'Turn Sync off to calibrate color');
 assert.match(app.$('#object-menu .card-slider-top').textContent,/with Sync/);
 const s=app.$('#object-menu [data-cal="correction"]');s.value='30';app.dispatch(s,'input');await app.advance(300);
 assert.equal(app.posts.at(-1).body.devices.find(r=>r.id==='govee-left').scale,30,'saved at once');
 await app.click(app.$('#object-menu [data-menu="cal-cancel"]'));await app.advance(300);
 assert.equal(app.posts.at(-1).body.devices.find(r=>r.id==='govee-left').scale,65,'Cancel restores it');
});

test('ending a test waits for the update still on its way',async()=>{
 const order=[];let release;
 const app=await harness({live:liveOk(SYNC_IDLE),calibrate:(url,body)=>{
  order.push(url+(body.correction!==undefined?':'+body.correction:''));
  if(body.correction===40)return new Promise(done=>{release=()=>done({status:200,body:{active:true}})});
  return {status:200,body:{active:url==='/api/calibration'}};
 }});
 app.run('select("govee-left")');await openCard(app);
 await app.click(app.$('#object-menu [data-menu="calibrate"]'));await app.advance(20);
 const s=app.$('#object-menu [data-cal="correction"]');s.value='40';app.dispatch(s,'input');await app.settle();
 await app.click(app.$('#object-menu [data-menu="cal-cancel"]'));
 assert.deepEqual(order,['/api/calibration:65','/api/calibration:40'],'the end waits');
 release();await app.settle();
 assert.deepEqual(order,['/api/calibration:65','/api/calibration:40','/api/calibration/end']);
});

test('Reset during Sync keeps the colour calibration it does not show',async()=>{
 const remote=saved();remote.devices.find(r=>r.id==='govee-left').calibration={warmth:50,tint:-10,lowLight:20};
 const app=await harness({layout:remote,calibrate:()=>assert.fail('no test light during Sync')});
 await app.click(app.$('#sync-tool'));
 app.run('select("govee-left")');await openCard(app);
 await app.click(app.$('#object-menu [data-menu="calibrate"]'));await app.advance(20);
 const s=app.$('#object-menu [data-cal="correction"]');s.value='30';app.dispatch(s,'input');await app.advance(300);
 await app.click(app.$('#object-menu [data-menu="cal-reset"]'));await app.advance(400);
 await app.click(app.$('#object-menu [data-menu="cal-save"]'));await app.advance(300);
 const row=app.posts.at(-1).body.devices.find(r=>r.id==='govee-left');
 assert.equal(row.scale,100);assert.deepEqual(row.calibration,{warmth:50,tint:-10,lowLight:20});
});

test('a test stops when Sync starts',async()=>{
 let answer={status:200,body:{active:true}};
 const app=await harness({live:liveOk(SYNC_IDLE),calibrate:()=>answer});
 app.run('select("govee-right")');await openCard(app);
 await app.click(app.$('#object-menu [data-menu="calibrate"]'));await app.advance(20);
 assert.ok(app.$('#app').classList.contains('calibrating'));
 const before=app.posts.length;answer={status:409,body:{active:false,reason:'sync active'}};
 await app.advance(2000);
 assert.ok(!app.$('#app').classList.contains('calibrating'));
 assert.equal(app.$('#toast').textContent,'Calibration stopped: Sync started');
 await app.advance(300);assert.equal(app.posts.length,before,'nothing was saved');
});

test('the correction without Sync is the lamp\'s own value, never the Sync one',async()=>{
 const remote=saved();remote.devices.find(r=>r.id==='play-left').scale=70;
 for(const r of remote.devices)delete r.sceneScale;
 const app=await harness({layout:remote});
 const lamp=id=>json(app,'lamps.find(l=>l.id==="'+id+'")');
 assert.equal(lamp('play-left').sceneScale,70,'a row without it starts from a copy of the Sync correction');
 await app.click(app.$('#sync-tool'));
 app.run('select("play-left")');await openCard(app);
 await app.click(app.$('#object-menu [data-menu="calibrate"]'));
 const bri=app.$('#object-menu [data-cal="correction"]');bri.value='30';app.dispatch(bri,'input');
 await app.click(app.$('#object-menu [data-menu="cal-save"]'));await app.advance(300);
 const row=app.posts.at(-1).body.devices.find(r=>r.id==='play-left');
 assert.equal(row.scale,30);assert.equal(row.sceneScale,70,'a Sync correction change keeps the one without Sync');
 await app.click(app.$('#sync-tool'));
 assert.equal(app.run('corr(lamps.find(l=>l.id==="play-left"))'),70);
 await app.click(app.$('#list-toggle'));
 await app.click(app.$('#lamp-panel').querySelector('[data-place]'));
 app.dispatch(app.stage,'pointerdown',{clientX:500+100*3.3,clientY:600});await app.settle();
 const added=lamp(discovered().id);assert.equal(added.sceneScale,added.scale,'a new lamp starts with equal corrections');
});

test('a drawn lamp that is not connected shows a broken-link badge where problems show',async()=>{
 const remote=saved();remote.devices.push({id:discovered().id,name:'Third <lamp>',model:'H6076',shape:'floor',zones:7,scale:45,positionCm:{x:150,height:60}});
 const app=await harness({layout:remote});
 const node=app.lamp(discovered().id),badge=node.querySelector('[data-link-badge]');
 assert.ok(badge,'the badge is drawn');assert.equal(badge.dataset.tooltip,'Not connected');
 assert.equal(node.querySelector('[data-badge]'),null,'no problem badge');
 assert.equal(app.lamp('govee-left').querySelector('[data-link-badge]'),null,'connected lamps have none');
 app.run('select("'+discovered().id+'")');await app.settle();
 assert.equal(app.lamp(discovered().id).querySelector('[data-link-badge]'),null,'hidden while the chip is shown');
});

test('a drawn lamp is connected while the core lists it, and shows its live light',async()=>{
 const id=discovered().id,remote=saved();
 remote.devices.push({id,name:'Third <lamp>',model:'H6008',shape:'bar',zones:1,scale:80,positionCm:{x:150,height:40},calibration:{warmth:10,tint:0,lowLight:0}});
 let lights=[{id,provider:'govee',model:'H6008',zones:1,migrated:false}];
 const app=await harness({layout:remote,live:()=>liveOk({lights,lamps:{[id]:lit([[0,0,255]])}})});
 await app.advance(1500);
 assert.equal(app.lamp(id).querySelector('[data-link-badge]'),null,'an enrolled lamp is connected');
 assert.equal(app.run('lamps.find(l=>l.id==="'+id+'").unbound'),false);
 assert.ok(app.run('emittedColors(lamps.find(l=>l.id==="'+id+'"))'),'its live light is drawn');
 assert.equal(app.run('calMode(lamps.find(l=>l.id==="'+id+'"))'),'live','a connected Govee lamp can be calibrated');
 // The calibration of an enrolled Govee row is saved with the row.
 app.run('setLamp(lamps.find(l=>l.id==="'+id+'"),l=>{l.scale=70})');await app.advance(300);
 assert.deepEqual(app.posts.at(-1).body.devices.find(r=>r.id===id).calibration,{warmth:10,tint:0,lowLight:0});
 lights=[];await app.advance(1500);
 assert.ok(app.lamp(id).querySelector('[data-link-badge]'),'a removed light is not connected again');
});

test('Blink asks the server once and explains a refusal; Off is saved for role lamps only',async()=>{
 const calls=[];
 const app=await harness({identify:body=>{calls.push(body);return calls.length===1?{status:200,body:{accepted:true}}:{status:409,body:{accepted:false,reason:'lamp off'}}}});
 app.run('select("play-left")');
 await app.run('blinkLamp(lamps.find(l=>l.id==="play-left"))');await app.settle();
 assert.deepEqual(JSON.parse(JSON.stringify(calls)),[{lamp:'play-left'}]);
 assert.ok(app.lamp('play-left').classList.contains('blinking'),'the drawing blinks too');
 assert.equal(app.$('#toast').textContent,'Blinking Hue Play Left');
 await app.run('blinkLamp(lamps.find(l=>l.id==="play-left"))');await app.settle();
 assert.equal(app.$('#toast').textContent,'The lamp is off');
 app.run('menuAction("role-off")');await app.advance(300);
 assert.equal(app.posts.at(-1).body.devices.find(r=>r.id==='play-left').syncRole,'off');
 assert.equal(json(app,'syncProjection(lamps.find(l=>l.id==="play-left"))').length,0,'an Off lamp takes no screen colour');
});

test('a desk lamp name is saved only once the owner renames it, and survives a reload',async()=>{
 const app=await harness();
 await app.advance(300);
 assert.equal(app.run('pack().devices.find(r=>r.id==="govee-left").name'),undefined,'built-in names are not stored');
 app.run('renameLamp("govee-left","  Left tower ")');await app.advance(300);
 assert.equal(app.posts.at(-1).body.devices.find(r=>r.id==='govee-left').name,'Left tower');
 app.run('renameLamp("'+discovered().id+'","x")');
 const reloaded=await harness({layout:app.posts.at(-1).body});
 assert.equal(reloaded.run('lamps.find(l=>l.id==="govee-left").name'),'Left tower');
 assert.equal(reloaded.run('pack().devices.find(r=>r.id==="govee-left").name'),'Left tower');
 // Govee names keep up to 35 characters; Hue names stop at the bridge's 32.
 const long=saved();long.devices[0].name='G'.repeat(35);long.devices[3].name='H'.repeat(32);
 const limits=await harness({layout:long});
 assert.equal(limits.run('pack().devices.find(r=>r.id==="govee-left").name'),'G'.repeat(35));
 limits.run('renameLamp("play-left","'+'P'.repeat(40)+'")');
 assert.equal(limits.run('lamps.find(l=>l.id==="play-left").name'),'P'.repeat(32));
});

test('a stale save merges the second configuration field by field',async()=>{
 const app=await harness();
 const base=canonical(saved()),local=json(app,'pack()'),remote=canonical(saved());
 delete base.syncFalloff;delete base.sceneFalloff;delete remote.syncFalloff;delete remote.sceneFalloff;
 local.sceneFalloff=true;local.devices[0].sceneScale=30;remote.devices[1].scale=10;
 const merged=JSON.parse(app.run('JSON.stringify(mergeLayouts('+JSON.stringify(base)+','+JSON.stringify(local)+','+JSON.stringify(remote)+'))'));
 assert.equal(merged.sceneFalloff,true);assert.equal(merged.syncFalloff,undefined,'a default switch is not a change');
 assert.equal(merged.devices[0].sceneScale,30);assert.equal(merged.devices[1].scale,10);
});

test('fromLayout reads version 6 and version 7 screens and pack writes the screen from the monitor lamp',async()=>{
 const app=await harness();
 const old=saved();old.version=6;old.monitorInches=43;
 const fromOld=json(app,'fromLayout('+JSON.stringify(old)+').environment.inches');
 assert.equal(fromOld,43);
 const current=canonical(old);current.screen.inches=40;
 assert.equal(json(app,'fromLayout('+JSON.stringify(current)+').environment.inches'),40);
 assert.equal(json(app,'fromLayout('+JSON.stringify(current)+').lamps.find(l=>l.id==="hue-monitor").x'),0);
 // The monitor lamp's position is the screen position.
 app.run('lamps.find(l=>l.id==="hue-monitor").x=-20;lamps.find(l=>l.id==="hue-monitor").h=125');
 const packed=json(app,'pack()');
 assert.deepEqual(packed.screen.positionCm,{x:80,height:125});
 assert.deepEqual(packed.devices.find(r=>r.id==='hue-monitor').positionCm,{x:80,height:125});
 assert.equal('monitorInches' in packed,false);
});

test('a stale save merges a remote screen change like one field',async()=>{
 const app=await harness();
 const merge=(base,local,remote)=>JSON.parse(app.run('JSON.stringify(mergeLayouts('+JSON.stringify(base)+','+JSON.stringify(local)+','+JSON.stringify(remote)+'))'));
 const base=canonical(saved()),local=json(app,'pack()'),remote=canonical(saved());
 remote.screen.inches=40;
 assert.equal(merge(base,local,remote).screen.inches,40,'a remote size change survives an untouched local screen');
 local.screen.inches=34;
 assert.equal(merge(base,local,remote).screen.inches,34,'a local size change wins');
 const untouched=json(app,'pack()'),moved=canonical(saved());
 moved.screen.positionCm={x:60,height:120};
 assert.deepEqual(merge(base,untouched,moved).screen.positionCm,{x:60,height:120});
 // A baseline saved before version 7 carries monitorInches: it does not count as a local change.
 const old=saved();old.version=6;
 assert.equal(merge(old,json(app,'pack()'),remote).screen.inches,40);
 assert.equal('monitorInches' in merge(old,json(app,'pack()'),remote),false);
});

test('an unlit lens or zone shows black under the light',async()=>{
 const app=await harness({live:liveOk({sync:{state:'off'},lamps:{'play-left':lit([[255,0,0]],{source:'scene-output',on:false})}})});
 const node=app.lamp('play-left'),off=node.querySelector('.lens-off');
 assert.ok(off,'a black lens is drawn');
 let inside=false;for(let n=off.parentNode;n&&n!==node;n=n.parentNode)if(n.getAttribute?.('data-sync-power')!=null)inside=true;
 assert.ok(!inside,'outside the brightness groups, so it shows when the lamp is off');
 assert.equal(node.querySelector('[data-sync-power]').getAttribute('opacity'),'0');
 for(const id of ['govee-left','hue-monitor'])assert.ok(app.lamp(id).querySelector('.lens-off'),id);
});

test('the desk, monitor and zone are edited in place and saved in layout units',async()=>{
 const app=await harness();
 app.run('select("desk")');app.run('changeField("height",90)');
 await app.advance(300);
 const body=app.posts.at(-1).body;
 assert.deepEqual(body.desk,{width:160,height:90});assert.deepEqual(body.devices[2].positionCm,{x:100,height:115},'the monitor rides on the tabletop');
 app.run('select("monitor")');app.run('changeField("inches",34)');await app.advance(300);
 assert.equal(app.posts.at(-1).body.screen.inches,34);assert.equal(app.posts.at(-1).body.monitorInches,undefined);
 await app.click(app.$('#sync-tool'));app.run('select("zone")');
 const before=app.run('JSON.stringify(zonePoints())');
 app.run('drag={handle:"zp-3",before:snapshot(),moved:false};dragHandle("zp-3",{x:500+90*3.3,y:740-70*3.3});');
 app.dispatch(app.stage,'pointerup');await app.advance(300);
 const zone=app.posts.at(-1).body.syncZone;
 assert.equal(zone.version,1);assert.equal(zone.points[3].u,90/80);assert.equal(zone.points[3].h,-20);
 assert.notEqual(app.run('JSON.stringify(zonePoints())'),before);
 assert.equal(app.run('validSyncZone({version:1,points:[{u:0,h:0},{u:1,h:0},{u:1,h:-1},{u:2,h:-2},{u:-1,h:-2},{u:0,h:-1}]})'),false);
});

test('names from discovery are escaped in the room and the panel',async()=>{
 const remote=saved();remote.devices.push({id:discovered().id,name:'<img src=x onerror=alert(1)>',model:'H6076',shape:'floor',zones:7,scale:45,positionCm:{x:150,height:60}});
 const app=await harness({layout:remote});
 await app.click(app.$('#list-toggle'));
 assert.equal(app.document.querySelectorAll('img').length,0);
 assert.match(app.$('#lamp-panel').textContent,/<img src=x onerror=alert\(1\)>/);
});

// ---------------------------------------------------------------- Sync Box --
const BOX={state:'ready',syncing:false,name:'Sync Box 8K',ip:'192.168.10.101'};
const WAITING=liveOk({sync:{state:'waiting',reason:'no_frames',blockers:[]}});
// A fake Light Studio Sync Box API: state moves the way the server moves it.
function fakeBox(start={state:'none'}){
 const box={status:structuredClone(start),pressAfter:Infinity,polls:0};
 box.api=(url,body)=>{
  if(url==='/api/sync-box'){
   if(box.status.state==='connecting'&&++box.polls>=box.pressAfter)box.status={...BOX};
   else if(box.status.state==='connecting')box.status={...box.status,remaining:box.status.remaining-1};
   return box.status;
  }
  if(url==='/api/sync-box/find')return {boxes:[{ip:BOX.ip,name:BOX.name}]};
  if(url==='/api/sync-box/pair'){if(body.ip==='8.8.8.8')return {status:400,body:{error:'invalid'}};box.status={state:'connecting',remaining:30,name:BOX.name,ip:body.ip};box.polls=0;return box.status}
  if(url==='/api/sync-box/cancel'){box.status={state:'none'};return box.status}
  if(url==='/api/sync-box/sync'){if(box.offline)return {status:503,body:{...box.status,state:'offline'}};if(box.silent)return {status:503,body:box.status};box.status={...box.status,syncing:body.active};return box.status}
  if(url==='/api/sync-box/forget'){box.status={state:'none'};return box.status}
  assert.fail('unexpected '+url);
 };
 return box;
}
// The corner bar: the one Sync status, then the one action.
const status=app=>app.$('#sync-status span').textContent;
const action=app=>{const act=app.$('#sync-box .box-act');return act.hidden?'':act.textContent.trim()};
const actionButton=app=>app.$('#sync-box .box-act button');
const said=app=>{const el=app.$('#sync-box .box-said');return el.hidden?'':el.textContent.trim()};

test('the toolbar has only the Settings switch; the Sync status is in the corner',async()=>{
 const app=await harness();
 assert.equal(app.$('#sync-tool .sync-word').textContent,'Settings');
 assert.equal(app.$('#sync-tool').getAttribute('aria-label'),'Show the Sync settings');
 assert.equal(app.$('.toolbar #sync-status'),null);
 assert.ok(app.$('#sync-box .box-bar #sync-status'));
});

test('without a box the corner shows Connect, and the adapter status only once Sync is attempted',async()=>{
 const app=await harness();
 assert.equal(status(app),'Syncing');
 assert.equal(action(app),'Connect Sync Box');
 const idle=await harness({live:liveOk({sync:{state:'off',reason:'explicit_stop'}})});
 assert.equal(idle.$('#sync-status').hidden,true,'Not syncing next to Connect is not news');
 assert.equal(action(idle),'Connect Sync Box');
 const waiting=await harness({live:WAITING});
 assert.equal(waiting.$('#sync-status').hidden,false);
});

test('controls in the box card have no tooltips that repeat their words',async()=>{
 const app=await harness({syncBox:fakeBox().api});
 await app.click(app.$('#sync-box [data-box="connect"]'));
 app.run('annotateTooltips()');
 assert.deepEqual(app.$('#box-card').querySelectorAll('[data-tooltip]').map(n=>n.dataset.tooltip),[]);
});

test('the card connects the box found on the network in two steps, all inside the corner',async()=>{
 const box=fakeBox();box.pressAfter=3;
 const app=await harness({syncBox:box.api});
 await app.click(app.$('#sync-box [data-box="connect"]'));
 const card=app.$('#box-card');
 assert.equal(card.hidden,false);
 assert.equal(app.$('#sync-box').classList.contains('open'),true);
 assert.match(card.textContent,/1Choose your Sync BoxFound on this network:/);
 const row=card.querySelector('[data-box-ip]');assert.match(row.textContent,/Sync Box 8K.*192\.168\.10\.101/);
 await app.click(row);
 assert.deepEqual(app.requests.box.at(-1),{url:'/api/sync-box/pair',body:{ip:'192.168.10.101'},at:app.now()});
 assert.ok(card.querySelector('.box-step.done'),'step 1 is ticked');
 assert.match(card.querySelector('.box-step.active').textContent,/Hold the button on the box/);
 assert.equal(card.querySelector('.box-ring b').textContent,'30');
 assert.equal(status(app),'Connecting…');
 await app.advance(1000);
 assert.equal(card.querySelector('.box-ring b').textContent,'29','the countdown follows the server');
 await app.advance(2000);
 assert.equal(said(app),'Connected');
 assert.equal(card.hidden,true);
 assert.equal((status(app)+' | '+action(app)),'Sync off | Start Sync');
 assert.equal(app.$('#toast').textContent,'','nothing is said at the bottom');
 await app.advance(4000);assert.equal(said(app),'');
});

test('Start Sync reads Starting until frames arrive, then Syncing',async()=>{
 let live=WAITING;let release;const box=fakeBox(BOX);
 const app=await harness({live:()=>live,syncBox:(url,body)=>url==='/api/sync-box/sync'?new Promise(done=>{release=()=>done(box.api(url,body))}):box.api(url,body)});
 assert.equal(status(app)+' | '+action(app),'Sync off | Start Sync','the adapter health is not news while Sync is off');
 await app.click(actionButton(app));
 assert.equal(status(app)+' | '+action(app),'Starting… | Start Sync');
 assert.ok(actionButton(app).disabled,'one change at a time');
 const sent=app.requests.box.length;await app.click(actionButton(app));assert.equal(app.requests.box.length,sent);
 release();await app.settle();
 assert.equal(status(app)+' | '+action(app),'Starting… | Stop Sync','the box runs Sync, the frames are still to come');
 await app.advance(10000);
 assert.equal(status(app),'Starting…');
 live=liveOk();await app.advance(200);
 assert.equal(status(app),'Syncing');
 assert.equal(app.$('#sync-status').dataset.level,'ok');
});

test('Sync that never gets frames says so after its start time',async()=>{
 const box=fakeBox(BOX);
 const app=await harness({live:WAITING,syncBox:box.api});
 await app.click(actionButton(app));
 await app.advance(19000);assert.equal(status(app),'Starting…');
 await app.advance(1500);assert.equal(status(app),'Waiting for frames');
});

test('Stop Sync reads Stopping, then Sync off however the stream winds down',async()=>{
 let release;const box=fakeBox({...BOX,syncing:true});
 const app=await harness({syncBox:(url,body)=>url==='/api/sync-box/sync'?new Promise(done=>{release=()=>done(box.api(url,body))}):box.api(url,body)});
 assert.equal(status(app)+' | '+action(app),'Syncing | Stop Sync');
 await app.click(actionButton(app));
 assert.equal(status(app)+' | '+action(app),'Stopping… | Stop Sync');
 release();await app.settle();
 assert.equal(status(app)+' | '+action(app),'Sync off | Start Sync');
 app.context.liveSnapshot=null;
});

test('a box that does not answer is offline without an action, and a failed Start says so in the corner',async()=>{
 const box=fakeBox(BOX);
 const app=await harness({syncBox:box.api});
 box.silent=true;
 await app.click(actionButton(app));
 assert.equal(said(app),'The Sync Box did not respond');
 assert.equal(status(app),'Sync off');
 box.silent=false;box.offline=true;
 await app.click(actionButton(app));
 assert.equal(status(app)+' | '+action(app),'Sync Box offline | ');
 assert.equal(app.$('#toast').textContent,'');
});

test('another page sees Starting while the box switches',async()=>{
 const box=fakeBox({...BOX,changing:true});
 const app=await harness({syncBox:box.api});
 assert.equal(status(app),'Starting…');
 assert.ok(actionButton(app).disabled);
 box.status={...BOX,syncing:true};
 await app.advance(3000);
 assert.equal(status(app)+' | '+action(app),'Syncing | Stop Sync');
});

test('an address can be typed, and a wrong one is explained at its step',async()=>{
 const box=fakeBox();
 const app=await harness({syncBox:(url,body)=>url==='/api/sync-box/find'?{boxes:[]}:box.api(url,body)});
 await app.click(app.$('#sync-box [data-box="connect"]'));
 const card=app.$('#box-card');
 assert.match(card.textContent,/None found\. Enter its IP address/);
 const input=card.querySelector('#box-ip');input.value='8.8.8.8';app.dispatch(input,'input');
 await app.press(input,'Enter');
 assert.match(card.querySelector('.note.bad').textContent,/Enter an address on this network/);
 assert.equal(app.$('#toast').textContent,'');
 const again=card.querySelector('#box-ip');assert.equal(again.value,'8.8.8.8','the typed address stays');
 again.value=' 192.168.10.101 ';app.dispatch(again,'input');
 assert.equal(card.querySelector('.note.bad'),null,'typing clears the error');
 await app.click(card.querySelector('[data-box="address"]'));
 assert.deepEqual(app.requests.box.at(-1).body,{ip:'192.168.10.101'});
 assert.match(card.textContent,/Hold the button/);
});

test('Escape while connecting stops asking the box',async()=>{
 const box=fakeBox();
 const app=await harness({syncBox:box.api});
 await app.click(app.$('#sync-box [data-box="connect"]'));
 await app.click(app.$('#box-card [data-box-ip]'));
 await app.press(app.$('#box-card'),'Escape');
 assert.equal(app.requests.box.at(-1).url,'/api/sync-box/cancel');
 assert.equal(app.$('#box-card').hidden,true);
 assert.equal(action(app),'Connect Sync Box');
});

test('a missed button press offers Try again',async()=>{
 const box=fakeBox({state:'timeout',name:BOX.name,ip:BOX.ip});
 const app=await harness({syncBox:box.api});
 app.run('openBoxCard("connect")');await app.settle();
 assert.match(app.$('#box-card').textContent,/not held in time/);
 await app.click(app.$('#box-card [data-box="retry"]'));
 assert.deepEqual(app.requests.box.at(-1),{url:'/api/sync-box/pair',body:{ip:BOX.ip},at:app.now()});
});

test('the status opens the box details, where Forget asks first',async()=>{
 const box=fakeBox(BOX);
 const app=await harness({syncBox:box.api});
 await app.click(app.$('#sync-status'));
 const card=app.$('#box-card');
 assert.match(card.textContent,/Sync Box 8K192\.168\.10\.101Forget Sync Box/);
 await app.click(card.querySelector('[data-box="forget"]'));
 assert.ok(!app.requests.box.some(r=>r.url==='/api/sync-box/forget'),'nothing is forgotten before the confirmation');
 await app.click(card.querySelector('[data-box="forget-cancel"]'));
 await app.click(card.querySelector('[data-box="forget"]'));
 await app.click(card.querySelector('[data-box="forget-yes"]'));
 assert.equal(app.requests.box.at(-1).url,'/api/sync-box/forget');
 assert.equal(action(app),'Connect Sync Box');
 assert.equal(said(app),'Sync Box forgotten');
});

test('the corner follows the box and stops asking while the page is hidden',async()=>{
 const box=fakeBox(BOX);
 const app=await harness({syncBox:box.api});
 box.status={...BOX,syncing:true};
 await app.advance(3000);
 assert.equal(status(app)+' | '+action(app),'Syncing | Stop Sync','Sync started elsewhere and frames already flow');
 box.status={state:'unauthorized',name:BOX.name,ip:BOX.ip};
 await app.advance(3000);
 assert.equal(status(app)+' | '+action(app),'Sync Box disconnected | Reconnect');
 await app.visible(false);
 const asked=app.requests.box.length;
 await app.advance(10000);
 assert.equal(app.requests.box.length,asked);
 await app.visible(true);
 assert.equal(app.requests.box.length,asked+1);
});

test('a lamp problem during Sync is the status, and clicking it shows the lamp',async()=>{
 const box=fakeBox({...BOX,syncing:true});
 const app=await harness({live:liveOk({sync:{state:'active',reason:'frames_received',blockers:[{role:'govee-left',code:'owner_off'}]},lamps:{'govee-left':lit(Array(7).fill([255,0,0]))}}),syncBox:box.api});
 assert.equal(status(app)+' | '+action(app),'Govee Left off in HomeKit | Stop Sync');
 await app.click(app.$('#sync-status'));
 assert.equal(app.run('selected'),'govee-left');
 assert.equal(app.$('#box-card').hidden,true,'a problem click shows the lamp, not the box details');
});

test('Sync that ends without a Stop from here is said in the corner',async()=>{
 const box=fakeBox({...BOX,syncing:true});
 const app=await harness({syncBox:box.api});
 box.status={...BOX,syncing:false};
 await app.advance(3000);
 assert.equal(status(app)+' | '+action(app),'Sync off | Start Sync');
 assert.equal(said(app),'Sync stopped on the Sync Box');
 await app.click(actionButton(app));await app.click(actionButton(app));
 assert.equal(said(app),'Sync stopped on the Sync Box','a Stop from here says nothing new');
 await app.advance(4000);assert.equal(said(app),'');
});

test('the corner shows the guard restarting Sync and says when it gives up',async()=>{
 const box=fakeBox({...BOX,syncing:true});
 const app=await harness({live:WAITING,syncBox:box.api});
 box.status={...BOX,syncing:false,recovery:'restarting'};
 await app.advance(3000);
 assert.equal(status(app),'Restarting Sync…');
 assert.equal(said(app),'','the guard\'s own Stop is not news');
 box.status={...BOX,syncing:true,recovery:'failed'};
 await app.advance(3000);
 assert.equal(said(app),'Sync did not come back. Stop and start it again');
 await app.advance(20100);
 assert.equal(status(app)+' | '+action(app),'Waiting for frames | Stop Sync','after the start time');
});

test('a connection that could not be saved is said at step 2 with Try again',async()=>{
 const box=fakeBox({state:'failed',name:'Sync Box 8K',ip:'192.168.10.101'});
 const app=await harness({syncBox:box.api});
 app.run('openBoxCard("connect")');await app.settle();
 const card=app.$('#box-card');
 assert.match(card.textContent,/Light Studio could not save the connection\./);
 assert.doesNotMatch(card.textContent,/not held in time/);
 await app.click(card.querySelector('[data-box="retry"]'));
 assert.deepEqual(app.requests.box.at(-1),{url:'/api/sync-box/pair',body:{ip:BOX.ip},at:app.now()});
});

test('the lamp card keeps its fixed width: no comment splits a selector list',()=>{
 const css=fs.readFileSync(path.join(STATIC,'style.css'),'utf8');
 assert.match(css,/#object-menu\.card\{width:280px;/);
 assert.doesNotMatch(css,/,\s*\/\*/,'a comment after a comma joins two rules into one selector list');
});

// ------------------------------------------------------------ connecting --
const enrolledRow=(id,extra={})=>({id,provider:'govee',model:'H6076',zones:7,migrated:false,...extra});
async function placeFound(app){
 await app.click(app.$('#list-toggle'));
 await app.click(app.$('#lamp-panel').querySelector('[data-place]'));
 app.dispatch(app.stage,'pointerdown',{clientX:500+100*3.3,clientY:600});await app.settle();
 await app.advance(40);
}
test('placing a found lamp asks to connect it; Not now leaves it in the layout only',async()=>{
 const calls=[];
 const app=await harness({lights:(url,body)=>{calls.push([url,body]);return {status:200,body:{accepted:true,restarting:true}}}});
 await placeFound(app);
 const menu=app.$('#object-menu');
 assert.equal(menu.hidden,false,'the prompt opens beside the new lamp');
 assert.match(menu.textContent,/Connect Third <lamp>\?/);
 assert.doesNotMatch(app.$('#toast')?.textContent||'',/Added to layout/,'no toast with the prompt');
 await app.click(menu.querySelector('[data-menu="connect-later"]'));
 assert.equal(calls.length,0);
 assert.equal(app.run('lamps.find(l=>l.id==="'+discovered().id+'").unbound'),true,'still not connected');
 await app.advance(300);
 assert.equal(app.$('#object-menu').hidden,true);
 assert.equal(app.posts.at(-1).body.devices.at(-1).id,discovered().id,'the row is saved');
});

test('Connect enrolls the lamp; it is Connected once the core lists it',async()=>{
 const id=discovered().id,calls=[];let lights=[];
 const app=await harness({live:()=>liveOk({lights}),lights:(url,body)=>{calls.push([url,body]);return {status:200,body:{accepted:true,restarting:true}}}});
 await placeFound(app);
 await app.click(app.$('#object-menu [data-menu="connect-now"]'));await app.advance(40);
 assert.deepEqual(calls,[['/api/lights/enroll',{id,room:'room-00000001'}]]);
 assert.match(app.$('#object-menu').textContent,/Connecting…/);
 const sw=app.$('#object-menu [data-menu="connected"]');assert.ok(sw.disabled,'no second request while connecting');
 await app.click(app.$('#list-toggle'));
 assert.match(app.$('#lamp-panel').querySelector('[data-list-lamp="'+id+'"]').textContent,/Connecting…/);
 lights=[enrolledRow(id)];await app.advance(1500);
 assert.equal(app.run('lamps.find(l=>l.id==="'+id+'").unbound'),false);
 assert.equal(app.run('linking.size'),0);
 assert.match(app.$('#toast').textContent,/Connected Third <lamp>/);
});

test('Connected off asks inline, then removes the lamp from the core',async()=>{
 const id=discovered().id,calls=[],remote=saved();let lights=[enrolledRow(id)];
 remote.devices.push({id,name:'Third <lamp>',model:'H6076',shape:'floor',zones:7,scale:45,positionCm:{x:150,height:60}});
 const app=await harness({layout:remote,live:()=>liveOk({lights}),lights:(url,body)=>{calls.push([url,body]);return {status:200,body:{accepted:true,restarting:true}}}});
 await app.advance(1500);
 app.run('select("'+id+'")');await app.settle();
 await app.click(app.stage.querySelector('[data-chip-action="more"]'));await app.advance(40);
 const menu=app.$('#object-menu');
 assert.equal(menu.querySelector('[data-menu="connected"]').getAttribute('aria-checked'),'true');
 await app.click(menu.querySelector('[data-menu="connected"]'));
 assert.match(menu.querySelector('.confirm').textContent,/Disconnect Third <lamp>\?/);
 await app.click(menu.querySelector('[data-menu="disconnect-cancel"]'));
 assert.equal(calls.length,0);assert.equal(menu.querySelector('.confirm'),null);
 await app.click(menu.querySelector('[data-menu="connected"]'));
 await app.click(menu.querySelector('[data-menu="disconnect-ok"]'));
 assert.deepEqual(calls,[['/api/lights/remove',{id}]]);
 lights=[];await app.advance(1500);
 assert.equal(app.run('lamps.find(l=>l.id==="'+id+'").unbound'),true,'not connected again');
 assert.match(app.$('#toast').textContent,/Disconnected Third <lamp>/);
});

test('a refused or failed connect says why and can be tried again',async()=>{
 let answer={status:409,body:{accepted:false,reason:'sync_active'}};
 const app=await harness({lights:()=>answer});
 await placeFound(app);
 await app.click(app.$('#object-menu [data-menu="connect-now"]'));await app.advance(40);
 assert.match(app.$('#toast').textContent,/Stop Sync to connect or disconnect a lamp/);
 assert.equal(app.run('linking.size'),0);
 answer={status:409,body:{accepted:false,reason:'channels_shared'}};
 await app.click(app.$('#object-menu [data-menu="connected"]'));await app.advance(40);
 assert.match(app.$('#toast').textContent,/same channels as another lamp/);
 answer={status:503,body:{accepted:false}};
 await app.click(app.$('#object-menu [data-menu="connected"]'));
 assert.match(app.$('#toast').textContent,/Connect failed: the adapter is not responding/);
});

test('a connect the core never confirms gives up and says so',async()=>{
 const app=await harness({live:()=>liveOk({lights:[]}),lights:()=>({status:200,body:{accepted:true,restarting:true}})});
 await placeFound(app);
 await app.click(app.$('#object-menu [data-menu="connect-now"]'));
 await app.advance(95000);
 assert.equal(app.run('linking.size'),0);
 assert.match(app.$('#toast').textContent,/Connecting Third <lamp> did not finish/);
});

test('the connect deadline holds while live status is unavailable',async()=>{
 let fail=false;
 const app=await harness({live:()=>fail?new Error('offline'):liveOk({lights:[]}),lights:()=>({status:200,body:{accepted:true,restarting:true}})});
 await placeFound(app);
 await app.click(app.$('#object-menu [data-menu="connect-now"]'));
 fail=true;await app.advance(95000);
 assert.equal(app.run('linking.size'),0);
 assert.match(app.$('#toast').textContent,/Connecting Third <lamp> did not finish/);
});

// -------------------------------------------------------- any set of lamps --
function screenLayout(keep){
 const layout=canonical(saved());
 layout.devices=layout.devices.filter(row=>keep.includes(row.id));
 return layout;
}
test('a room without some of the six lamps loads as saved and writes no lamp back',async()=>{
 const app=await harness({layout:screenLayout(['govee-right','play-left','play-center','play-right'])});
 await app.advance(1000);
 assert.deepEqual(json(app,'lamps.map(l=>l.id)'),['govee-right','play-left','play-center','play-right']);
 assert.equal(app.lamp('govee-left'),null);assert.equal(app.lamp('hue-monitor'),null);
 app.run('setLamp(lamps.find(l=>l.id==="play-left"),l=>{l.scale=70})');await app.advance(300);
 const body=app.posts.at(-1).body;
 assert.deepEqual(body.devices.map(r=>r.id),['govee-right','play-left','play-center','play-right']);
 assert.deepEqual(body.screen.positionCm,{x:100,height:110},'the screen keeps its place without the strip row');
});

test('an empty room has no lamps and the monitor alone moves the screen',async()=>{
 const app=await harness({layout:screenLayout([])});
 await app.advance(1000);
 assert.equal(app.run('lamps.length'),0);
 assert.equal(app.$('#count').textContent,'0');
 app.run('strip().x=20;persistIfChanged()');await app.advance(300);
 assert.deepEqual(app.posts.at(-1).body.screen.positionCm,{x:120,height:110});
 assert.deepEqual(app.posts.at(-1).body.devices,[]);
});

test('a migrated lamp without a row is found under its registry id and placed without a prompt',async()=>{
 const found=discovered({id:'device-'+'b'.repeat(32),name:'H6076 1A2B',layout_id:'govee-left',selectable:false});
 const app=await harness({layout:screenLayout(['govee-right']),discovery:{connected:true,devices:[found],providers:{govee:'ready',hue:'ready'}}});
 await app.click(app.$('#list-toggle'));
 const place=app.$('#lamp-panel').querySelector('[data-place="govee-left"]');
 assert.ok(place,'Found offers Govee Left');
 await app.click(place);
 app.dispatch(app.stage,'pointerdown',{clientX:500-80*3.3,clientY:600});await app.settle();await app.advance(40);
 assert.equal(app.$('#object-menu').hidden,true,'a connected lamp gets no prompt');
 await app.advance(300);
 const row=app.posts.at(-1).body.devices.find(r=>r.id==='govee-left');
 assert.ok(row,'saved under its registry id');
 assert.equal(row.shape,undefined,'a migrated row stores no drawing metadata');
});

test('a layout without a screen and without the monitor row is not loaded',async()=>{
 const broken=saved();broken.devices=broken.devices.filter(r=>r.id!=='hue-monitor');
 const app=await harness();
 assert.equal(app.run('fromLayout('+JSON.stringify(broken)+')'),null);
});

test('the screen keeps the place of a moved monitor strip after the strip row goes',async()=>{
 const app=await harness({layout:screenLayout(['hue-monitor','play-left'])});
 await app.advance(1000);
 app.run('lamps.find(l=>l.id==="hue-monitor").x=20;render();lamps=lamps.filter(l=>l.id!=="hue-monitor");render()');
 assert.deepEqual(json(app,'pack().screen.positionCm'),{x:120,height:110});
});

test('a migrated lamp disconnects with a HomeKit note and reconnects through its found device',async()=>{
 const calls=[],device='device-'+'b'.repeat(32);
 let lights=['govee-left','govee-right','hue-monitor','play-left','play-center','play-right'].map(id=>enrolledRow(id,{migrated:true}));
 const found=discovered({id:device,name:'Hue Play',provider:'hue',layout_id:'play-center'});
 const app=await harness({live:()=>liveOk({lights}),discovery:{connected:true,devices:[found],providers:{govee:'ready',hue:'ready'}},
  lights:(url,body)=>{calls.push([url,body]);return {status:200,body:{accepted:true,restarting:true,...(url.endsWith('enroll')?{id:'play-center'}:{})}}}});
 await app.advance(1500);
 app.run('select("play-center")');await app.settle();
 await app.click(app.stage.querySelector('[data-chip-action="more"]'));await app.advance(40);
 const menu=app.$('#object-menu');
 await app.click(menu.querySelector('[data-menu="connected"]'));
 assert.match(menu.querySelector('.confirm').textContent,/Disconnect Hue Play Center\?HomeKit will show it as not responding/);
 await app.click(menu.querySelector('[data-menu="disconnect-ok"]'));
 assert.deepEqual(calls,[['/api/lights/remove',{id:'play-center'}]]);
 lights=lights.filter(r=>r.id!=='play-center');await app.advance(1500);
 assert.equal(app.lamp('play-center')!==null,true,'the row stays in the layout');
 assert.equal(app.run('lamps.find(l=>l.id==="play-center").unbound'),true);
 // Reconnecting enrolls the found device that discovery names after the row.
 await app.click(app.$('#list-toggle'));await app.advance(40);
 await app.click(app.$('#lamp-panel').querySelector('[data-connect="play-center"]'));await app.advance(40);
 assert.deepEqual(calls.at(-1),['/api/lights/enroll',{id:device,room:'room-00000001'}]);
 lights=[...lights,enrolledRow('play-center',{migrated:true})];await app.advance(1500);
 assert.equal(app.run('lamps.find(l=>l.id==="play-center").unbound'),false);
 assert.match(app.$('#toast').textContent,/Connected Hue Play Center/);
});

test('a disconnected migrated lamp that is not discovered cannot reconnect',async()=>{
 const calls=[];
 const lights=['govee-left','govee-right','hue-monitor','play-left','play-right'].map(id=>enrolledRow(id,{migrated:true}));
 const app=await harness({live:()=>liveOk({lights}),discovery:{connected:true,devices:[],providers:{govee:'ready',hue:'ready'}},lights:(url,body)=>{calls.push([url,body]);return {status:200,body:{accepted:true}}}});
 await app.advance(1500);
 assert.equal(app.run('lamps.find(l=>l.id==="play-center").unbound'),true);
 await app.click(app.$('#list-toggle'));await app.advance(40);
 await app.click(app.$('#lamp-panel').querySelector('[data-connect="play-center"]'));await app.advance(40);
 assert.deepEqual(calls,[]);
 assert.match(app.$('#toast').textContent,/The lamp does not respond/);
});

// ---------------------------------------------------------------- rooms --
const BEDROOM='room-0000000b',OFFICE='room-00000001';
function bedroomLayout(){return {version:7,gridStepCm:5,room:'Bedroom',desk:{width:120,height:75},screen:{positionCm:{x:60,height:100},inches:27},devices:[{id:'govee-right',scale:70,positionCm:{x:140,height:60},orientation:'horizontal'}]};}
function twoRooms(extra={}){return {rooms:[{id:OFFICE,name:'Office'},{id:BEDROOM,name:'Bedroom',layout:bedroomLayout()}],sync:OFFICE,lights:{},assigned:{'govee-right':BEDROOM},placed:{'govee-right':BEDROOM},...extra};}

test('the room cards switch rooms; each room has its own layout, draft and Sync settings',async()=>{
 const storage=new Map([['light-studio-view-v1','sync']]);
 const layout=saved();layout.devices=layout.devices.filter(d=>d.id!=='govee-right');
 const app=await harness({rooms:twoRooms(),layout,storage});await app.advance(100);
 assert.equal(app.$('#room-name').textContent,'Office');
 assert.equal(app.$('#room-box').hidden,false,'the Sync Box room is marked');
 assert.equal(app.run('mode'),'sync');assert.ok(!app.$('#app').classList.contains('no-sync'));
 await app.click(app.$('#room-switch'));await app.advance(0);
 const cards=app.$('#room-cards');assert.equal(cards.hidden,false);
 assert.deepEqual(cards.querySelectorAll('[data-room-card]').map(c=>c.dataset.roomCard),[OFFICE,BEDROOM]);
 assert.ok(cards.querySelector('[data-room-card="'+OFFICE+'"]').classList.contains('current'));
 assert.ok(cards.querySelector('[data-room-card="'+OFFICE+'"] .rc-box'),'the Sync Box shows on its room');
 assert.deepEqual(app.requests.otherLayouts,[BEDROOM],'the other rooms are drawn from their layouts');
 assert.equal(cards.querySelectorAll('[data-room-card="'+BEDROOM+'"] .rc-lamp').length,1);
 await app.click(cards.querySelector('[data-room-go="'+BEDROOM+'"]'));await app.advance(0);
 assert.equal(cards.hidden,true);
 assert.equal(app.$('#room-name').textContent,'Bedroom');
 assert.deepEqual(json(app,'lamps.map(l=>l.id)'),['govee-right']);
 assert.equal(app.storage.get('light-studio-room-v1'),BEDROOM);
 assert.ok(app.$('#app').classList.contains('no-sync'),'Sync settings stay in the Sync Box room');
 assert.equal(app.run('mode'),'placement');assert.equal(app.storage.get('light-studio-view-v1'),'sync','the view choice is kept');
 assert.equal(app.run('past.length'),0,'undo history belongs to its room');
 app.lamp('govee-right').focus();await app.press(app.lamp('govee-right'),'ArrowRight');await app.advance(300);
 assert.equal(app.posts.at(-1).room,BEDROOM);assert.equal(app.posts.at(-1).body.devices[0].positionCm.x,145);
 assert.ok(app.storage.has('light-studio-grid-v3:'+BEDROOM),'the room keeps its own browser draft');
 assert.equal(JSON.parse(app.storage.get('light-studio-grid-v3')).room,'Office','the first room keeps its draft');
 await app.click(app.$('#room-switch'));await app.advance(0);
 await app.click(app.$('#room-cards').querySelector('[data-room-go="'+OFFICE+'"]'));await app.advance(0);
 assert.equal(app.$('#room-name').textContent,'Office');assert.equal(app.run('mode'),'sync');
 assert.equal(app.lamp('govee-right'),null);assert.ok(app.lamp('govee-left'));
});

test('a remembered room opens first; a room deleted elsewhere falls back to the first room',async()=>{
 const app=await harness({rooms:twoRooms(),storage:new Map([['light-studio-room-v1',BEDROOM]])});await app.advance(100);
 assert.equal(app.$('#room-name').textContent,'Bedroom');assert.equal(app.requests.layout,0,'the first room is not loaded');
 const gone=await harness({rooms:twoRooms(),storage:new Map([['light-studio-room-v1','room-000000ff']])});await gone.advance(100);
 assert.equal(gone.$('#room-name').textContent,'Office');assert.equal(gone.storage.get('light-studio-room-v1'),OFFICE);
});

test('Found leaves out lamps of other rooms, and placing a lamp from the first room moves it first',async()=>{
 const other=discovered({id:'device-'+'c'.repeat(32),name:'Spare'});
 const rooms=twoRooms({placed:{'govee-right':BEDROOM,[discovered().id]:OFFICE}});
 const app=await harness({rooms,storage:new Map([['light-studio-room-v1',BEDROOM]]),discovery:{connected:true,devices:[discovered(),other],providers:{govee:'ready',hue:'ready'}},
  roomPost:(action,body,server)=>{if(action==='move'){server.index.assigned[body.light]=body.room;return null}return {status:404,body:{}}}});
 await app.advance(100);
 await app.click(app.$('#list-toggle'));await app.advance(0);
 const panel=app.$('#lamp-panel');
 assert.equal(panel.querySelector('[data-place="'+discovered().id+'"]'),null,'a lamp in another room is not found here');
 await app.click(panel.querySelector('[data-place="'+other.id+'"]'));
 app.dispatch(app.stage,'pointerdown',{clientX:500+100*3.3,clientY:600});await app.settle();await app.advance(300);
 assert.deepEqual(app.requests.roomPosts,[{url:'/api/rooms/move',body:{light:other.id,room:BEDROOM}}]);
 assert.equal(app.posts.at(-1).room,BEDROOM);assert.ok(app.posts.at(-1).body.devices.some(d=>d.id===other.id));
});

test('Move to room moves a lamp with its corrections and reads the room again',async()=>{
 const app=await harness({rooms:twoRooms(),roomPost:(action,body,server)=>{
  if(action!=='move')return {status:404,body:{}};
  server.index.assigned[body.light]=body.room;server.layout.devices=server.layout.devices.filter(d=>d.id!==body.light);server.revision='r9';return null}});
 await app.advance(100);
 app.dispatch(app.lamp('play-right').querySelector('.hit'),'pointerdown',{clientX:599,clientY:608});app.dispatch(app.stage,'pointerup');await app.settle();
 app.dispatch(app.stage.querySelector('[data-chip-action="more"]'),'pointerdown');await app.settle();
 await app.click(app.$('#object-menu').querySelector('[data-menu="move"]'));await app.advance(20);
 const target=app.$('#object-menu').querySelector('[data-menu="move-to-'+BEDROOM+'"]');assert.ok(target);
 assert.equal(app.$('#object-menu').querySelector('[data-menu="move-to-'+OFFICE+'"]'),null,'only the other rooms');
 await app.click(target);await app.advance(300);
 assert.deepEqual(app.requests.roomPosts.at(-1),{url:'/api/rooms/move',body:{light:'play-right',room:BEDROOM}});
 assert.equal(app.lamp('play-right'),null);assert.match(app.$('#toast').textContent,/Hue Play Right moved to Bedroom/);
 assert.equal(app.run('past.length'),0,'undo cannot bring the row back');
 assert.ok(app.posts.every(p=>!p.body.devices.some(d=>d.id==='play-right')),'the page never writes the moved row back');
});

test('rooms are added, renamed and deleted on the cards; a refusal is said on its card',async()=>{
 const NEW='room-0000000c';
 const app=await harness({rooms:twoRooms(),roomPost:(action,body,server)=>{
  if(action==='create'){server.index.rooms.push({id:NEW,name:body.name});server.rooms[NEW]={layout:{...bedroomLayout(),room:body.name,devices:[],version:7},revision:'n1'};return null}
  if(action==='rename'){server.index.rooms.find(r=>r.id===body.room).name=body.name;if(body.room===OFFICE){server.layout.room=body.name;server.revision='r7'}return null}
  if(action==='delete'){if(body.room===OFFICE)return {status:409,body:{accepted:false,reason:'has_sync_box'}};server.index.rooms=server.index.rooms.filter(r=>r.id!==body.room);return null}
  return {status:404,body:{}}}});
 await app.advance(100);
 await app.click(app.$('#room-switch'));await app.advance(0);
 await app.click(app.$('#room-cards').querySelector('[data-room-add]'));
 await app.press(app.$('#room-cards').querySelector('[data-room-new]'),'Escape');
 assert.equal(app.$('#room-cards').hidden,false,'Esc in a name field only leaves the field');
 assert.equal(app.$('#room-cards').querySelector('[data-room-new]'),null);
 await app.click(app.$('#room-cards').querySelector('[data-room-add]'));
 const input=app.$('#room-cards').querySelector('[data-room-new]');input.value='Hall';await app.press(input,'Enter');await app.advance(0);
 assert.deepEqual(app.requests.roomPosts.at(-1),{url:'/api/rooms/create',body:{name:'Hall'}});
 assert.equal(app.$('#room-name').textContent,'Hall','a new room opens');assert.equal(app.storage.get('light-studio-room-v1'),NEW);
 await app.click(app.$('#room-switch'));await app.advance(0);
 await app.click(app.$('#room-cards').querySelector('[data-room-delete="'+OFFICE+'"]'));
 await app.click(app.$('#room-cards').querySelector('[data-room-delete-ok="'+OFFICE+'"]'));await app.advance(0);
 assert.match(app.$('#room-cards').querySelector('[data-room-card="'+OFFICE+'"]').textContent,/The Sync Box is in this room/);
 await app.click(app.$('#room-cards').querySelector('[data-room-delete="'+NEW+'"]'));
 await app.click(app.$('#room-cards').querySelector('[data-room-delete-ok="'+NEW+'"]'));await app.advance(0);
 assert.equal(app.$('#room-name').textContent,'Office','deleting the open room opens the first one');
 assert.equal(app.storage.has('light-studio-grid-v3:'+NEW),false,'its draft is gone');
 assert.equal(app.$('#room-cards').hidden,false,'the cards stay open');
 await app.click(app.$('#room-cards').querySelector('[data-room-rename="'+OFFICE+'"]'));
 const name=app.$('#room-cards').querySelector('[data-room-name="'+OFFICE+'"]');name.value='Studio';await app.press(name,'Enter');await app.advance(0);
 assert.deepEqual(app.requests.roomPosts.at(-1),{url:'/api/rooms/rename',body:{room:OFFICE,name:'Studio'}});
 assert.equal(app.$('#room-name').textContent,'Studio','the open room reads its new name');
});

test('a room deleted elsewhere falls back to the first room when the rooms are read again',async()=>{
 const app=await harness({rooms:twoRooms(),storage:new Map([['light-studio-room-v1',BEDROOM]])});await app.advance(100);
 assert.equal(app.$('#room-name').textContent,'Bedroom');
 app.server.index.rooms=app.server.index.rooms.filter(r=>r.id!==BEDROOM);
 await app.click(app.$('#list-toggle'));await app.advance(0);
 assert.equal(app.$('#room-name').textContent,'Office');assert.match(app.$('#toast').textContent,/That room was deleted/);
 assert.equal(app.$('#lamp-panel').hidden,true);
});

test('the room cards say so when the rooms cannot be read',async()=>{
 const app=await harness({rooms:twoRooms(),roomsDown:()=>true});await app.advance(100);
 await app.click(app.$('#room-switch'));await app.advance(0);
 const cards=app.$('#room-cards');assert.equal(cards.hidden,false);
 assert.match(cards.textContent,/Light Studio server is unavailable/);
 assert.equal(cards.querySelector('[data-room-add]'),null,'nothing to add to without the rooms');
});

test('without a registered Sync Box no room shows the Sync settings',async()=>{
 const app=await harness({rooms:twoRooms({sync:null}),storage:new Map([['light-studio-view-v1','sync']])});await app.advance(100);
 assert.ok(app.$('#app').classList.contains('no-sync'));assert.equal(app.run('mode'),'placement');
 assert.equal(app.$('#room-box').hidden,true);
});

test('a click outside the lamp card keeps it open; its chip and Esc close it',async()=>{
 const app=await harness();
 app.run('select("govee-left")');await app.settle();await openCard(app);
 const menu=app.$('#object-menu'),name=()=>menu.querySelector('.card-name').value;
 assert.equal(name(),'Govee Left');
 app.dispatch(app.stage,'pointerdown',{clientX:20,clientY:20});app.dispatch(app.stage,'pointerup');await app.click(app.stage);await app.advance(200);
 assert.equal(menu.hidden,false,'a click on the empty scene keeps the card');
 assert.equal(app.run('selected'),'govee-left','and the selection');
 await app.click(app.$('#list-toggle'));await app.advance(200);
 assert.equal(menu.hidden,false,'a click on the header keeps the card');
 await app.click(app.$('#list-toggle'));await app.advance(200);
 app.dispatch(app.lamp('play-right').querySelector('.hit'),'pointerdown',{clientX:599,clientY:608});app.dispatch(app.stage,'pointerup');await app.advance(200);
 assert.equal(menu.hidden,false,'another lamp takes over the open card');
 assert.equal(app.run('selected'),'play-right');assert.equal(name(),'Hue Play Right');
 app.dispatch(app.stage.querySelector('[data-chip-action="more"]'),'pointerdown');await app.advance(200);
 assert.equal(menu.hidden,true,'the chip closes the card');
 await openCard(app);await app.press(menu,'Escape');await app.advance(200);
 assert.equal(menu.hidden,true,'Esc closes the card');
});

test('while calibrating, a click on another lamp keeps the calibration',async()=>{
 const calls=[];
 const app=await harness({live:liveOk(SYNC_IDLE),calibrate:(url,body)=>{calls.push([url,body]);return {status:200,body:{active:true}}}});
 app.run('select("play-center")');await openCard(app);
 await app.click(app.$('#object-menu [data-menu="calibrate"]'));await app.advance(20);
 app.dispatch(app.lamp('play-right').querySelector('.hit'),'pointerdown',{clientX:599,clientY:608});app.dispatch(app.stage,'pointerup');await app.advance(200);
 assert.equal(app.run('selected'),'play-center');assert.ok(app.$('#object-menu [data-cal="correction"]'),'still on the Calibrate page');
 assert.ok(!calls.some(([url])=>url==='/api/calibration/end'),'the session goes on');
});

test('a click outside the Sync Box connect card keeps it open; Cancel closes it',async()=>{
 const box=fakeBox();
 const app=await harness({syncBox:box.api});
 await app.click(app.$('#sync-box [data-box="connect"]'));
 await app.click(app.$('#box-card [data-box-ip]'));
 app.dispatch(app.stage,'pointerdown',{clientX:20,clientY:20});await app.click(app.stage);
 assert.equal(app.$('#box-card').hidden,false,'pairing goes on');
 assert.ok(!app.requests.box.some(r=>r.url==='/api/sync-box/cancel'));
 await app.click(app.$('#box-card [data-box="close"]'));
 assert.equal(app.$('#box-card').hidden,true);
});

test('every text button, icon button, confirmation and field comes from the shared controls',()=>{
 // Rows, segments, switches and cards are their own components; any other button goes
 // through btn() or iconBtn(), so a new Cancel cannot get a style of its own again.
 const own=new Set(['btn','icon-btn','card-row','lp-row','card-switch','box-action','rc-pick','rc-card']);
 const raw=[...code.matchAll(/<button[^>]*/g)].map(m=>m[0]).filter(b=>!/data-menu="'\+prefix/.test(b)&&!b.startsWith("<button ','"));
 for(const b of raw)assert.ok(own.has((/class="([a-z-]+)/.exec(b)||[])[1]),'a button outside the shared controls: '+b.slice(0,80));
 assert.deepEqual([...code.matchAll(/<input(?![^>]*type="range")[^>]*/g)].map(m=>m[0]).filter(i=>!/class="field/.test(i)&&!/class="card-name"/.test(i)&&!/class="dim-input"/.test(i)),[],'text fields use field()');
 const css=fs.readFileSync(path.join(STATIC,'style.css'),'utf8');
 for(const old of ['lp-confirm-btn','lp-connect','card-confirm','rc-confirm','rc-input','box-retry','card-reset','box-error'])
  assert.equal(css.includes(old)||code.includes(old),false,old+' is replaced by the shared controls');
});

test('the page uses three type sizes and scene labels keep them at any window size',async()=>{
 const css=fs.readFileSync(path.join(STATIC,'style.css'),'utf8');
 assert.match(css,/--fs-title:15px;--fs-ui:13px;--fs-small:12px/);
 const raw=[...css.matchAll(/([^{}]+)\{[^{}]*font-size:(\d[\d.]*px)/g)].map(m=>m[1].trim()+' '+m[2]);
 // Only the "!" glyphs of the draft status and the health badge keep a size of their own.
 assert.deepEqual(raw.map(r=>r.split(' ').pop()).sort(),['10px','11px'],'font sizes outside the scale: '+raw.join(', '));
 const app=await harness();
 assert.match(app.run("labelUnit=.5,chipSvg(100,200,'Govee Left','',[])"),/translate\(100 200\) scale\(0\.5\)/,'a chip is drawn at screen size around its anchor');
 assert.match(app.run("labelUnit=.5,tagSvg(40,60,'80 cm')"),/^<g transform="translate\(40 60\) scale\(0\.5\) translate\(-40 -60\)">/,'so is a dimension tag');
});

test('a Play lamp lights the wall around it, below the desktop as well as above',async()=>{
 const app=await harness();
 const washes=[...app.stage.querySelectorAll('[data-wash-for="play-left"] .render-wash')];
 assert.ok(washes.length>0,'the Play has a wall wash');
 for(const w of washes){
  assert.equal(w.tagName.toLowerCase(),'ellipse');
  assert.equal(+w.getAttribute('cy'),0,'centred on the lamp');
  assert.ok(+w.getAttribute('ry')>0,'reaching below it as far as above it');
 }
});

test('a segmented floor lamp lights the wall in its zone colours along its whole length',async()=>{
 const app=await harness();
 const washes=[...app.stage.querySelectorAll('[data-wash-for="govee-left"] .render-wash')];
 assert.ok(washes.length>0);
 for(const w of washes)assert.match(w.getAttribute('style')||'',/fill:url\(#wash-govee-left\)/,'not one averaged colour');
 const column=washes.find(w=>w.tagName.toLowerCase()==='rect');
 assert.ok(column,'an even column, not a shape that narrows to the foot');
 assert.equal(+column.getAttribute('width'),200);
});

test('every lamp gives the wall the same light: a long lamp spreads it thinner, white shows as clearly as colour',async()=>{
 const app=await harness();
 const share=id=>+app.stage.querySelector('[data-wash-for="'+id+'"] .render-wash').style.getPropertyValue('--wash-a');
 const play=share('play-left'),floor=share('govee-left');
 assert.equal(play,.7);
 assert.ok(floor<play/2,'the floor lamp wash covers more than twice the area of a Play spot');
 assert.ok(app.run('washBoost([[255,235,207]])')>app.run('washBoost([[255,0,0]])'),'a white light is drawn stronger than a saturated one');
 assert.equal(app.run('washBoost([[255,0,0]])'),1);
});

test('wall light adds up without burning out: washes blend with screen, so no lamp covers another whatever their order',async()=>{
 const css=fs.readFileSync(path.join(STATIC,'style.css'),'utf8');
 assert.match(css,/#wash-layer\{isolation:isolate\}/,'the blend stays inside the wash layer, over the wall');
 assert.match(css,/#wash-layer>\.lamp-wash\{mix-blend-mode:screen\}/,'commutative and never clips to white');
 assert.doesNotMatch(css,/mix-blend-mode:plus-lighter/);
 const app=await harness();
 const layer=app.stage.querySelector('#wash-layer');
 assert.ok([...layer.children].every(w=>w.tagName.toLowerCase()==='defs'||w.classList.contains('lamp-wash')),'every lamp wash is a direct child of the layer');
});

test('the lens glow is blurred even for a straight-line lamp: the bloom region does not use the bounding box',async()=>{
 const app=await harness();
 const f=app.stage.querySelector('filter#bloom');
 assert.equal(f.getAttribute('filterUnits'),'userSpaceOnUse','a vertical floor-lamp line has a zero-width box that disables an objectBoundingBox filter');
 for(const k of ['x','y'])assert.ok(+f.getAttribute(k)<=-400);
 for(const k of ['width','height'])assert.ok(+f.getAttribute(k)>=800);
});

const READY_BOX=()=>({state:'ready',name:'Sync Box',ip:'192.0.2.20',syncing:false});
const placeBox=(action,body,server)=>{if(action!=='sync-box')return {status:404,body:{}};server.index.sync=body.room;return null};

test('the Lamps panel says the Sync Box is in this room, in its own room only',async()=>{
 const app=await harness({rooms:twoRooms(),syncBox:READY_BOX});await app.advance(100);
 await app.click(app.$('#list-toggle'));await app.advance(0);
 const panel=app.$('#lamp-panel');
 assert.ok(panel.querySelector('[data-box-row]'),'the box is an object of its room');
 assert.equal(panel.querySelector('[data-box-room="ask"]'),null,'nothing to move in its own room');
 assert.match(panel.textContent,/Syncs this room with the screen/);
});

test('another room moves the Sync Box in after a calm confirm, and Sync follows it there',async()=>{
 const app=await harness({rooms:twoRooms(),storage:new Map([['light-studio-room-v1',BEDROOM]]),syncBox:READY_BOX,roomPost:placeBox});
 await app.advance(100);
 assert.ok(app.$('#app').classList.contains('no-sync'),'no Sync settings without the box');
 await app.click(app.$('#list-toggle'));await app.advance(0);
 const panel=app.$('#lamp-panel');
 assert.match(panel.textContent,/In Office/);
 await app.click(panel.querySelector('[data-box-room="ask"]'));
 const confirm=panel.querySelector('.confirm');
 assert.ok(confirm.classList.contains('calm'),'moving is not destructive');
 assert.match(confirm.textContent,/Sync leaves Office/);
 assert.ok(confirm.querySelector('[data-box-room="place"]').classList.contains('primary'));
 assert.deepEqual(app.requests.roomPosts,[],'nothing moves before the confirm');
 await app.click(confirm.querySelector('[data-box-room="place"]'));await app.settle();
 assert.deepEqual(app.requests.roomPosts,[{url:'/api/rooms/sync-box',body:{room:BEDROOM}}]);
 assert.ok(!app.$('#app').classList.contains('no-sync'),'Sync settings and Start/Stop appear in the new room');
 assert.ok(panel.querySelector('[data-box-row]'),'the box is now listed with this room');
 assert.equal(app.requests.box.filter(r=>r.url!=='/api/sync-box').length,0,'a paired box is not paired again');
});

test('cancelling the Sync Box confirm moves nothing',async()=>{
 const app=await harness({rooms:twoRooms(),storage:new Map([['light-studio-room-v1',BEDROOM]]),syncBox:READY_BOX,roomPost:placeBox});
 await app.advance(100);
 await app.click(app.$('#list-toggle'));await app.advance(0);
 const panel=app.$('#lamp-panel');
 await app.click(panel.querySelector('[data-box-room="ask"]'));
 await app.click(panel.querySelector('[data-box-room="cancel"]'));
 assert.equal(panel.querySelector('.confirm'),null);
 assert.deepEqual(app.requests.roomPosts,[]);
});

test('an unpaired Sync Box added to a room opens the connect card there',async()=>{
 const app=await harness({rooms:twoRooms(),storage:new Map([['light-studio-room-v1',BEDROOM]]),roomPost:placeBox,
  syncBox:url=>url.endsWith('/find')?{boxes:[]}:{state:'none'}});
 await app.advance(100);
 await app.click(app.$('#list-toggle'));await app.advance(0);
 const panel=app.$('#lamp-panel');
 assert.match(panel.textContent,/not connected/);
 await app.click(panel.querySelector('[data-box-room="ask"]'));
 await app.click(panel.querySelector('[data-box-room="place"]'));await app.settle();
 assert.equal(app.server.index.sync,BEDROOM);
 assert.ok(panel.hidden,'the lamp panel gives way to the connect card');
 assert.ok(!app.$('#box-card').hidden,'the Sync Box connect card is open in this room');
});

test('a refused Sync Box move keeps the room and says why',async()=>{
 const app=await harness({rooms:twoRooms(),storage:new Map([['light-studio-room-v1',BEDROOM]]),syncBox:READY_BOX,
  roomPost:()=>({status:409,body:{accepted:false,reason:'sync_active'}})});
 await app.advance(100);
 await app.click(app.$('#list-toggle'));await app.advance(0);
 const panel=app.$('#lamp-panel');
 await app.click(panel.querySelector('[data-box-room="ask"]'));
 await app.click(panel.querySelector('[data-box-room="place"]'));await app.settle();
 assert.ok(app.$('#app').classList.contains('no-sync'));
 assert.match(app.$('#toast').textContent,/Not while Sync is on/);
 assert.ok(panel.querySelector('[data-box-room="ask"]'),'the move can be asked again');
});

test('a Sync Box that must connect again offers Reconnect in its own room',async()=>{
 const app=await harness({rooms:twoRooms(),syncBox:url=>url.endsWith('/find')?{boxes:[]}:{state:'unauthorized',name:'Sync Box',ip:'192.0.2.20'}});
 await app.advance(100);
 await app.click(app.$('#list-toggle'));await app.advance(0);
 const panel=app.$('#lamp-panel'),row=panel.querySelector('[data-box-row]');
 assert.match(row.textContent,/Needs connecting again/);
 await app.click(row.querySelector('[data-box-room="connect"]'));await app.settle();
 assert.ok(panel.hidden);assert.ok(!app.$('#box-card').hidden,'the connect card opens');
});

test('the Sync Box lies on the desk of its room only, beside the monitor stand until it is dragged',async()=>{
 const app=await harness({rooms:twoRooms(),syncBox:READY_BOX});await app.advance(100);
 const node=app.stage.querySelector('[data-select="box"]');
 assert.ok(node,'drawn in the Sync Box room');
 assert.equal(node.dataset.led,'ready','the front light shows a ready box');
 assert.equal(app.run('boxX()'),app.run('clampBoxX(environment.screen.x+BOX_GAP)'),'right of the stand');
 assert.equal(app.run('environment.box'),undefined,'no own place until dragged');
 assert.ok(!('syncBox' in app.run('pack()')),'nothing stored for the default place');
 const other=await harness({rooms:twoRooms(),storage:new Map([['light-studio-room-v1',BEDROOM]]),syncBox:READY_BOX});await other.advance(100);
 assert.equal(other.stage.querySelector('[data-select="box"]'),null,'not drawn in a room without it');
});

test('dragging the Sync Box moves it along the desktop and stores its place',async()=>{
 const app=await harness({rooms:twoRooms(),syncBox:READY_BOX});await app.advance(100);
 const start=app.run('boxX()'),x=500+start*3.3,y=740-app.run('environment.height')*3.3-5;
 app.dispatch(app.stage.querySelector('[data-select="box"] .box-body'),'pointerdown',{clientX:x,clientY:y});
 app.dispatch(app.stage,'pointermove',{clientX:x-10*3.3,clientY:y-200});
 app.dispatch(app.stage,'pointerup');await app.settle();await app.advance(2000);
 assert.equal(app.run('boxX()'),start-10,'it follows the pointer in 5 cm steps and stays on the desktop');
 assert.equal(app.posts.at(-1).body.syncBox.x,start-10+100,'the place is saved with the layout');
 assert.ok(app.$('#box-card').hidden,'a drag does not open the card');
 app.run('undo()');assert.equal(app.run('boxX()'),start,'undo puts it back');
 app.dispatch(app.stage.querySelector('[data-select="box"] .box-body'),'pointerdown',{clientX:x,clientY:y});
 app.dispatch(app.stage,'pointermove',{clientX:x+2000,clientY:y});app.dispatch(app.stage,'pointerup');await app.settle();
 const edge=app.run('environment.width')/2-9;
 assert.ok(app.run('boxX()')<=edge,'it cannot leave the desktop');
});

test('a click on the Sync Box opens its card, and another click closes it',async()=>{
 const app=await harness({rooms:twoRooms(),syncBox:READY_BOX});await app.advance(100);
 const body=()=>app.stage.querySelector('[data-select="box"] .box-body');
 await app.click(body());
 assert.ok(!app.$('#box-card').hidden,'the Sync Box card opens');
 assert.match(app.$('#box-card').textContent,/192\.0\.2\.20/);
 await app.click(body());
 assert.ok(app.$('#box-card').hidden,'a second click closes it');
});

test('the front light of the Sync Box follows Sync',async()=>{
 let state=READY_BOX();
 const app=await harness({rooms:twoRooms(),syncBox:url=>url==='/api/sync-box'?state:state});await app.advance(100);
 state={...state,syncing:true};await app.advance(10000);
 assert.equal(app.stage.querySelector('[data-select="box"]').dataset.led,'sync');
 state={state:'offline'};await app.advance(10000);
 assert.equal(app.stage.querySelector('[data-select="box"]').dataset.led,'off');
});

test('Found lists the lamps that can be placed above the unsupported ones',async()=>{
 const id=n=>'device-'+String(n).repeat(32);
 const devices=[discovered({id:id(1),name:'Plug',supported:false,shape:null,zones:null}),discovered({id:id(2),name:'Strip'})];
 const app=await harness({discovery:{connected:true,devices,providers:{}}});
 await app.click(app.$('#list-toggle'));
 const rows=[...app.$('#lamp-panel').querySelectorAll('.lp-row.found')];
 assert.ok(rows[0].querySelector('[data-place]'),'the supported lamp is first');
 assert.match(rows[1].textContent,/Unsupported/);
});

test('the Lamps panel lists lamps only: no desk or monitor rows',async()=>{
 const app=await harness({rooms:twoRooms(),storage:new Map([['light-studio-room-v1',BEDROOM]])});await app.advance(100);
 await app.click(app.$('#list-toggle'));
 const panel=app.$('#lamp-panel');
 assert.equal(panel.querySelector('[data-list-lamp="desk"],[data-list-lamp="monitor"]'),null);
});
