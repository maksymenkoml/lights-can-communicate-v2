// Light Studio client contracts. The real index.html and app.js run in a vm with a
// small DOM (HTML/SVG parser, selector engine, events) and a fake clock and server.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const STATIC=path.join(__dirname,'../experiments/setup-studio/static');
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
 const result=structuredClone(layout);result.version=4;
 if(result.syncZone==null)delete result.syncZone;
 for(const row of result.devices){if(row.enabled!==false)delete row.enabled;if(row.syncRole!=='own')delete row.syncRole;row.orientation??='horizontal';}
 return result;
}
const discovered=(overrides={})=>({id:'device-'+'a'.repeat(32),provider:'govee',name:'Third <lamp>',model:'H6076',shape:'floor',zones:7,supported:true,online:true,selectable:true,ambiguous:false,layout_id:null,height_cm:136,width_cm:null,...overrides});
const pilotOk=(overrides={})=>({version:1,connected:true,mode:'sync',sync:{state:'active',reason:'frames_received',blockers:[]},lamps:{},...overrides});
const lit=(colors,extra={})=>({online:true,on:true,brightness:80,colors,source:'sync-output',stale:false,included:true,...extra});
const json=(app,source)=>JSON.parse(app.run('JSON.stringify('+source+')'));
const response=(status,body)=>({status,ok:status>=200&&status<300,json:async()=>structuredClone(body)});
async function settle(){for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));}

async function harness({layout=saved(),revision='r1',storage=new Map(),pilot=pilotOk(),discovery={connected:true,devices:[discovered()],providers:{govee:'ready',hue:'ready'}},onPost,layoutGet,hidden=false,reducedMotion=false}={}){
 let now=0,timerId=0;const timers=new Map();
 const clock={setTimeout:(fn,delay=0)=>{timers.set(++timerId,{fn,at:now+Math.max(0,delay),delay});return timerId},clearTimeout:id=>timers.delete(id)};
 const {document}=createDom(clock);document.hidden=hidden;
 const server={layout:layout&&canonical(layout),revision};
 const posts=[],requests={layout:0,discovery:[],pilot:[]};
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
   if(url==='/api/pilot'){requests.pilot.push(now);const value=typeof pilot==='function'?pilot(requests.pilot.length):pilot;if(value instanceof Error)throw value;return response(200,await value)}
   if(url==='/api/discovery'){requests.discovery.push(now);const value=typeof discovery==='function'?discovery(requests.discovery.length):discovery;if(value instanceof Error)throw value;return response(200,value)}
   assert.equal(url,'/api/layout');
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
 assert.deepEqual(JSON.parse(app.run('JSON.stringify(pack())')),{...canonical(remote),syncZone:null});
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

test('edits autosave once after 300 ms with If-Match and version 4, undo saves too',async()=>{
 const app=await harness();
 app.lamp('play-left').focus();await app.press(app.lamp('play-left'),'ArrowRight');
 assert.equal(app.$('#draft-status').dataset.state,'pending');
 await app.advance(299);assert.equal(app.posts.length,0);
 await app.advance(1);assert.equal(app.posts.length,1);
 assert.equal(app.posts[0].ifMatch,'"r1"');assert.equal(app.posts[0].body.version,4);
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
 assert.deepEqual(app.posts[0].body,{...canonical(draft),syncZone:null});
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

test('Found shows unavailable lamps with their reason and hides placed ones',async()=>{
 const id=n=>'device-'+String(n).repeat(32);
 const devices=[discovered({id:id(1),name:'Gone',online:false}),discovered({id:id(2),name:'Odd',supported:false,shape:null,zones:null}),
  discovered({id:id(3),name:'Twin',ambiguous:true}),discovered({id:id(4),name:'Role',layout_id:'govee-left'}),discovered({id:id(5),name:'Placed'})];
 const remote=saved();remote.devices.push({id:id(5),name:'Placed',model:'H6076',shape:'floor',zones:7,scale:80,positionCm:{x:200,height:60}});
 const app=await harness({layout:remote,discovery:{connected:true,devices,providers:{}}});
 await app.click(app.$('#list-toggle'));
 const rows=[...app.$('#lamp-panel').querySelectorAll('.lp-row.found')].map(r=>r.textContent);
 assert.deepEqual(rows,['GoneH6076 · 7 zonesOffline','OddH6076Unsupported','TwinH6076 · 7 zonesAmbiguous']);
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
 const app=await harness({pilot:pilotOk({lamps:{'govee-left':lit([[255,0,0]])}})});
 await app.click(app.$('#list-toggle'));
 await app.click(app.$('#lamp-panel').querySelector('[data-place]'));
 assert.equal(app.$('#lamp-panel').hidden,true);assert.ok(app.$('#app').classList.contains('placing'));
 assert.match(app.$('#placement-hint').textContent,/Click in the room to place Third <lamp>/);
 app.dispatch(app.stage,'pointerdown',{clientX:500+100*3.3,clientY:600});await app.settle();
 assert.ok(!app.$('#app').classList.contains('placing'));
 await app.advance(300);
 const row=app.posts.at(-1).body.devices.at(-1);
 assert.deepEqual(row,{id:discovered().id,scale:80,positionCm:{x:200,height:60},orientation:'horizontal',shape:'floor',name:'Third <lamp>',model:'H6076',zones:7});
 const placed=app.lamp(discovered().id);
 assert.equal(placed.querySelector('[data-sync-power]').getAttribute('opacity'),'0','not connected rows stay neutral');
 await app.click(app.$('#list-toggle'));
 assert.match(app.$('#lamp-panel').querySelector('[data-list-lamp="'+discovered().id+'"]').textContent,/Not connected/);
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
 assert.deepEqual(app.posts.at(-1).body.devices[4],{id:'play-center',scale:50,positionCm:{x:100,height:40},orientation:'horizontal',enabled:false});
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
 assert.equal(menu.querySelector('.card-name').readOnly,true,'role names are canonical');
 assert.equal(menu.querySelector('[data-menu="blink"]').disabled,true);
 assert.equal(menu.querySelectorAll('.seg button').length,2,'only Sync and Own light in this version');
 await app.click(menu.querySelector('[data-menu="role-own"]'));
 await app.advance(300);
 assert.equal(app.posts.at(-1).body.devices[5].syncRole,'own');
 assert.equal(app.$('#object-menu').querySelector('[data-menu="role-own"]').getAttribute('aria-pressed'),'true');
 await app.click(app.$('#object-menu').querySelector('[data-menu="role-sync"]'));await app.advance(300);
 assert.equal(app.posts.at(-1).body.devices[5].syncRole,undefined);
 await app.click(app.$('#object-menu').querySelector('[data-menu="remove"]'));
 assert.match(app.$('#object-menu').querySelector('.card-confirm').textContent,/Remove Hue Play Right\?/);
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

test('pilot polling: 200 ms after each answer, 1000 ms deadline, 1500 ms freshness, paused while hidden',async()=>{
 let hang=false;
 const app=await harness({pilot:()=>hang?new Promise(()=>{}):pilotOk()});
 assert.deepEqual(app.requests.pilot,[0]);
 await app.advance(199);assert.equal(app.requests.pilot.length,1);
 await app.advance(1);assert.deepEqual(app.requests.pilot,[0,200]);
 await app.advance(400);assert.deepEqual(app.requests.pilot,[0,200,400,600]);
 hang=true;await app.advance(200);assert.equal(app.requests.pilot.at(-1),800);
 assert.equal(app.$('#sync-status span').textContent,'Syncing');
 await app.advance(999);assert.equal(app.requests.pilot.length,5);
 await app.advance(1);assert.equal(app.$('#sync-status span').textContent,'Sync status unknown');
 await app.advance(200);assert.equal(app.requests.pilot.at(-1),2000);
 await app.visible(false);const count=app.requests.pilot.length;
 await app.advance(5000);assert.equal(app.requests.pilot.length,count);
 hang=false;await app.visible(true);assert.equal(app.requests.pilot.length,count+1);
 assert.equal(app.$('#sync-status span').textContent,'Syncing');
 const hiddenStart=await harness({hidden:true});await hiddenStart.advance(3000);assert.equal(hiddenStart.requests.pilot.length,0);
});

test('a fresh answer expires after 1500 ms even when the next poll is delayed',async()=>{
 const app=await harness();
 assert.equal(app.$('#sync-status span').textContent,'Syncing');
 // A throttled background timer: the next poll does not run, the freshness guard still does.
 for(const [id,timer] of app.timers)if(timer.delay===200)app.timers.delete(id);
 await app.advance(1499);assert.equal(app.$('#sync-status span').textContent,'Syncing');
 await app.advance(1);assert.equal(app.$('#sync-status span').textContent,'Sync status unknown');
 assert.equal(app.requests.pilot.length,1);
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
  const app=await harness({pilot:pilotOk({sync})});
  assert.equal(app.$('#sync-status span').textContent,label,JSON.stringify(sync));
  assert.equal(app.$('#sync-status').dataset.level,level);
 }
 const paused=await harness({pilot:{version:1,connected:false,mode:'paused',sync:{state:'paused'},lamps:{}}});
 assert.equal(paused.$('#sync-status span').textContent,'Not syncing');
 const disconnected=await harness({pilot:{version:1,connected:false,mode:'unknown',sync:{state:'unknown'},lamps:{}}});
 assert.equal(disconnected.$('#sync-status span').textContent,'Sync status unknown');
});

test('blockers name the lamp, badge it unlit, and the pill selects it',async()=>{
 const app=await harness({pilot:pilotOk({sync:{state:'active',reason:'frames_received',blockers:[{role:'left',code:'owner_off'}]},lamps:{'govee-left':lit(Array(7).fill([255,0,0]))}})});
 assert.equal(app.$('#sync-status span').textContent,'Govee Left off in HomeKit');
 const badge=app.lamp('govee-left').querySelector('[data-badge]');
 assert.equal(badge.getAttribute('visibility'),'visible');assert.equal(badge.dataset.tooltip,'Off in HomeKit');
 assert.equal(app.lamp('govee-left').querySelector('[data-sync-power]').getAttribute('opacity'),'0');
 await app.click(app.$('#sync-status'));assert.equal(app.run('selected'),'govee-left');
 const many=await harness({pilot:pilotOk({sync:{state:'active',blockers:[{role:'hue_left',code:'native_stale'},{role:'strip',code:'color_fenced'},{role:'nobody',code:'x'},{role:'right',code:'future_code'}]}})});
 assert.equal(many.$('#sync-status span').textContent,'3 lamps not lit');
 assert.equal(many.lamp('govee-right').querySelector('[data-badge]').dataset.tooltip,'Not ready');
});

test('live colours repaint existing nodes without rebuilding the room or saving',async()=>{
 let answer=pilotOk({lamps:{'play-left':lit([[255,0,0]]),'govee-left':lit([[0,0,255],[0,0,255],[0,0,255],[0,255,0],[0,255,0],[0,255,0],[255,255,255]]),
  'hue-monitor':lit(Array.from({length:9},(_,i)=>[i*20,0,0])),'play-center':lit([[9,9,9]],{on:false}),'play-right':lit([[9,9,9]],{stale:true}),'govee-right':lit(Array(7).fill([1,2,3]),{included:false})}});
 const unverified=pilotOk({lamps:{'play-left':lit([[255,0,0]],{source:'unknown'})}});
 const app=await harness({pilot:()=>answer});
 await app.click(app.$('#sync-tool'));
 const writes=app.stage.writes,node=app.lamp('play-left');
 assert.equal(node.querySelector('[data-beam]').style.getPropertyValue('--beam'),'#ff0000');
 assert.equal(node.querySelector('[data-sync-power]').getAttribute('opacity'),'1');
 const floorStops=[...app.lamp('govee-left').querySelectorAll('stop')].map(s=>s.getAttribute('stop-color'));
 assert.equal(floorStops[0],'#ffffff');assert.equal(floorStops.at(-1),'#0000ff','zones run bottom to top');
 for(const id of ['play-center','play-right','govee-right'])assert.equal(app.lamp(id).querySelector('[data-sync-power]').getAttribute('opacity'),'0',id);
 assert.equal(app.stage.querySelector('#screen-image').getAttribute('opacity'),'1');
 answer=pilotOk({lamps:{'play-left':lit([[0,128,0]])}});
 await app.advance(200);
 assert.equal(node.querySelector('[data-beam]').style.getPropertyValue('--beam'),'#008000');
 assert.equal(app.stage.querySelector('[data-wash-for="play-left"]').style.getPropertyValue('--beam'),'#008000');
 assert.equal(app.lamp('govee-left').querySelector('[data-sync-power]').getAttribute('opacity'),'0');
 assert.equal(app.stage.querySelector('#screen-image').getAttribute('opacity'),'0','dark screen without strip colours');
 assert.equal(app.stage.writes,writes,'the stage markup was not rebuilt');
 assert.equal(app.lamp('play-left'),node);
 answer=pilotOk({sync:{state:'off'},lamps:{'play-left':lit([[0,128,0]],{source:'scene-output'})}});
 await app.advance(200);
 assert.equal(app.$('#sync-status span').textContent,'Not syncing');
 assert.equal(node.querySelector('[data-sync-power]').getAttribute('opacity'),'1','scene colours stay visible');
 assert.equal(app.stage.writes,writes);
 answer=unverified;await app.advance(200);
 assert.equal(node.querySelector('[data-sync-power]').getAttribute('opacity'),'0','colours without a verified output source are not light');
 await app.advance(2000);assert.equal(app.posts.length,0);
});

test('a selected lamp shows rays coloured by its emitted zones',async()=>{
 const app=await harness({pilot:pilotOk({lamps:{'govee-left':lit([[10,0,0],[20,0,0],[30,0,0],[40,0,0],[50,0,0],[60,0,0],[70,0,0]])}})});
 await app.click(app.$('#sync-tool'));
 app.run('select("govee-left")');await app.settle();
 const rays=[...app.stage.querySelectorAll('#sync-links .ray')];
 assert.ok(rays.length>0&&rays.length<7,'zones below the Sync zone have no ray');
 for(const ray of rays){const i=+ray.dataset.k.split(':r')[1];assert.equal(ray.getAttribute('stroke'),'#'+((i+1)*10).toString(16).padStart(2,'0')+'0000')}
 app.run('select("monitor")');await app.settle();
 assert.equal(app.stage.querySelectorAll('#sync-links .ray').length,rays.length,'the monitor shows every lit lamp');
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

test('the desk, monitor and zone are edited in place and saved in layout units',async()=>{
 const app=await harness();
 app.run('select("desk")');app.run('changeField("height",90)');
 await app.advance(300);
 const body=app.posts.at(-1).body;
 assert.deepEqual(body.desk,{width:160,height:90});assert.deepEqual(body.devices[2].positionCm,{x:100,height:115},'the monitor rides on the tabletop');
 app.run('select("monitor")');app.run('changeField("inches",34)');await app.advance(300);
 assert.equal(app.posts.at(-1).body.monitorInches,34);
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
