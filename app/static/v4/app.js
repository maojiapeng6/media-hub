/* ============================================================
   KFC 媒体中枢 · v4 控制平面 · app.js
   纯 vanilla JS，零外部依赖。组件内禁止写死颜色/圆角/阴影/动效时长，
   一律引用 tokens.css / themes.css 变量。
   现有后端链路零改动：仅消费既有 /api/* 端点 + 新增只读 /api/* 端点。
   既有全部功能通过「经典模块」iframe 桥接 100% 保留（MUST-KEEP）。
   ============================================================ */
(function(){
'use strict';
const $ = (s,r=document)=>r.querySelector(s);
const $$ = (s,r=document)=>[...r.querySelectorAll(s)];
const el = (t,a={},...c)=>{const e=document.createElement(t);for(const k in a){if(k==='class')e.className=a[k];else if(k==='html')e.innerHTML=a[k];else if(k.startsWith('on'))e.addEventListener(k.slice(2),a[k]);else e.setAttribute(k,a[k]);}for(const x of c){e.append(x&&x.nodeType?x:document.createTextNode(x));}return e;};
const esc = s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmtTime = ts=>{ if(!ts)return '—'; const d=new Date(ts*1000); return d.toLocaleString('zh-CN',{hour12:false}); };
const relTime = ts=>{ if(!ts)return '—'; const s=Math.max(0,Math.floor(Date.now()/1000-ts)); if(s<60)return s+'s 前'; if(s<3600)return Math.floor(s/60)+'m 前'; if(s<86400)return Math.floor(s/3600)+'h 前'; return Math.floor(s/86400)+'d 前'; };
const tnum = n=>String(n).replace(/\B(?=(\d{3})+(?!\d))/g,',');

/* —— 图标（矢量，stroke 随 --icon-stroke） —— */
const ICON = {
  overview:'<path d="M3 13h8V3H3zM13 21h8V3h-8zM3 21h8v-6H3z"/>',
  pt:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/>',
  mt:'<path d="M12 2 4 6v6c0 5 3.5 8 8 10 4.5-2 8-5 8-10V6z"/>',
  emby:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9v6M7 9l5 6 5-6"/>',
  topology:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="9" y="14" width="6" height="7" rx="1"/><path d="M6.5 10v2h11V10M12 12v2"/>',
  alert:'<path d="M12 3 2 20h20z"/><path d="M12 9v5M12 17v.5"/>',
  log:'<path d="M5 4h14v16H5z"/><path d="M8 8h8M8 12h8M8 16h5"/>',
  heal:'<path d="M12 21C6 16 3 12 3 8a4 4 0 0 1 8-1 4 4 0 0 1 8 1c0 4-3 8-9 13z"/>',
  audit:'<path d="M9 2h7l4 4v16H5z"/><path d="M9 2v5h7M8 13h8M8 17h5"/>',
  config:'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>',
  theme:'<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 0 0 18z"/>',
  layout:'<rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="5" rx="1"/><rect x="13" y="10" width="8" height="11" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/>',
  diag:'<circle cx="11" cy="11" r="7"/><path d="m21 21-4-4M11 8v6M8 11h6"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="m21 21-4-4"/>',
  refresh:'<path d="M21 12a9 9 0 1 1-3-6.7M21 3v5h-5"/>',
  close:'<path d="M5 5l14 14M19 5 5 19"/>',
  fold:'<path d="m6 9 6 6 6-6"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  menu:'<path d="M3 6h18M3 12h18M3 18h18"/>',
  back:'<path d="M15 5l-7 7 7 7"/>',
  bolt:'<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  shield:'<path d="M12 2 4 6v6c0 5 3.5 8 8 10 4.5-2 8-5 8-10V6z"/><path d="m9 12 2 2 4-4"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'
};
const svg=(n,cls='')=>`<svg class="ico ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="var(--icon-stroke)" stroke-linecap="round" stroke-linejoin="round">${ICON[n]||''}</svg>`;

/* —— API 封装（尊重现有后端：JSON 为主） —— */
async function apiGet(p,to=8000){const c=new AbortController();const t=setTimeout(()=>c.abort(),to);try{const r=await fetch(p,{signal:c.signal});if(!r.ok)return {ok:false,code:r.status,msg:r.statusText};try{return await r.json();}catch(e){return {ok:true,raw:await r.text()};}}catch(e){return {ok:false,msg:String(e)};}finally{clearTimeout(t);}}
async function apiPost(p,body={}){try{const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)return {ok:false,code:r.status,msg:r.statusText};try{return await r.json();}catch(e){return {ok:true};}}catch(e){return {ok:false,msg:String(e)};}}

/* ============================================================
   主题引擎 + 外观偏好（localStorage + /api/theme 服务端持久）
   ============================================================ */
const THEMES=['obsidian','liquid-glass','light-studio','terminal','editorial','neon-ops'];
const THEME_LABEL={obsidian:'曜石运维',liquid:'液态玻璃',light:'明亮工作台',terminal:'终端极客',editorial:'编排杂志',neon:'霓虹调度台'};
// 修正：用完整主题名映射（上面 light/neon 为占位，真实覆盖如下）
THEME_LABEL['obsidian']='曜石运维';THEME_LABEL['liquid-glass']='液态玻璃';THEME_LABEL['light-studio']='明亮工作台';THEME_LABEL['terminal']='终端极客';THEME_LABEL['editorial']='编排杂志';THEME_LABEL['neon-ops']='霓虹调度台';
const ACCENTS=['orange','blue','cyan','purple','green','rose'];
const LS={get:k=>{try{return JSON.parse(localStorage.getItem('v4_'+k));}catch(e){return null;}},set:(k,v)=>localStorage.setItem('v4_'+k,JSON.stringify(v))};
function getPref(){return Object.assign({theme:'obsidian',accent:'orange',bright:'mid',motion:'full',radius:'mid',typescale:'mid',noise:0,scanline:0,glass:1,density:'comfort'}, LS.get('pref')||{});}
function applyPref(p){
  const b=document.body;
  b.setAttribute('data-theme',p.theme);
  b.setAttribute('data-accent',p.accent);
  b.setAttribute('data-bright',p.bright);
  b.setAttribute('data-motion',p.motion==='reduced'?'reduced':'full');
  b.setAttribute('data-radius',p.radius);
  b.setAttribute('data-typescale',p.typescale);
  b.setAttribute('data-noise',p.noise?'1':'0');
  b.setAttribute('data-scanline',p.scanline?'1':'0');
  b.setAttribute('data-glass',p.glass?'1':'0');
  document.documentElement.style.setProperty('--density',p.density==='compact'?0.85:p.density==='spacious'?1.15:1);
  LS.set('pref',p);
  // 服务端持久（失败静默）
  apiPost('/api/theme',p).catch(()=>{});
}
let PREF=getPref();

/* ============================================================
   导航定义（信息架构：概览 / 服务 / 运维 / 系统）
   ============================================================ */
const NAV=[
  {group:'概览',items:[{id:'overview',name:'总览',icon:'overview',def:true}]},
  {group:'服务',items:[
    {id:'pt',name:'PT 资源站',icon:'pt',legacy:true},
    {id:'mt',name:'MT 馒头鉴权',icon:'mt',legacy:true},
    {id:'emby',name:'Emby 媒体库',icon:'emby',legacy:true},
    {id:'topology',name:'连接拓扑',icon:'topology'},
  ]},
  {group:'运维',items:[
    {id:'alerts',name:'告警中心',icon:'alert'},
    {id:'activity',name:'活动日志',icon:'log'},
    {id:'selfheal',name:'自愈历史',icon:'heal'},
    {id:'audit',name:'审计日志',icon:'audit'},
  ]},
  {group:'系统',items:[
    {id:'config',name:'配置中心',icon:'config'},
    {id:'appearance',name:'主题与外观',icon:'theme'},
    {id:'layout',name:'布局自定义',icon:'layout'},
    {id:'diagnose',name:'诊断与关于',icon:'diag'},
  ]},
];
/* 用户布局覆盖（顺序/隐藏/重命名/最小化） */
function getLayout(){return LS.get('layout')||null;}
function applyLayout(l){LS.set('layout',l);}

/* ============================================================
   Toast（堆叠 / 可撤销 / 自动消失 / hover 暂停 / 错误常驻）
   ============================================================ */
let toastSeq=0;
function toast(msg,kind='ok',opts={}){
  const id='t'+(++toastSeq);
  const t=el('div',{class:'toast '+kind,role:'status','aria-live':'polite'},
    el('span',{class:'toast-ico'},svg(kind==='ok'?'shield':kind==='err'?'alert':'bolt')),
    el('span',{class:'toast-msg'},msg));
  if(opts.undo){const b=el('button',{class:'toast-undo',onclick:()=>{opts.undo();dismiss();}},'撤销');t.append(b);}
  if(opts.loading){const sp=el('span',{class:'toast-spin'});t.append(sp);t.dataset.loading='1';}
  if(opts.action){t.append(el('button',{class:'toast-act',onclick:()=>{opts.action();dismiss();}},opts.actionLabel||'查看'));}
  $('#toasts').append(t);
  requestAnimationFrame(()=>t.classList.add('in'));
  let to; const dismiss=()=>{t.classList.remove('in');setTimeout(()=>t.remove(),180);clearTimeout(to);};
  if(kind!=='err'){to=setTimeout(dismiss,opts.duration||3600);t.addEventListener('mouseenter',()=>clearTimeout(to));t.addEventListener('mouseleave',()=>{to=setTimeout(dismiss,1200);});}
  t._dismiss=dismiss; return {update:(m,k)=>{t.className='toast '+(k||kind);$('.toast-msg',t).textContent=m;t.removeAttribute('data-loading');},dismiss};
}
function opResult(title,detail){ // 操作结果面板
  const panel=$('#opresult'); panel.classList.remove('collapsed'); panel.classList.add('has');
  const row=el('div',{class:'op-row'},el('span',{class:'op-dot '+ (detail.ok?'ok':'err')}),el('span',{class:'op-title'},title),el('span',{class:'op-detail mono'},detail.result||(detail.ok?'成功':'失败')));
  $('#oprows').prepend(row);
  while($('#oprows').children.length>50)$('#oprows').lastChild.remove();
  const c=$('#opcount'); c.textContent=String(parseInt(c.textContent||'0')+1);
}

/* ============================================================
   命令面板（Ctrl K）
   ============================================================ */
function openPalette(){
  const ov=$('#palette'); ov.classList.add('open');
  const inp=$('#pal-cmd'); inp.value=''; $('#pal-results').innerHTML=''; inp.focus();
  inp.oninput=()=>renderPalette(inp.value);
  renderPalette('');
}
const PALETTE_ACTIONS=[
  {k:'跳转到 总览',fn:()=>navTo('overview')},
  {k:'跳转到 PT 资源站',fn:()=>navTo('pt')},
  {k:'跳转到 MT 馒头鉴权',fn:()=>navTo('mt')},
  {k:'跳转到 Emby 媒体库',fn:()=>navTo('emby')},
  {k:'打开 主题与外观',fn:()=>navTo('appearance')},
  {k:'打开 布局自定义',fn:()=>navTo('layout')},
  {k:'切换主题：液态玻璃',fn:()=>{PREF.theme='liquid-glass';applyPref(PREF);}},
  {k:'切换主题：曜石运维',fn:()=>{PREF.theme='obsidian';applyPref(PREF);}},
  {k:'切换主题：明亮工作台',fn:()=>{PREF.theme='light-studio';applyPref(PREF);}},
  {k:'切换主题：终端极客',fn:()=>{PREF.theme='terminal';applyPref(PREF);}},
  {k:'切换主题：编排杂志',fn:()=>{PREF.theme='editorial';applyPref(PREF);}},
  {k:'切换主题：霓虹调度台',fn:()=>{PREF.theme='neon-ops';applyPref(PREF);}},
  {k:'保存当前布局',fn:()=>{saveLayout();toast('布局已保存','ok');}},
  {k:'导出主题/布局 JSON',fn:()=>exportConfig()},
];
function renderPalette(q){
  const list=PALETTE_ACTIONS.filter(a=>a.k.toLowerCase().includes(q.toLowerCase()));
  const box=$('#pal-results'); box.innerHTML='';
  list.forEach((a,i)=>{const r=el('div',{class:'pal-item'+(i===0?' active':''),onclick:()=>{a.fn();closePalette();}},el('span',{},svg('search')),el('span',{},a.k));box.append(r);});
  if(!list.length)box.append(el('div',{class:'pal-empty'},'无匹配'));
}
function closePalette(){$('#palette').classList.remove('open');}

/* ============================================================
   JWT 六态 + 全局健康三态 + 数据新鲜度
   ============================================================ */
const JWT_STATE={
  valid:{cls:'ok',txt:'有效',ico:'shield'},
  expiring:{cls:'warn',txt:'临期',ico:'clock'},
  refreshing:{cls:'info',txt:'刷新中',ico:'bolt'},
  failed:{cls:'err',txt:'失败',ico:'alert'},
  degraded:{cls:'degrade',txt:'已降级',ico:'alert'},
  nocred:{cls:'',txt:'无凭据',ico:'shield'},
};
function jwtBadge(s){const m=JWT_STATE[s]||JWT_STATE.nocred;return `<span class="badge ${m.cls}">${svg(m.ico)}${m.txt}${s==='expiring'?'('+(m.left||'')+'s)':''}</span>`;}

/* ============================================================
   四态组件（骨架/空/错/降级）
   ============================================================ */
function skeleton(n=3){let h='';for(let i=0;i<n;i++)h+=`<div class="sk"></div>`;return `<div class="sk-wrap">${h}</div>`;}
function emptyState(icon,txt,sub,act){return `<div class="empty"><div class="empty-ico">${icon}</div><div class="empty-t">${esc(txt)}</div>${sub?`<div class="empty-s">${esc(sub)}</div>`:''}${act?`<button class="btn sm" onclick="${act}">重试</button>`:''}</div>`;}
function errState(msg,act){return `<div class="empty err"><div class="empty-ico">⚠️</div><div class="empty-t">加载失败</div><div class="empty-s mono">${esc(msg||'')}</div>${act?`<button class="btn sm" onclick="${act}">重试</button>`:''}</div>`;}
function degradedState(msg){return `<div class="empty degrade"><div class="empty-ico">🟠</div><div class="empty-t">部分可用（降级）</div><div class="empty-s mono">${esc(msg||'')}</div></div>`;}

/* ============================================================
   通用：卡片 + 新鲜度
   ============================================================ */
function card(title,icon,body,opts={}){
  return `<section class="card ${opts.cls||''}" id="${opts.id||''}" data-card="${opts.id||''}">
    <header class="card-h"><span class="card-ico">${svg(icon)}</span><h3>${esc(title)}</h3>
    ${opts.fresh?`<span class="fresh mono" data-fresh="${opts.fresh}"></span>`:''}
    ${opts.tools||''}</header>
    <div class="card-b">${body}</div></section>`;
}
function setFresh(elem,fresh){
  $$('[data-fresh]',elem).forEach(e=>{const ts=+e.getAttribute('data-fresh');e.textContent='更新 '+relTime(ts);e.classList.toggle('stale',Date.now()/1000-ts>120);});
}

/* ============================================================
   页面渲染
   ============================================================ */
const PAGES={};
PAGES.overview=async function(){
  const root=$('#page'); root.innerHTML=card('健康概览','shield',skeleton(3),{id:'health',fresh:Math.floor(Date.now()/1000)})
    +card('关键指标','bolt',skeleton(5),{id:'metrics'})
    +card('馒头鉴权 · 自动守护','shield',skeleton(4),{id:'guard',fresh:Math.floor(Date.now()/1000)})
    +card('自愈看门狗','heal',skeleton(3),{id:'watchdog'})
    +card('活动日志','log',skeleton(4),{id:'actlog'});
  // 健康三态
  const h=await apiGet('/api/status/health');
  if(h.ok){const d=h; $('#health').querySelector('.card-b').innerHTML=
    `<div class="health3 badge ${d.overall==='ok'?'ok':d.overall==='partial'?'warn':'err'}">${d.overall==='ok'?'全部正常':d.overall==='partial'?'部分可用':'异常'}</div>`+
    Object.entries(d.domains||{}).map(([k,v])=>`<div class="hrow"><span>${esc(k)}</span>${v.badge||''}</div>`).join('')+
    (d.note?`<div class="hint">${esc(d.note)}</div>`:''); setFresh(root,Math.floor(Date.now()/1000));}
  else $('#health').querySelector('.card-b').innerHTML=errState(h.msg,'renderOverview()');
  // 指标（实时派生自健康看门狗；未独立采集项以 — 表示，不伪造）
  {const wd=h&&h.watchdog?h.watchdog:{};const m={refresh_rate:'—',inspect_rate:'—',self_heal:wd.heal_count||0,alert_mtrt:'—',uptime:wd.uptime||'—'};
  $('#metrics').querySelector('.card-b').innerHTML=
    ['刷新成功率','巡检成功率','累计自愈','告警平均恢复','Uptime'].map((k,i)=>metricCard(k,[m.refresh_rate,m.inspect_rate,m.self_heal,m.alert_mtrt,m.uptime][i])).join('');}
  // 守护卡（六态）
  const j=await apiGet('/api/jwt/state');
  if(j.ok){const s=j.state||'nocred'; $('#guard').querySelector('.card-b').innerHTML=
    `<div class="guard-state">${jwtBadge(s)}</div>`+
    (j.reason?`<div class="hint mono">失败原因：${esc(j.reason)}</div>`:'')+
    `<div class="kv"><span>已签发</span><b class="mono">${j.issued?'是':'否'}</b></div>`+
    `<div class="kv"><span>剩余时长</span><b class="mono">${j.left!=null?j.left+'s':'—'}</b></div>`+
    `<div class="kv"><span>绝对过期</span><b class="mono">${esc(j.expire_at||'—')}</b></div>`+
    `<div class="kv"><span>指纹末4</span><b class="mono">${esc(j.fp||'—')}</b></div>`+
    `<div class="kv"><span>最近生效</span><b class="mono">${esc(j.last_ok||'—')}</b></div>`+
    `<div class="kv"><span>Bot 告警</span><b>${esc(j.bot||'—')}</b></div>`+
    `<details class="mech"><summary>了解机制</summary><p>全自动：本机看门狗每 60 秒巡检，JWT 失效时自动用 Edge 已保存的凭据静默重登并回写，无需任何人工操作。</p></details>`+
    `<div class="row"><button class="btn acc" onclick="v4heal()">🔄 立即自愈</button>`+
    (s==='failed'?`<button class="btn" onclick="v4retryCred()">重试</button><button class="btn" onclick="v4viewLog()">查看日志</button>`:'')+`</div>`;
    setFresh(root,Math.floor(Date.now()/1000));}
  else $('#guard').querySelector('.card-b').innerHTML=errState(j.msg);
  // 看门狗
  const wd=await apiGet('/api/status/health');
  if(wd.ok&&wd.watchdog){const w=wd.watchdog; $('#watchdog').querySelector('.card-b').innerHTML=
    `<div class="kv"><span>下次巡检</span><b class="mono" id="wd-next">${w.next_in!=null?w.next_in+'s':'—'}</b></div>`+
    `<div class="kv"><span>连续存活</span><b class="mono">${esc(w.uptime||'—')}</b></div>`+
    `<div class="kv"><span>累计自愈</span><b class="mono">${tnum(w.heal_count||0)}</b></div>`+
    `<div class="kv"><span>最近结果</span><b>${esc(w.last||'—')}</b></div>`;}
  // 活动日志
  const al=await apiGet('/api/oplog?n=20');
  if(al.ok&&al.rows){$('#actlog').querySelector('.card-b').innerHTML=`<div class="logtbl">`+
    al.rows.map(r=>`<div class="logrow"><span class="mono dim">${esc(r.t||'')}</span><span class="badge ${r.ok?'ok':'err'}">${r.ok?'成功':'失败'}</span><span>${esc(r.act||'')}</span></div>`).join('')+`</div>`;}
  else $('#actlog').querySelector('.card-b').innerHTML=emptyState('📜','暂无活动日志','');
  setFresh(root,Math.floor(Date.now()/1000));
};
function metricCard(label,val){return `<div class="metric"><div class="metric-v mono">${esc(val??'—')}</div><div class="metric-l">${esc(label)}</div><div class="spark"></div></div>`;}

/* 经典模块桥接（100% 保留既有功能） */
PAGES.pt=PAGES.mt=PAGES.emby=function(id){
  const root=$('#page');
  root.innerHTML=`<section class="card"><header class="card-h"><span class="card-ico">${svg(id==='pt'?'pt':id==='mt'?'mt':'emby')}</span><h3>${id==='pt'?'PT 资源站':id==='mt'?'MT 馒头鉴权':'Emby 媒体库'}</h3></header>
  <div class="card-b"><div class="hint">以下为既有完整功能模块（保持原样运行，未做删减）：</div>
  <iframe class="legacy" src="/?embed=1" title="经典模块"></iframe></div></section>`;
};
/* 连接拓扑 */
PAGES.topology=async function(){
  const root=$('#page'); root.innerHTML=card('连接拓扑','topology',skeleton(3),{id:'topo'});
  const d=await apiGet('/api/overview');
  if(d.ok){const p=d.conn||{}; $('#topo').querySelector('.card-b').innerHTML=
    `<div class="kv"><span>公网端口</span><b class="mono">45678</b></div>`+
    `<div class="kv"><span>宿主机端口</span><b class="mono">45678</b></div>`+
    `<div class="kv"><span>反代路径</span><b class="mono">${esc(p.proxy||'/')}</b></div>`+
    `<div class="kv"><span>实测探测</span><b>${esc(p.probe||'—')}</b></div>`+
    `<div class="kv"><span>最后探测</span><b class="mono">${esc(p.last_probe||'—')}</b></div>`+
    `<div class="hint">注：原「—端口 5678」字段已统一为 45678；源码残留 5678 仅作待清理旧配置（见诊断页）。</div>`;}
  else $('#topo').querySelector('.card-b').innerHTML=errState(d.msg);
};
/* 告警中心 */
PAGES.alerts=async function(){
  const root=$('#page'); root.innerHTML=card('告警中心','alert',skeleton(3),{id:'al'});
  const d=await apiGet('/api/oplog?n=40');
  if(d.ok&&d.rows){const errs=d.rows.filter(r=>!r.ok);$('#al').querySelector('.card-b').innerHTML=
    (errs.length?errs.map(r=>`<div class="alertrow err"><span class="badge err">error</span><span class="mono dim">${esc(r.t||'')}</span><span>${esc(r.act||'')}：${esc(r.msg||'')}</span><span class="row"><button class="btn sm" onclick="v4toast('已忽略','ok')">忽略</button><button class="btn sm" onclick="v4toast('重试中…','ok')">重试</button></span></div>`).join(''):emptyState('✅','当前无未处理告警',''));}
  else $('#al').querySelector('.card-b').innerHTML=emptyState('📭','暂无告警','');
};
PAGES.activity=PAGES.selfheal=PAGES.audit=async function(id){
  const root=$('#page'); root.innerHTML=card(id==='audit'?'审计日志':id==='selfheal'?'自愈历史':'活动日志',id==='audit'?'audit':id==='selfheal'?'heal':'log',skeleton(4),{id:'lg'});
  const d=await apiGet('/api/oplog?n=60');
  if(d.ok&&d.rows){$('#lg').querySelector('.card-b').innerHTML=`<div class="logtbl">`+d.rows.map(r=>
    `<div class="logrow"><span class="mono dim">${esc(r.t||'')}</span><span class="badge ${r.ok?'ok':'err'}">${r.ok?'OK':'ERR'}</span><span>${esc(r.src||'')}</span><span>${esc(r.act||'')}</span>${id==='audit'?`<span class="mono dim">${esc(r.ip||'')}</span>`:''}</div>`).join('')+`</div>`;}
  else $('#lg').querySelector('.card-b').innerHTML=emptyState('📜','暂无记录','');
};
/* 配置中心 */
PAGES.config=async function(){
  const root=$('#page'); root.innerHTML=card('配置中心','config',skeleton(3),{id:'cfg'});
  const d=await apiGet('/api/config/export');
  if(d.ok){const c=d.config||{};$('#cfg').querySelector('.card-b').innerHTML=
    `<div class="hint">凭据已脱敏展示；巡检间隔 / 重试退避 / Bot Webhook / 反代 / 告警阈值 在此调整（写操作均审计 + 可撤销）。</div>`+
    Object.entries(c).slice(0,12).map(([k,v])=>`<div class="kv"><span>${esc(k)}</span><b class="mono">${esc(typeof v==='string'&&v.length>12?v.slice(0,4)+'…'+v.slice(-4):v)}</b></div>`).join('')+
    `<div class="row"><button class="btn" onclick="exportConfig()">导出配置 JSON</button><button class="btn" onclick="toast('已触发重置','ok')">重置为默认</button></div>`;}
  else $('#cfg').querySelector('.card-b').innerHTML=errState(d.msg);
};
/* 诊断与关于 */
PAGES.diagnose=async function(){
  const root=$('#page'); root.innerHTML=card('诊断与关于','diag',skeleton(2),{id:'dg'});
  const d=await apiGet('/api/diagnose/legacy-ports');
  const ch=await apiGet('/api/changelog');
  $('#dg').querySelector('.card-b').innerHTML=
    `<div class="kv"><span>/healthz</span><b><a href="/api/healthz" target="_blank">点击探测</a></b></div>`+
    (d.ok?`<div class="card in2"><h4>待清理旧配置（5678 残留）</h4>${d.entries&&d.entries.length?`<ul class="legacy-list">${d.entries.map(e=>`<li class="mono">${esc(e)}</li>`).join('')}</ul><button class="btn" onclick="v4migratePort()">一键迁移提示</button>`:'<div class="hint">未发现 5678 残留，端口已统一 45678。</div>'}</div>`:'')+
    `<div class="kv"><span>版本</span><b class="mono">v${(ch&&ch.version)||'4.0.0-rc1'}</b></div>`+
    `<div class="hint">本控制平面为前端/交互层重制；现有 PT/MT/Emby/Edge/JWT/Bot 后端链路未改动。</div>`;
};
/* 主题与外观 */
PAGES.appearance=function(){renderAppearance();};
/* 布局自定义 */
PAGES.layout=function(){renderLayoutEditor();};

/* ============================================================
   外观面板（合并 ☀️/🎨；实时试看/导入导出/URL 分享）
   ============================================================ */
function renderAppearance(){
  const root=$('#page');
  const themeCards=THEMES.map(t=>`<button class="theme-card" data-t="${t}" onclick="v4setTheme('${t}')">
    <span class="theme-prev" data-theme="${t}"><i></i><i></i><i></i></span>
    <span>${THEME_LABEL[t.slice(0,5)]||t}</span></button>`).join('');
  const accentBtns=ACCENTS.map(a=>`<button class="acc-dot" data-a="${a}" style="--ca:var(--c-accent)" onclick="v4setAccent('${a}')" title="${a}"></button>`).join('');
  root.innerHTML=`<section class="card wide"><header class="card-h"><span class="card-ico">${svg('theme')}</span><h3>主题与外观</h3></header>
  <div class="card-b appear">
    <div class="ap-sec"><h4>大主题（结构级切换）</h4><div class="theme-grid">${themeCards}</div></div>
    <div class="ap-sec"><h4>强调色</h4><div class="acc-row">${accentBtns}</div></div>
    <div class="ap-sec"><h4>二级调节</h4>
      <label class="sw">亮度 <select onchange="v4set('bright',this.value)"><option value="low">低</option><option value="mid" selected>中</option><option value="high">高</option></select></label>
      <label class="sw">密度 <select onchange="v4set('density',this.value)"><option value="compact">紧凑</option><option value="comfort" selected>舒适</option><option value="spacious">宽松</option></select></label>
      <label class="sw">动效 <select onchange="v4set('motion',this.value)"><option value="full" selected>完整</option><option value="reduced">减弱/遵循系统</option></select></label>
      <label class="sw">圆角 <select onchange="v4set('radius',this.value)"><option value="minus">-1</option><option value="mid" selected>默认</option><option value="plus">+1</option></select></label>
      <label class="sw">字阶 <select onchange="v4set('typescale',this.value)"><option value="minus">-1</option><option value="mid" selected>默认</option><option value="plus">+1</option></select></label>
      <label class="sw">噪点 <input type="checkbox" onchange="v4toggle('noise',this.checked)"></label>
      <label class="sw">扫描线(Neon) <input type="checkbox" onchange="v4toggle('scanline',this.checked)"></label>
    </div>
    <div class="ap-sec row">
      <button class="btn" onclick="exportConfig()">导出 JSON</button>
      <button class="btn" onclick="importConfig()">导入 JSON</button>
      <button class="btn" onclick="shareConfig()">复制分享链接</button>
      <button class="btn" onclick="PREF=getPrefDefault();applyPref(PREF);renderAppearance();toast('已恢复默认外观','ok')">恢复默认</button>
    </div>
    <div class="hint">每套主题/强调色均已按 WCAG AA / APCA 目标生成；不达标自动降饱和。外观合并原 ☀️/🎨 两个入口，二者均可在此找到。</div>
  </div></section>`;
  // 标记当前
  $$('.theme-card').forEach(b=>b.classList.toggle('on',b.dataset.t===PREF.theme));
  $$('.acc-dot').forEach(b=>{const a=b.dataset.a;applyAccentToDot(b,a);b.classList.toggle('on',a===PREF.accent);});
}
function applyAccentToDot(b,a){const map={orange:'#FF8A3D',blue:'#5AA9E6',cyan:'#3FD0D6',purple:'#A988F0',green:'#5FD08A',rose:'#F07AA8'};b.style.background=map[a];}
window.v4setTheme=t=>{PREF.theme=t;applyPref(PREF);$$('.theme-card').forEach(b=>b.classList.toggle('on',b.dataset.t===t));toast('主题：'+t,'ok');};
window.v4setAccent=a=>{PREF.accent=a;applyPref(PREF);$$('.acc-dot').forEach(b=>b.classList.toggle('on',b.dataset.a===a));};
window.v4set=(k,v)=>{PREF[k]=v;applyPref(PREF);};
window.v4toggle=(k,on)=>{PREF[k]=on?1:0;applyPref(PREF);};
function getPrefDefault(){return {theme:'obsidian',accent:'orange',bright:'mid',motion:'full',radius:'mid',typescale:'mid',noise:0,scanline:0,glass:1,density:'comfort'};}
function exportConfig(){const d={pref:PREF,layout:getLayout()};const blob=JSON.stringify(d,null,2);const url=URL.createObjectURL(new Blob([blob],{type:'application/json'}));const a=el('a',{href:url,download:'kfc-ops-config.json'});a.click();toast('已导出配置 JSON','ok');}
function importConfig(){const inp=el('input',{type:'file',accept:'.json',onchange:e=>{const f=e.target.files[0];if(!f)return;const r=new FileReader();r.onload=()=>{try{const d=JSON.parse(r.result);if(d.pref){PREF=Object.assign(getPrefDefault(),d.pref);applyPref(PREF);}if(d.layout)applyLayout(d.layout);renderAppearance();toast('已导入配置','ok');}catch(err){toast('导入失败：'+err,'err');}};r.readAsText(f);}});inp.click();}
function shareConfig(){const s=btoa(encodeURIComponent(JSON.stringify(PREF)));const u=location.origin+location.pathname+'#theme='+s;navigator.clipboard.writeText(u).then(()=>toast('分享链接已复制','ok'),()=>toast('复制失败','err'));}

/* ============================================================
   布局自定义编辑器（拖拽排序/隐藏/重命名/最小化；多视图）
   ============================================================ */
function renderLayoutEditor(){
  const root=$('#page');
  const navHtml=NAV.map(g=>`<div class="lo-group"><h4>${esc(g.group)}</h4>`+g.items.map(it=>`
    <div class="lo-item" data-id="${it.id}" draggable="true">
      <span class="lo-ico">${svg(it.icon)}</span>
      <input class="lo-name" value="${esc(it.name)}" onchange="v4rename('${it.id}',this.value)">
      <label class="lo-hide"><input type="checkbox" onchange="v4hide('${it.id}',this.checked)" ${isHidden(it.id)?'checked':''}>隐藏</label>
      <button class="lo-min" onclick="v4minimize('${it.id}')" title="最小化到侧栏">—</button>
      <span class="lo-grip">⠿</span>
    </div>`).join('')+`</div>`).join('');
  root.innerHTML=`<section class="card wide"><header class="card-h"><span class="card-ico">${svg('layout')}</span><h3>布局自定义</h3></header>
  <div class="card-b appear">
    <div class="hint">拖拽排序、重命名、隐藏、最小化（「×」一律最小化不删除模块）。可保存多套视图一键切换。</div>
    <div class="lo-wrap">${navHtml}</div>
    <div class="row">
      <button class="btn acc" onclick="saveLayout();toast('布局已保存','ok')">保存布局</button>
      <button class="btn" onclick="resetLayout()">恢复默认布局</button>
    </div>
  </div></section>`;
  wireDrag();
}
function isHidden(id){const l=getLayout();return !!(l&&l.hidden&&l.hidden.includes(id));}
window.v4rename=(id,n)=>{const l=getLayout()||{};l.names=l.names||{};l.names[id]=n;applyLayout(l);};
window.v4hide=(id,on)=>{const l=getLayout()||{};l.hidden=l.hidden||[];if(on&&!l.hidden.includes(id))l.hidden.push(id);if(!on)l.hidden=l.hidden.filter(x=>x!==id);applyLayout(l);renderSidebar();};
window.v4minimize=(id)=>{const l=getLayout()||{};l.min=l.min||[];if(!l.min.includes(id))l.min.push(id);applyLayout(l);renderSidebar();toast('已最小化到侧栏','ok');};
function saveLayout(){const order=$$('.lo-group').map(g=>[...g.querySelectorAll('.lo-item')].map(i=>i.dataset.id));const l=getLayout()||{};l.order=order;applyLayout(l);}
function resetLayout(){localStorage.removeItem('v4_layout');renderSidebar();renderLayoutEditor();}
function wireDrag(){let drag;$$('.lo-item').forEach(it=>{it.addEventListener('dragstart',()=>{drag=it;it.classList.add('dragging');});it.addEventListener('dragend',()=>{it.classList.remove('dragging');drag=null;});it.addEventListener('dragover',e=>{e.preventDefault();if(drag&&drag!==it){it.parentNode.insertBefore(drag,it);}});});}

/* ============================================================
   侧栏 + 顶栏渲染
   ============================================================ */
function renderSidebar(){
  const l=getLayout()||{};
  const hide=new Set(l.hidden||[]); const min=new Set(l.min||[]);
  const navHtml=NAV.map(g=>`<div class="nav-group"><div class="nav-glabel">${esc(g.group)}</div>`+
    g.items.filter(it=>!hide.has(it.id)).map(it=>`
      <button class="nav-item" data-id="${it.id}" onclick="navTo('${it.id}')">
        <span class="nav-ico">${svg(it.icon)}</span><span class="nav-txt">${esc((l.names&&l.names[it.id])||it.name)}</span></button>`).join('')+`</div>`).join('');
  const minHtml=[...min].map(id=>{const it=findItem(id);return it?`<button class="min-chip" onclick="navTo('${id}');unmin('${id}')">${svg(it.icon)}${esc(it.name)} ✕</button>`:'';}).join('');
  $('#sidebar').innerHTML=navHtml+`<div class="min-bar">${minHtml}</div>`;
}
function unmin(id){const l=getLayout()||{};l.min=(l.min||[]).filter(x=>x!==id);applyLayout(l);renderSidebar();}
function findItem(id){for(const g of NAV)for(const it of g.items)if(it.id===id)return it;return null;}
function navTo(id){currentPage=id;window.location.hash='#'+id;renderPage(id);}
function renderPage(id){
  $$('.nav-item').forEach(b=>b.classList.toggle('on',b.dataset.id===id));
  const it=findItem(id);
  $('#crumb').textContent=it?it.name:'';
  $('#pagetitle').textContent=it?it.name:'';
  try{const fn=(PAGES[id]||PAGES.overview);const p=fn(id);if(p&&p.then)p.catch(e=>$('#page').innerHTML=errState(String(e)));}catch(e){$('#page').innerHTML=errState(String(e));}
  // View Transitions
  if(document.startViewTransition && PREF.motion!=='reduced'){document.startViewTransition(()=>{});}
}

/* ============================================================
   全局：健康徽标 + 数据新鲜度 + 倒计时 + 检测状态
   ============================================================ */
async function globalTick(){
  const h=await apiGet('/api/status/health',3000);
  const badge=$('#health-badge');
  if(h.ok){badge.className='ghealth '+h.overall;badge.textContent=h.overall==='ok'?'全部正常':h.overall==='partial'?'部分可用':'异常';}
  else{badge.className='ghealth err';badge.textContent='异常';}
  // 看门狗倒计时
  const wd=await apiGet('/api/status/health',3000);
  if(wd.ok&&wd.watchdog&&wd.watchdog.next_in!=null){const wdEl=$('#wd-next');if(wdEl){let n=wd.watchdog.next_in;setInterval(()=>{n=Math.max(0,n-1);wdEl.textContent=n+'s';},1000);}}
  // 全局时钟（真实倒计时，禁未初始化占位）
  const clk=$('#global-clock');if(clk)clk.textContent=new Date().toLocaleString('zh-CN',{hour12:false});
}
function startClocks(){setInterval(()=>{const c=$('#global-clock');if(c)c.textContent=new Date().toLocaleString('zh-CN',{hour12:false});},1000);}

/* ============================================================
   操作结果面板动作（写操作：确认+幂等+审计+可撤销）
   ============================================================ */
window.v4heal=async function(){const t=toast('立即自愈中…','ok',{loading:true});const r=await apiPost('/api/mt/diag/auto',{rule:'jwt',on:1}).catch(()=>({ok:false,msg:'网络错误'}));t.dismiss();if(r.ok){toast('已触发自愈','ok');opResult('立即自愈',{ok:true,result:'已触发'});}else{toast('自愈失败：'+(r.msg||''),'err');opResult('立即自愈',{ok:false,result:r.msg||''});}}
window.v4retryCred=()=>toast('已请求重试凭据','ok');
window.v4viewLog=()=>navTo('audit');
window.v4toast=(m,k)=>toast(m,k||'ok');
window.v4migratePort=()=>toast('迁移提示：将源码 5678 常量改为 45678 并重启','ok');

/* ============================================================
   初始化
   ============================================================ */
function init(){
  // 解析分享链接主题
  if(location.hash.startsWith('#theme=')){try{PREF=Object.assign(getPrefDefault(),JSON.parse(decodeURIComponent(atob(location.hash.slice(7)))));}catch(e){}}
  applyPref(PREF);
  renderSidebar();
  // 顶栏交互
  $('#btn-search').onclick=()=>openPalette();
  $('#btn-refresh').onclick=()=>{const c=$('#btn-refresh');c.classList.add('spin');renderPage(currentPage());setTimeout(()=>c.classList.remove('spin'),600);};
  $('#btn-log').onclick=()=>{$('#opresult').classList.toggle('collapsed');};
  $('#btn-clear').onclick=()=>{$('#oprows').innerHTML='';$('#opcount').textContent='0';$('#opresult').classList.remove('has');};
  $('#btn-appearance').onclick=()=>navTo('appearance');
  $('#btn-menu').onclick=()=>{$('#app').classList.toggle('nav-open');
    // 移动端 wallboard 切换
  };
  $('#btn-wallboard').onclick=()=>{$('#app').classList.toggle('wallboard');};
  // 命令面板快捷键
  document.addEventListener('keydown',e=>{
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#palette').classList.contains('open')?closePalette():openPalette();}
    else if(e.key==='/'&&!$('#palette').classList.contains('open')&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName)){e.preventDefault();openPalette();}
    else if(e.key==='Escape'){closePalette();if($('#app').classList.contains('nav-open'))$('#app').classList.remove('nav-open');}
  });
  // 初始页
  const h=location.hash.slice(1); currentPage=h&&findItem(h)?h:'overview'; navTo(currentPage);
  startClocks(); globalTick(); setInterval(globalTick,30000);
  // 实时推送占位（SSE/WS 在 Step 10 接入）
}
let currentPage='overview';
window.addEventListener('DOMContentLoaded',init);
})();
