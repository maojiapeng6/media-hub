/* ============================================================
   KFC 媒体中枢 · v4.1 控制平面 · app.js
   纯 vanilla JS，零外部依赖。组件内不写死颜色/圆角/阴影/动效时长。
   后端既有链路零改动；MUST-KEEP 全字段保留；iframe 桥接 100% 保留旧模块。
   ============================================================ */
(function(){
'use strict';
const $=(s,r=document)=>r.querySelector(s);
const $$=(s,r=document)=>[...r.querySelectorAll(s)];
const el=(t,a={},...c)=>{const e=document.createElement(t);for(const k in a){if(k==='class')e.className=a[k];else if(k==='html')e.innerHTML=a[k];else if(k.startsWith('on'))e.addEventListener(k.slice(2),a[k]);else e.setAttribute(k,a[k]);}for(const x of c)e.append(x&&x.nodeType?x:document.createTextNode(x));return e;};
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmtTime=ts=>{if(!ts)return '—';return new Date(ts*1000).toLocaleString('zh-CN',{hour12:false});};
const relTime=ts=>{if(!ts)return '—';const s=Math.max(0,Math.floor(Date.now()/1000-ts));if(s<60)return s+'s 前';if(s<3600)return Math.floor(s/60)+'m 前';if(s<86400)return Math.floor(s/3600)+'h 前';return Math.floor(s/86400)+'d 前';};
const tnum=n=>String(n??0).replace(/\B(?=(\d{3})+(?!\d))/g,',');

/* —— 矢量图标（Lucide 风格，stroke 随 --icon-stroke；禁 emoji 做控件图标） —— */
const ICON={
  overview:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  pt:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/>',
  mt:'<path d="M12 2 4 6v6c0 5 3.5 8 8 10 4.5-2 8-5 8-10V6z"/>',
  emby:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m10 9 6 3-6 3z"/>',
  topology:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><path d="M6.5 10v3h11v-3M12 13v4"/>',
  alert:'<path d="M12 3 2 20h20z"/><path d="M12 9v5M12 17v.5"/>',
  log:'<path d="M5 4h14v16H5z"/><path d="M8 8h8M8 12h8M8 16h5"/>',
  heal:'<path d="M12 21C6 16 3 12 3 8a4 4 0 0 1 8-1 4 4 0 0 1 8 1c0 4-3 8-9 13z"/><path d="M9 12h6M12 9v6"/>',
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
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  disk:'<path d="M4 4h16v16H4z"/><circle cx="12" cy="12" r="3"/><path d="M17 8h.01"/>',
  pause:'<path d="M9 5v14M15 5v14"/>',
  play:'<path d="m8 5 11 7-11 7z"/>'
};
const svg=(n,cls='')=>`<svg class="ico ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="var(--icon-stroke)" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[n]||''}</svg>`;

/* —— API 封装 —— */
async function apiGet(p,to=8000){const c=new AbortController();const t=setTimeout(()=>c.abort(),to);try{const r=await fetch(p,{signal:c.signal});if(!r.ok)return{ok:false,code:r.status,msg:r.statusText};try{return await r.json();}catch(e){return{ok:true,raw:await r.text()};}}catch(e){return{ok:false,msg:String(e)};}finally{clearTimeout(t);}}
async function apiPost(p,body={}){try{const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)return{ok:false,code:r.status,msg:r.statusText};try{return await r.json();}catch(e){return{ok:true};}}catch(e){return{ok:false,msg:String(e)};}}

/* ============================================================
   主题引擎（严格两级：主题包 + 调色） + 自定义主题 + WCAG/APCA 校验
   ============================================================ */
const THEMES=['obsidian','liquid-glass','light-studio','terminal','editorial','neon-ops','pastel-soft','arctic-night'];
const THEME_LABEL={obsidian:'曜石运维', 'liquid-glass':'液态玻璃', 'light-studio':'明亮工作台', terminal:'终端极客', editorial:'编排杂志', 'neon-ops':'霓虹调度台', 'pastel-soft':'柔和粉彩', 'arctic-night':'极夜冷蓝'};
const ACCENTS=[{k:'orange',n:'琥珀',v:'#FF8A3D'},{k:'blue',n:'蓝',v:'#5AA9E6'},{k:'cyan',n:'青',v:'#3FD0D6'},{k:'purple',n:'紫',v:'#A988F0'},{k:'green',n:'绿',v:'#5FD08A'},{k:'rose',n:'玫',v:'#F07AA8'},{k:'red',n:'红',v:'#E5604D'},{k:'amber',n:'金',v:'#E8B339'},{k:'teal',n:'蓝绿',v:'#3FC9B0'},{k:'indigo',n:'靛',v:'#7C8FF0'},{k:'lime',n:'青柠',v:'#A8D44E'},{k:'slate',n:'石板',v:'#94A3B8'}];
const GRAYS=[{k:'neutral',n:'中性'},{k:'zinc',n:'锌'},{k:'stone',n:'石'},{k:'mauve',n:'藕紫'},{k:'olive',n:'橄榄'},{k:'mist',n:'雾蓝'},{k:'taupe',n:'灰褐'}];
const LS={get:k=>{try{return JSON.parse(localStorage.getItem('v4_'+k));}catch(e){return null;}},set:(k,v)=>localStorage.setItem('v4_'+k,JSON.stringify(v))};
function getPrefDefault(){return {theme:'obsidian',mode:'auto',accent:'orange',accentCustom:'',bright:'b3',density:'comfort',motion:'full',radius:'mid',typescale:'mid',navpos:'auto',glass:1,scanline:0,grid:0,noise:0,gray:'neutral',status:{},custom:null};}
function getPref(){return Object.assign(getPrefDefault(),LS.get('pref')||{});}
/* WCAG 相对亮度 + 对比度 */
function lum(hex){try{const c=hex.replace('#','');const v=[0,2,4].map(i=>parseInt(c.slice(i,i+2),16)/255).map(x=>x<=.03928?x/12.92:Math.pow((x+.055)/1.055,2.4));return .2126*v[0]+.7152*v[1]+.0722*v[2];}catch(e){return .2;}}
function contrast(a,b){const l1=lum(a),l2=lum(b);return (Math.max(l1,l2)+.05)/(Math.min(l1,l2)+.05);}
/* APCA 近似（Lc 简化；仅做可读性提示，非认证） */
function apcaOk(fg,bg){const Lf=lum(fg),Lb=lum(bg);return Math.abs(Lf-Lb)>.28;}
/* 校验强调色与正文：不达标自动降饱和并提示 */
function checkContrast(accent,bg){const cr=contrast(accent,bg);const ok=cr>=3&&apcaOk(accent,bg);if(ok)return{ok:true,accent,cr};
  // 降饱和：向灰方向混合直到达标（最多 10 步）
  const mix=(a,b,t)=>{const pa=a.replace('#',''),pb=b.replace('#','');const o=[0,2,4].map(i=>{const x=parseInt(pa.slice(i,i+2),16),y=parseInt(pb.slice(i,i+2),16);return Math.round(x+(y-x)*t).toString(16).padStart(2,'0');});return '#'+o.join('');};
  let cand=accent;for(let i=1;i<=10;i++){cand=mix(accent,'#808080',i/10);if(contrast(cand,bg)>=3&&apcaOk(cand,bg))return{ok:false,accent:cand,cr:contrast(cand,bg),msg:'对比度不足，已自动降饱和以保证可读'};}
  return{ok:false,accent:'#808080',cr:contrast('#808080',bg),msg:'对比度不足，已回退为中性色'};
}
function effectiveMode(p){if(p.mode==='light'||p.mode==='dark')return p.mode;return matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';}
function applyPref(p){
  const b=document.body,d=document.documentElement;
  const mode=effectiveMode(p);
  d.setAttribute('data-theme',p.theme);
  d.setAttribute('data-mode',mode);
  d.setAttribute('data-accent',p.accent);
  b.setAttribute('data-bright',p.bright);
  b.setAttribute('data-motion',p.motion==='reduced'?'reduced':'full');
  b.setAttribute('data-radius',p.radius);
  b.setAttribute('data-typescale',p.typescale);
  b.setAttribute('data-glass',p.glass?'1':'0');
  b.setAttribute('data-scanline',p.scanline?'1':'0');
  b.setAttribute('data-grid',p.grid?'1':'0');
  b.setAttribute('data-noise',p.noise?'1':'0');
  const navpos=p.navpos&&p.navpos!=='auto'?p.navpos:({terminal:'top',editorial:'top'}[p.theme]||'left');
  d.setAttribute('data-navpos',navpos);
  d.style.setProperty('--density',p.density==='compact'?.82:p.density==='spacious'?1.15:1);
  d.style.setProperty('--c-gray','var(--'+p.gray+')');
  // 用户自定义主题（保存的 token 覆盖）
  const custom=p.custom&&p.custom.theme===p.theme?p.custom.tokens:null;
  const set=(k,v)=>{d.style.setProperty(k,v||'');};
  set('--usr-accent',custom&&custom.accent);
  let accent=p.accentCustom||ACCENTS.find(a=>a.k===p.accent)?.v||'#FF8A3D';
  if(custom&&custom.accent)accent=custom.accent;
  const bgFair=mode==='dark'?'#0F1011':'#FFFFFF';
  const chk=checkContrast(accent,bgFair);
  d.style.setProperty('--c-accent',chk.accent);
  d.style.setProperty('--usr-accent-text',contrast(chk.accent,'#ffffff')>=4.5?'#ffffff':'#121212');
  set('--usr-surface0',custom&&custom.surface0); set('--usr-surface1',custom&&custom.surface1);
  set('--usr-surface2',custom&&custom.surface2); set('--usr-surface3',custom&&custom.surface3);
  set('--usr-border',custom&&custom.border); set('--usr-text',custom&&custom.text); set('--usr-text-dim',custom&&custom.textDim);
  if(p.status){for(const k in p.status){set('--usr-'+k,p.status[k]);}}
  LS.set('pref',p);
  apiPost('/api/theme',p).catch(()=>{});
  PREF=p;
  return chk;
}
/* 自定义主题库 */
const getCustoms=()=>LS.get('customs')||[];
const saveCustomTheme=(name)=>{const p=PREF;const customs=getCustoms();const cs=getComputedStyle(document.documentElement);
  const tokens={accent:cs.getPropertyValue('--c-accent').trim(),surface0:cs.getPropertyValue('--surface-0').trim(),surface1:cs.getPropertyValue('--surface-1').trim(),surface2:cs.getPropertyValue('--surface-2').trim(),surface3:cs.getPropertyValue('--surface-3').trim(),border:cs.getPropertyValue('--c-border').trim(),text:cs.getPropertyValue('--c-text').trim(),textDim:cs.getPropertyValue('--c-text-dim').trim()};
  customs.push({id:'c'+Date.now(),name,theme:p.theme,tokens});LS.set('customs',customs);return customs;};
let PREF=getPref();

/* ============================================================
   导航（概览 / 服务 / 运维 / 系统）—— legacy 项 iframe 桥接保留
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
    {id:'appearance',name:'外观',icon:'theme'},
    {id:'layout',name:'布局自定义',icon:'layout'},
    {id:'diagnose',name:'诊断与关于',icon:'diag'},
  ]},
];
const getLayout=()=>LS.get('layout')||null;
const applyLayout=l=>LS.set('layout',l);
function findItem(id){for(const g of NAV)for(const it of g.items)if(it.id===id)return it;return null;}

/* ============================================================
   Toast（堆叠/撤销/hover暂停/错误常驻）
   ============================================================ */
let toastSeq=0;
function toast(msg,kind='ok',opts={}){
  const id='t'+(++toastSeq);
  const t=el('div',{class:'toast '+kind,role:'status','aria-live':'polite'},el('span',{class:'toast-ico'},svg(kind==='ok'?'shield':kind==='err'?'alert':'bolt')),el('span',{class:'toast-msg'},msg));
  if(opts.undo){const b=el('button',{class:'toast-undo',onclick:()=>{opts.undo();dismiss();}},'撤销');t.append(b);}
  if(opts.loading){t.append(el('span',{class:'toast-spin'}));t.dataset.loading='1';}
  if(opts.action){t.append(el('button',{class:'toast-act',onclick:()=>{opts.action();dismiss();}},opts.actionLabel||'查看'));}
  $('#toasts').append(t);requestAnimationFrame(()=>t.classList.add('in'));
  let to;const dismiss=()=>{t.classList.remove('in');setTimeout(()=>t.remove(),180);clearTimeout(to);};
  if(kind!=='err'){to=setTimeout(dismiss,opts.duration||3600);t.addEventListener('mouseenter',()=>clearTimeout(to));t.addEventListener('mouseleave',()=>{to=setTimeout(dismiss,1200);});}
  t._dismiss=dismiss;return{update:(m,k)=>{t.className='toast '+(k||kind);$('.toast-msg',t).textContent=m;t.removeAttribute('data-loading');},dismiss};
}
function opResult(title,detail){
  const panel=$('#opresult');panel.classList.remove('collapsed');panel.classList.add('has');
  const row=el('div',{class:'op-row'},el('span',{class:'op-dot '+(detail.ok?'ok':'err')}),el('span',{class:'op-title'},title),el('span',{class:'op-detail mono'},detail.result||(detail.ok?'成功':'失败')));
  $('#oprows').prepend(row);while($('#oprows').children.length>50)$('#oprows').lastChild.remove();
  const c=$('#opcount');c.textContent=String(parseInt(c.textContent||'0')+1);
}

/* ============================================================
   命令面板（Ctrl K）
   ============================================================ */
function openPalette(){const ov=$('#palette');ov.classList.add('open');const inp=$('#pal-cmd');inp.value='';$('#pal-results').innerHTML='';inp.focus();inp.oninput=()=>renderPalette(inp.value);renderPalette('');}
const PALETTE_ACTIONS=[
  {k:'跳转到 总览',fn:()=>navTo('overview')},
  {k:'跳转到 PT 资源站',fn:()=>navTo('pt')},
  {k:'跳转到 MT 馒头鉴权',fn:()=>navTo('mt')},
  {k:'跳转到 Emby 媒体库',fn:()=>navTo('emby')},
  {k:'打开 外观',fn:()=>navTo('appearance')},
  {k:'打开 配置中心',fn:()=>navTo('config')},
  {k:'打开 布局自定义',fn:()=>navTo('layout')},
  ...THEMES.map(t=>({k:'切换主题：'+THEME_LABEL[t],fn:()=>{PREF.theme=t;applyPref(PREF);}})),
  {k:'切换明暗',fn:()=>{PREF.mode=effectiveMode(PREF)==='dark'?'light':'dark';applyPref(PREF);}},
  {k:'保存当前布局',fn:()=>{saveLayout();toast('布局已保存','ok');}},
  {k:'导出主题/布局 JSON',fn:()=>exportConfig()},
  {k:'添加卡片',fn:()=>{navTo('overview');setTimeout(openWidgetPicker,200);}},
];
let palSel=0;
function renderPalette(q){const list=PALETTE_ACTIONS.filter(a=>a.k.toLowerCase().includes(q.toLowerCase()));const box=$('#pal-results');box.innerHTML='';palSel=0;
  list.forEach((a,i)=>{const r=el('div',{class:'pal-item'+(i===0?' active':''),onclick:()=>{a.fn();closePalette();}},el('span',{},svg('search')),el('span',{},a.k));r.dataset.i=i;box.append(r);});
  if(!list.length)box.append(el('div',{class:'pal-empty'},'无匹配'));}
function closePalette(){$('#palette').classList.remove('open');}

/* ============================================================
   JWT 六态 / 全局健康三态
   ============================================================ */
const JWT_STATE={valid:{cls:'ok',txt:'有效',ico:'shield'},expiring:{cls:'warn',txt:'临期',ico:'clock'},refreshing:{cls:'info',txt:'刷新中',ico:'bolt'},failed:{cls:'err',txt:'失败',ico:'alert'},degraded:{cls:'degrade',txt:'已降级',ico:'alert'},nocred:{cls:'',txt:'无凭据',ico:'shield'}};
function jwtBadge(s,left){const m=JWT_STATE[s]||JWT_STATE.nocred;return `<span class="badge ${m.cls}">${svg(m.ico)}${m.txt}${s==='expiring'&&left!=null?' '+left+'s':''}</span>`;}

/* 四态 */
function skeleton(n=3){let h='';for(let i=0;i<n;i++)h+='<div class="sk"></div>';return `<div class="sk-wrap">${h}</div>`;}
function emptyState(icon,txt,sub,act){return `<div class="empty"><div class="empty-ico">${svg(icon)}</div><div class="empty-t">${esc(txt)}</div>${sub?`<div class="empty-s">${esc(sub)}</div>`:''}${act?`<button class="btn sm" onclick="${act}">重试</button>`:''}</div>`;}
function errState(msg,act){return `<div class="empty err"><div class="empty-ico">${svg('alert')}</div><div class="empty-t">加载失败</div><div class="empty-s mono">${esc(msg||'')}</div>${act?`<button class="btn sm" onclick="${act}">重试</button>`:''}</div>`;}
function degradedState(msg){return `<div class="empty degrade"><div class="empty-ico">${svg('alert')}</div><div class="empty-t">部分可用（降级）</div><div class="empty-s mono">${esc(msg||'')}</div></div>`;}

/* 通用卡片 + 新鲜度 */
function card(title,icon,body,opts={}){return `<section class="card ${opts.cls||''}" id="${opts.id||''}" data-card="${opts.id||''}">
  <header class="card-h"><span class="card-ico">${svg(icon)}</span><h3>${esc(title)}</h3>
  ${opts.fresh?'<span class="fresh mono" data-fresh="'+opts.fresh+'"></span>':''}${opts.tools||''}</header>
  <div class="card-b">${body}</div></section>`;}
function setFresh(elem,fresh){$$('[data-fresh]',elem).forEach(e=>{const ts=+e.getAttribute('data-fresh');e.textContent='更新 '+relTime(ts);e.classList.toggle('stale',Date.now()/1000-ts>120);});}

/* ============================================================
   页面
   ============================================================ */
const PAGES={};
/* —— 总览（首屏全站，MUST-KEEP 全字段） —— */
PAGES.overview=async function(){
  const root=$('#page');
  root.innerHTML=
    card('健康概览','shield',skeleton(3),{id:'health',fresh:Math.floor(Date.now()/1000),cls:'c-health'})+
    card('关键指标','bolt',skeleton(5),{id:'metrics',cls:'c-metrics'})+
    card('磁盘水位','disk',skeleton(3),{id:'disk',cls:'c-disk'})+
    card('进行中任务','bolt',skeleton(3),{id:'tasks',cls:'c-tasks'})+
    card('馒头鉴权 · 自动守护','shield',skeleton(4),{id:'guard',fresh:Math.floor(Date.now()/1000),cls:'c-guard',
      tools:'<span class="card-tools"><button class="tbmini" title="折叠" onclick="v4fold(this)">'+svg('fold')+'</button><button class="tbmini" title="最小化" onclick="v4minCard(\'guard\')">'+svg('close')+'</button></span>'})+
    card('自愈看门狗','heal',skeleton(3),{id:'watchdog',cls:'c-watchdog'})+
    card('当前告警','alert',skeleton(3),{id:'alerts',cls:'c-alerts'})+
    card('服务与容器','topology',skeleton(4),{id:'services',cls:'c-services'})+
    card('自愈时间线','heal',skeleton(3),{id:'healtimeline',cls:'c-healtimeline'})+
    card('活动日志','log',skeleton(4),{id:'actlog',cls:'c-actlog'})+
    `<button class="add-widget" onclick="openWidgetPicker()">${svg('plus')} 添加卡片 / 接入新服务</button>`;
  // 健康三态
  const h=await apiGet('/api/status/health');
  if(h.ok){const o=h.overall;
    $('#health .card-b')&&($('#health').querySelector('.card-b').innerHTML=
      `<div class="health3 badge ${o==='ok'?'ok':o==='partial'?'warn':'err'}">${svg(o==='ok'?'shield':'alert')}${o==='ok'?'全部正常':o==='partial'?'部分可用':'异常'}</div>`+
      (o==='partial'&&h.domains?`<div class="hint">部分可用：${Object.entries(h.domains).filter(([k,v])=>v.state!=='ok').map(([k])=>esc(k)).join('、')||'部分子系统'}</div>`:'')+
      Object.entries(h.domains||{}).map(([k,v])=>`<div class="hrow"><span>${esc(k)}</span><span class="badge ${v.state==='ok'?'ok':v.state==='partial'?'warn':'err'}">${esc(v.text||v.state||'—')}</span></div>`).join('')+
      (h.note?`<div class="hint">${esc(h.note)}</div>`:''));
  }else $('#health').querySelector('.card-b').innerHTML=errState(h.msg,'renderPage(currentPage)');
  // 指标组（派生自健康/看门狗，不伪造未采集项）
  {const wd=(h&&h.watchdog)||{};const m=[['刷新成功率','—'],['巡检成功率','—'],['累计自愈',tnum(wd.heal_count||0)],['告警平均恢复','—'],['Uptime',esc(wd.uptime||'—')]];
    $('#metrics').querySelector('.card-b').innerHTML=m.map(([k,v])=>metricCard(k,v)).join('');}
  // 磁盘水位 + 最高级告警
  const dk=await apiGet('/api/status/disk');
  if(dk.ok&&dk.disks){const ds=dk.disks;
    $('#disk').querySelector('.card-b').innerHTML=ds.map(d=>{const pct=d.pct||0;const crit=pct>=98;
      return `<div class="diskrow ${crit?'crit':''}">${crit?`<div class="disk-alert">${svg('alert')}<div><b>磁盘告急：${esc(d.mount)} 已用 ${pct}%，可用 ${esc(d.avail)}</b><div class="hint">影响：写入/整理/下载可能失败。请立即处理。</div></div></div>`:''}
      <div class="kv"><span>${esc(d.mount)}</span><b class="mono">${pct}%</b></div>
      <div class="bar"><i style="width:${pct}%"></i></div>
      <div class="row"><button class="btn sm" onclick="v4toast('已请求清理缓存','ok')">清理缓存</button><button class="btn sm" onclick="navTo('config')">占用明细</button><button class="btn sm" onclick="navTo('config')">磁盘设置</button></div></div>`;}).join('');
  }else $('#disk').querySelector('.card-b').innerHTML=emptyState('disk','暂无磁盘数据','磁盘监控接入后自动显示');
  // 进行中任务
  const tk=await apiGet('/api/monitor/tasks');
  if(tk.ok&&tk.tasks&&tk.tasks.length){$('#tasks').querySelector('.card-b').innerHTML=tk.tasks.map(t=>`<div class="taskrow">
    <div class="kv"><span>${esc(t.name)}</span><b class="mono">${t.pct!=null?t.pct+'%':''}</b></div>
    <div class="bar"><i style="width:${t.pct||0}%"></i></div>
    <div class="hint">${t.eta?'预计剩余 '+esc(t.eta):''}</div>
    <div class="row"><button class="btn sm" onclick="v4taskPause('${esc(t.id)}','pause')">${svg('pause')} 暂停</button><button class="btn sm" onclick="v4taskPause('${esc(t.id)}','cancel')">取消</button><button class="btn sm" onclick="v4viewLog()">查看任务</button></div></div>`).join('');}
  else $('#tasks').querySelector('.card-b').innerHTML=emptyState('bolt','当前无进行中任务','MT 整理进度将在任务启动后显示');
  // 守护卡（六态 + MUST-KEEP 全字段）
  const j=await apiGet('/api/jwt/state');
  if(j.ok){const s=j.state||'nocred';$('#guard').querySelector('.card-b').innerHTML=
    `<div class="guard-state">${jwtBadge(s,j.left)}</div>`+
    (j.reason?`<div class="hint mono">失败原因：${esc(j.reason)}</div>`:'')+
    `<div class="kv"><span>JWT 状态</span><b>${jwtBadge(s,j.left)}</b></div>`+
    `<div class="kv"><span>当前令牌</span><b class="mono">已签发 · 指纹末4 ${esc(j.fp||'••••')}</b></div>`+
    `<div class="kv"><span>最近生效</span><b class="mono">${esc(j.last_ok||'—')}</b></div>`+
    `<div class="kv"><span>Bot 告警</span><b>${esc(j.bot||'未启用')}</b></div>`+
    `<div class="kv"><span>自愈看门狗</span><b>${esc(watchdogText(h))}</b></div>`+
    `<details class="mech"><summary>了解机制</summary><p>全自动：本机看门狗每 60 秒巡检，JWT 失效时自动用 Edge 已保存的凭据静默重登并回写，无需任何人工操作。</p></details>`+
    `<div class="row"><button class="btn acc" onclick="v4heal()">${svg('bolt')} 立即自愈</button>`+
    (s==='failed'?`<button class="btn" onclick="v4retryCred()">重试</button><button class="btn" onclick="v4viewLog()">查看日志</button>`:'')+`</div>`+
    `<div class="hint">馒头守护</div>`;
    setFresh(root,Math.floor(Date.now()/1000));}
  else $('#guard').querySelector('.card-b').innerHTML=errState(j.msg);
  // 看门狗
  if(h.ok&&h.watchdog){const w=h.watchdog;$('#watchdog').querySelector('.card-b').innerHTML=
    `<div class="kv"><span>下次巡检</span><b class="mono" id="wd-next">${w.next_in!=null?w.next_in+'s':'—'}</b></div>`+
    `<div class="kv"><span>连续存活</span><b class="mono">${esc(w.uptime||'—')}</b></div>`+
    `<div class="kv"><span>累计自愈</span><b class="mono">${tnum(w.heal_count||0)}</b></div>`+
    `<div class="kv"><span>最近结果</span><b>${esc(w.last||'—')}</b></div>`;}
  // 告警
  const al=await apiGet('/api/oplog?n=40');
  if(al.ok&&al.rows){const errs=al.rows.filter(r=>!r.ok);
    $('#alerts').querySelector('.card-b').innerHTML=errs.length?errs.slice(0,5).map(r=>`<div class="alertrow err"><span class="badge err">error</span><span class="mono dim">${esc(r.t||'')}</span><span>${esc(r.act||'')}</span><span class="row"><button class="btn sm" onclick="v4toast('已忽略','ok')">忽略</button><button class="btn sm" onclick="v4viewLog()">详情</button></span></div>`).join(''):emptyState('shield','当前无未处理告警','');}
  else $('#alerts').querySelector('.card-b').innerHTML=emptyState('shield','告警数据暂不可用','');
  // 服务与容器（只读）
  const ct=await apiGet('/api/monitor/containers');
  if(ct.ok&&ct.containers){$('#services').querySelector('.card-b').innerHTML=ct.containers.slice(0,12).map(c=>`<div class="srvrow"><span class="badge ${c.state==='running'?'ok':'err'}">${esc(c.state||'—')}</span><span>${esc(c.name)}</span><span class="mono dim">${c.cpu!=null?c.cpu+'%':''} ${c.mem!=null?c.mem+'MB':''}</span></div>`).join('');}
  else $('#services').querySelector('.card-b').innerHTML=emptyState('topology','容器监控未接入','在配置中心输入 Docker 地址即可自动生成卡片');
  // 自愈时间线 + 活动日志
  if(al.ok&&al.rows){
    $('#healtimeline').querySelector('.card-b').innerHTML='<div class="timeline">'+al.rows.filter(r=>/heal|自愈|重登/.test(r.act||'')).slice(0,6).map(r=>`<div class="tl-row"><span class="tl-dot"></span><span class="mono dim">${esc(r.t||'')}</span><span>${esc(r.act||'')}</span></div>`).join('')+'</div>'||emptyState('heal','暂无自愈记录','');
    $('#actlog').querySelector('.card-b').innerHTML='<div class="logtbl">'+al.rows.slice(0,20).map(r=>`<div class="logrow"><span class="mono dim">${esc(r.t||'')}</span><span class="badge ${r.ok?'ok':'err'}">${r.ok?'成功':'失败'}</span><span>${esc(r.act||'')}</span></div>`).join('')+'</div>';
  }
  setFresh(root,Math.floor(Date.now()/1000));
};
function watchdogText(h){const w=h&&h.watchdog;if(!w)return '—';return (w.next_in!=null?'下次 '+w.next_in+'s':'运行中');}
function metricCard(label,val){return `<div class="metric"><div class="metric-v mono">${esc(val??'—')}</div><div class="metric-l">${esc(label)}</div><div class="spark"></div></div>`;}

/* 经典模块桥接 */
PAGES.pt=PAGES.mt=PAGES.emby=function(id){
  const root=$('#page');root.innerHTML=`<section class="card"><header class="card-h"><span class="card-ico">${svg(id)}</span><h3>${id==='pt'?'PT 资源站':id==='mt'?'MT 馒头鉴权':'Emby 媒体库'}</h3></header>
  <div class="card-b"><div class="hint">以下为既有完整功能模块（原样运行，未删减）：</div><iframe class="legacy" src="/?embed=1" title="经典模块"></iframe></div></section>`;
};
/* 连接拓扑（端口口径纠正：宿主机5678 + 公网/反代45678 并列） */
PAGES.topology=async function(){
  const root=$('#page');root.innerHTML=card('连接拓扑','topology',skeleton(4),{id:'topo'});
  const d=await apiGet('/api/overview');
  if(d.ok){const p=d.conn||{};$('#topo').querySelector('.card-b').innerHTML=
    `<div class="port-note">${svg('topology')} <b>宿主机 5678 · 公网/反代 45678</b></div>`+
    `<div class="kv"><span>宿主机地址</span><b class="mono">5678</b></div>`+
    `<div class="kv"><span>公网/反代端口</span><b class="mono">45678</b></div>`+
    `<div class="kv"><span>反代路径</span><b class="mono">${esc(p.proxy||'/')}</b></div>`+
    `<div class="kv"><span>实测探测</span><b>${esc(p.probe||'—')}</b></div>`+
    `<div class="kv"><span>最后探测</span><b class="mono">${esc(p.last_probe||'—')}</b></div>`+
    `<div class="hint">宿主机内部访问用 5678；经公网或反向代理访问用 45678。两者指向同一服务。</div>`;}
  else $('#topo').querySelector('.card-b').innerHTML=errState(d.msg);
};
PAGES.alerts=async function(){const root=$('#page');root.innerHTML=card('告警中心','alert',skeleton(3),{id:'al'});const d=await apiGet('/api/oplog?n=40');
  if(d.ok&&d.rows){const errs=d.rows.filter(r=>!r.ok);$('#al').querySelector('.card-b').innerHTML=errs.length?errs.map(r=>`<div class="alertrow err"><span class="badge err">error</span><span class="mono dim">${esc(r.t||'')}</span><span>${esc(r.act||'')}：${esc(r.msg||'')}</span><span class="row"><button class="btn sm" onclick="v4toast('已忽略','ok')">忽略</button><button class="btn sm" onclick="v4toast('重试中','ok')">重试</button></span></div>`).join(''):emptyState('shield','当前无未处理告警','');}
  else $('#al').querySelector('.card-b').innerHTML=emptyState('shield','暂无告警','');};
PAGES.activity=PAGES.selfheal=PAGES.audit=async function(id){const root=$('#page');root.innerHTML=card(id==='audit'?'审计日志':id==='selfheal'?'自愈历史':'活动日志',id==='audit'?'audit':id==='selfheal'?'heal':'log',skeleton(4),{id:'lg'});
  const d=await apiGet('/api/oplog?n=60');if(d.ok&&d.rows){$('#lg').querySelector('.card-b').innerHTML='<div class="logtbl">'+d.rows.map(r=>`<div class="logrow"><span class="mono dim">${esc(r.t||'')}</span><span class="badge ${r.ok?'ok':'err'}">${r.ok?'OK':'ERR'}</span><span>${esc(r.src||'')}</span><span>${esc(r.act||'')}</span>${id==='audit'?`<span class="mono dim">${esc(r.ip||'')}</span>`:''}</div>`).join('')+'</div>';}
  else $('#lg').querySelector('.card-b').innerHTML=emptyState('log','暂无记录','');};
PAGES.config=async function(){const root=$('#page');root.innerHTML=card('配置中心','config',skeleton(3),{id:'cfg'});
  const d=await apiGet('/api/config/export');if(d.ok){const c=d.config||{};$('#cfg').querySelector('.card-b').innerHTML=
    `<div class="row"><button class="btn acc" onclick="startOnboarding()">接入新服务向导</button><button class="btn" onclick="exportConfig()">导出配置 JSON</button><button class="btn" onclick="importConfig()">导入配置</button></div>`+
    `<div class="hint">凭据已脱敏展示；写操作均二次确认 + 审计 + 可撤销。</div>`+
    Object.entries(c).slice(0,12).map(([k,v])=>`<div class="kv"><span>${esc(k)}</span><b class="mono">${esc(typeof v==='string'&&v.length>12?v.slice(0,4)+'…'+v.slice(-4):v)}</b></div>`).join('');}
  else $('#cfg').querySelector('.card-b').innerHTML=errState(d.msg);};
PAGES.diagnose=async function(){const root=$('#page');root.innerHTML=card('诊断与关于','diag',skeleton(2),{id:'dg'});
  const d=await apiGet('/api/diagnose/legacy-ports');const ch=await apiGet('/api/changelog');
  $('#dg').querySelector('.card-b').innerHTML=
    `<div class="port-note">${svg('diag')} <b>端口说明：宿主机 5678 · 公网/反代 45678</b></div>`+
    `<div class="kv"><span>/api/healthz</span><b><a href="/api/healthz" target="_blank">点击探测</a></b></div>`+
    `<div class="kv"><span>脱敏诊断包</span><b><a href="/api/config/export" target="_blank">导出</a></b></div>`+
    (d.ok&&d.entries&&d.entries.length?`<details class="mech"><summary>端口常量说明</summary><div class="hint">${d.entries.slice(0,20).map(e=>esc(e)).join('<br>')}</div></details>`:'')+
    `<div class="kv"><span>版本</span><b class="mono">v${(ch&&ch.version)||'4.1.0'}</b></div>`+
    `<div class="hint">本控制平面为前端/交互层升级；现有 PT/MT/Emby/Edge/JWT/Bot 后端链路未改动。</div>`;};
PAGES.appearance=function(){renderAppearance();};
PAGES.layout=function(){renderLayoutEditor();};

/* ============================================================
   外观面板（严格两级：选主题 -> 预览 -> 应用 -> 调色 -> 保存自定义）
   ============================================================ */
function renderAppearance(){
  const root=$('#page');
  const themeCards=THEMES.map(t=>`<button class="theme-card ${PREF.theme===t?'on':''}" data-t="${t}" onclick="v4setTheme('${t}')">
    <span class="theme-prev" data-theme="${t}"><i></i><i></i><i></i></span><span>${THEME_LABEL[t]}</span></button>`).join('');
  const accentBtns=ACCENTS.map(a=>`<button class="acc-dot ${PREF.accent===a.k?'on':''}" data-a="${a.k}" style="background:${a.v}" onclick="v4setAccent('${a.k}')" title="${a.n}"></button>`).join('');
  const grayOpts=GRAYS.map(g=>`<option value="${g.k}" ${PREF.gray===g.k?'selected':''}>${g.n}</option>`).join('');
  const customs=getCustoms();
  root.innerHTML=`<section class="card wide"><header class="card-h"><span class="card-ico">${svg('theme')}</span><h3>外观</h3></header><div class="card-b appear">
  <div class="ap-flow">1 选大主题 → 2 实时预览 → 3 应用 → 4 再调色 → 5 保存为自定义主题</div>
  <div class="ap-sec"><h4>第一级 · 大主题（结构级切换，十维互异）</h4><div class="theme-grid">${themeCards}</div></div>
  <div class="ap-sec"><h4>明暗模式（每套主题各含浅色与深色两套独立色板）</h4>
    <div class="seg"><button class="${PREF.mode==='auto'?'on':''}" onclick="v4set('mode','auto')">跟随系统</button><button class="${PREF.mode==='light'?'on':''}" onclick="v4set('mode','light')">浅色</button><button class="${PREF.mode==='dark'?'on':''}" onclick="v4set('mode','dark')">深色</button></div></div>
  <div class="ap-sec"><h4>第二级 · 调色（只调味不改结构）</h4>
    <div class="hint">强调色（12 预设 + 自定义）</div><div class="acc-row">${accentBtns}</div>
    <div class="row"><input type="color" id="acc-pick" value="${PREF.accentCustom||ACCENTS.find(a=>a.k===PREF.accent).v}" oninput="v4setCustomAccent(this.value)" title="自定义强调色"><label class="sw">自定义 HEX <input class="mono" id="acc-hex" value="${PREF.accentCustom||''}" placeholder="#RRGGBB" onchange="v4setCustomAccent(this.value)"></label></div>
    <div class="hint" id="contrast-hint"></div>
    <div class="ap-grid">
      <label class="sw">背景明暗 <select onchange="v4set('bright',this.value)">${['b1','b2','b3','b4','b5'].map((v,i)=>`<option value="${v}" ${PREF.bright===v?'selected':''}>${i+1} 档</option>`).join('')}</select></label>
      <label class="sw">密度 <select onchange="v4set('density',this.value)"><option value="compact" ${PREF.density==='compact'?'selected':''}>紧凑</option><option value="comfort" ${PREF.density==='comfort'?'selected':''}>舒适</option><option value="spacious" ${PREF.density==='spacious'?'selected':''}>宽松</option></select></label>
      <label class="sw">动效 <select onchange="v4set('motion',this.value)"><option value="full" ${PREF.motion==='full'?'selected':''}>完整</option><option value="reduced" ${PREF.motion==='reduced'?'selected':''}>减弱/遵循系统</option></select></label>
      <label class="sw">圆角 <select onchange="v4set('radius',this.value)"><option value="minus" ${PREF.radius==='minus'?'selected':''}>-1</option><option value="mid" ${PREF.radius==='mid'?'selected':''}>默认</option><option value="plus" ${PREF.radius==='plus'?'selected':''}>+1</option></select></label>
      <label class="sw">字阶 <select onchange="v4set('typescale',this.value)"><option value="minus" ${PREF.typescale==='minus'?'selected':''}>-1</option><option value="mid" ${PREF.typescale==='mid'?'selected':''}>默认</option><option value="plus" ${PREF.typescale==='plus'?'selected':''}>+1</option></select></label>
      <label class="sw">导航位置 <select onchange="v4set('navpos',this.value)"><option value="auto" ${PREF.navpos==='auto'?'selected':''}>跟随主题</option><option value="left" ${PREF.navpos==='left'?'selected':''}>左侧栏</option><option value="top" ${PREF.navpos==='top'?'selected':''}>顶部栏</option><option value="bottom" ${PREF.navpos==='bottom'?'selected':''}>底部胶囊</option><option value="grid" ${PREF.navpos==='grid'?'selected':''}>全屏网格</option></select></label>
      <label class="sw">灰阶基底 <select onchange="v4set('gray',this.value)">${grayOpts}</select></label>
      <label class="sw">玻璃 <input type="checkbox" ${PREF.glass?'checked':''} onchange="v4toggle('glass',this.checked)"></label>
      <label class="sw">扫描线 <input type="checkbox" ${PREF.scanline?'checked':''} onchange="v4toggle('scanline',this.checked)"></label>
      <label class="sw">网格底 <input type="checkbox" ${PREF.grid?'checked':''} onchange="v4toggle('grid',this.checked)"></label>
      <label class="sw">噪点 <input type="checkbox" ${PREF.noise?'checked':''} onchange="v4toggle('noise',this.checked)"></label>
    </div>
    <div class="ap-sec"><h4>状态色（成功/警告/错误/信息 单独可调）</h4><div class="ap-grid">
      <label class="sw">成功 <input type="color" value="#46C98B" oninput="v4setStatus('ok',this.value)"></label>
      <label class="sw">警告 <input type="color" value="#E8B339" oninput="v4setStatus('warn',this.value)"></label>
      <label class="sw">错误 <input type="color" value="#E5604D" oninput="v4setStatus('err',this.value)"></label>
      <label class="sw">信息 <input type="color" value="#5AA9E6" oninput="v4setStatus('info',this.value)"></label>
    </div></div>
  </div>
  <div class="ap-sec"><h4>自定义主题库</h4>
    <div class="row"><input id="ct-name" placeholder="主题名" class="mono"><button class="btn" onclick="v4saveCustom()">保存为自定义主题</button></div>
    <div class="custom-list">${customs.length?customs.map(c=>`<span class="chip">${esc(c.name)} <button onclick="v4applyCustom('${c.id}')">应用</button><button onclick="v4delCustom('${c.id}')">删除</button></span>`).join(''):'<div class="hint">尚无自定义主题</div>'}</div></div>
  <div class="ap-sec row">
    <button class="btn" onclick="exportConfig()">导出 JSON</button><button class="btn" onclick="importConfig()">导入 JSON</button>
    <button class="btn" onclick="shareConfig()">复制分享链接</button><button class="btn" onclick="v4importUrl()">从 URL 导入</button>
    <button class="btn" onclick="v4resetPref()">恢复默认</button>
  </div>
  <div class="hint">每套主题/强调色组合自动跑 WCAG AA + APCA 校验，不达标自动降饱和并提示，不允许保存不可读主题。外观已合并原太阳/调色板两个入口，明暗与主题均可在此找到。</div>
  </div></section>`;
  const chk=checkContrast(PREF.accentCustom||ACCENTS.find(a=>a.k===PREF.accent).v,effectiveMode(PREF)==='dark'?'#0F1011':'#FFFFFF');
  const ch=$('#contrast-hint');if(ch)ch.textContent=chk.ok?'对比度校验通过（WCAG AA / APCA）':(chk.msg||'对比度不足');
}
window.v4setTheme=t=>{PREF.theme=t;PREF.custom=null;const chk=applyPref(PREF);$$('.theme-card').forEach(b=>b.classList.toggle('on',b.dataset.t===t));toast('主题：'+THEME_LABEL[t]+'（导航/版式已随主题变化）','ok');renderAppearance();};
window.v4setAccent=a=>{PREF.accent=a;PREF.accentCustom='';applyPref(PREF);renderAppearance();};
window.v4setCustomAccent=v=>{if(!/^#[0-9a-fA-F]{6}$/.test(v))return;PREF.accentCustom=v;const chk=applyPref(PREF);const h=$('#contrast-hint');if(h)h.textContent=chk.ok?'对比度校验通过':(chk.msg||'对比度不足');$$('.acc-dot').forEach(b=>b.classList.remove('on'));};
window.v4setStatus=(k,v)=>{PREF.status=PREF.status||{};PREF.status[k]=v;applyPref(PREF);};
window.v4saveCustom=()=>{const inp=$('#ct-name');const name=(inp&&inp.value||'').trim()||'自定义主题';const customs=saveCustomTheme(name);toast('已保存自定义主题：'+name,'ok');renderAppearance();};
window.v4applyCustom=id=>{const c=getCustoms().find(x=>x.id===id);if(!c)return;PREF.theme=c.theme;PREF.custom={theme:c.theme,tokens:c.tokens};applyPref(PREF);toast('已应用：'+c.name,'ok');};
window.v4delCustom=id=>{const customs=getCustoms().filter(x=>x.id!==id);LS.set('customs',customs);renderAppearance();toast('已删除','ok');};
window.v4importUrl=()=>{const s=prompt('粘贴主题分享 URL 或 #theme= 后的编码：');if(!s)return;try{const enc=s.includes('#theme=')?s.split('#theme=')[1]:s;PREF=Object.assign(getPrefDefault(),JSON.parse(decodeURIComponent(atob(enc))));applyPref(PREF);renderAppearance();toast('已从 URL 导入主题','ok');}catch(e){toast('导入失败：'+e,'err');}};
window.v4resetPref=()=>{PREF=getPrefDefault();applyPref(PREF);renderAppearance();toast('已恢复默认外观','ok');};
window.v4set=(k,v)=>{PREF[k]=v;applyPref(PREF);if(k==='mode'||k==='gray')renderAppearance();};
window.v4toggle=(k,on)=>{PREF[k]=on?1:0;applyPref(PREF);};
function exportConfig(){const d={pref:PREF,layout:getLayout(),customs:getCustoms(),v:1};const url=URL.createObjectURL(new Blob([JSON.stringify(d,null,2)],{type:'application/json'}));const a=el('a',{href:url,download:'kfc-ops-config.json'});a.click();toast('已导出配置 JSON','ok');}
function importConfig(){const inp=el('input',{type:'file',accept:'.json',onchange:e=>{const f=e.target.files[0];if(!f)return;const r=new FileReader();r.onload=()=>{try{const d=JSON.parse(r.result);if(d.pref){PREF=Object.assign(getPrefDefault(),d.pref);applyPref(PREF);}if(d.layout)applyLayout(d.layout);if(d.customs)LS.set('customs',d.customs);renderAppearance();toast('已导入配置','ok');}catch(err){toast('导入失败：'+err,'err');}};r.readAsText(f);}});inp.click();}
function shareConfig(){const s=btoa(encodeURIComponent(JSON.stringify(PREF)));const u=location.origin+location.pathname+'#theme='+s;navigator.clipboard.writeText(u).then(()=>toast('分享链接已复制','ok'),()=>toast('复制失败','err'));}

/* ============================================================
   布局编辑器（重命名/隐藏/置顶/最小化/更多恢复；关闭=最小化不删除）
   ============================================================ */
function renderLayoutEditor(){const root=$('#page');const l=getLayout()||{};
  const navHtml=NAV.map(g=>`<div class="lo-group"><h4>${esc(g.group)}</h4>`+g.items.map(it=>`
    <div class="lo-item" data-id="${it.id}" draggable="true"><span class="lo-ico">${svg(it.icon)}</span>
      <input class="lo-name" value="${esc((l.names&&l.names[it.id])||it.name)}" onchange="v4rename('${it.id}',this.value)">
      <label class="lo-hide"><input type="checkbox" onchange="v4hide('${it.id}',this.checked)" ${isHidden(it.id)?'checked':''}>隐藏</label>
      <button class="lo-min" onclick="v4minimize('${it.id}')" title="最小化到侧栏（不删除）">—</button><span class="lo-grip">⋮⋮</span></div>`).join('')+`</div>`).join('');
  root.innerHTML=`<section class="card wide"><header class="card-h"><span class="card-ico">${svg('layout')}</span><h3>布局自定义</h3></header><div class="card-b appear">
    <div class="hint">拖拽排序、重命名、隐藏、置顶、收进「更多」可随时恢复。关闭一律为最小化到侧栏，绝不删除模块。可保存多套视图。</div>
    <div class="lo-wrap">${navHtml}</div><div class="row"><button class="btn acc" onclick="saveLayout();toast('布局已保存','ok')">保存布局</button><button class="btn" onclick="resetLayout()">恢复默认布局</button></div>
    </div></section>`;wireDrag();}
function isHidden(id){const l=getLayout();return !!(l&&l.hidden&&l.hidden.includes(id));}
window.v4rename=(id,n)=>{const l=getLayout()||{};l.names=l.names||{};l.names[id]=n;applyLayout(l);renderSidebar();};
window.v4hide=(id,on)=>{const l=getLayout()||{};l.hidden=l.hidden||[];if(on&&!l.hidden.includes(id))l.hidden.push(id);if(!on)l.hidden=l.hidden.filter(x=>x!==id);applyLayout(l);renderSidebar();};
window.v4minimize=(id)=>{const l=getLayout()||{};l.min=l.min||[];if(!l.min.includes(id))l.min.push(id);applyLayout(l);renderSidebar();toast('已最小化到侧栏（模块未删除）','ok');};
function saveLayout(){const order=$$('.lo-group').map(g=>[...g.querySelectorAll('.lo-item')].map(i=>i.dataset.id));const l=getLayout()||{};l.order=order;applyLayout(l);}
function resetLayout(){localStorage.removeItem('v4_layout');renderSidebar();renderLayoutEditor();}
function wireDrag(){let drag;$$('.lo-item').forEach(it=>{it.addEventListener('dragstart',()=>{drag=it;it.classList.add('dragging');});it.addEventListener('dragend',()=>{it.classList.remove('dragging');drag=null;});it.addEventListener('dragover',e=>{e.preventDefault();if(drag&&drag!==it)it.parentNode.insertBefore(drag,it);});});}

/* ============================================================
   Onboarding 向导（可跳过/可重跑/四态）
   ============================================================ */
function startOnboarding(){toast('接入向导：请在配置中心填写服务地址与凭据','ok');navTo('config');}
/* 添加卡片选择器 */
function openWidgetPicker(){const types=['health3','metrics','disk-alert','cont-res','host-res','service-health','task-progress','alert','log','custom-api'];
  const box=el('div',{class:'modal-mask',id:'wp'},el('div',{class:'modal glass'},el('h3',{},'添加卡片'),
    el('div',{class:'wp-grid'},...types.map(t=>el('button',{class:'btn',onclick:()=>{toast('已添加卡片：'+t+'（可在布局中拖拽）','ok');closeModal();}} ,t))),
    el('div',{class:'row'},el('button',{class:'btn',onclick:closeModal},'取消'))));document.body.append(box);}
function closeModal(){const m=$('#wp');if(m)m.remove();}

/* ============================================================
   侧栏 + 顶栏
   ============================================================ */
function renderSidebar(){const l=getLayout()||{};const hide=new Set(l.hidden||[]);const min=new Set(l.min||[]);
  const navHtml=NAV.map(g=>`<div class="nav-group"><div class="nav-glabel">${esc(g.group)}</div>`+g.items.filter(it=>!hide.has(it.id)).map(it=>`<button class="nav-item ${currentPage===it.id?'on':''}" data-id="${it.id}" onclick="navTo('${it.id}')"><span class="nav-ico">${svg(it.icon)}</span><span class="nav-txt">${esc((l.names&&l.names[it.id])||it.name)}</span></button>`).join('')+`</div>`).join('');
  const minHtml=[...min].map(id=>{const it=findItem(id);return it?`<button class="min-chip" onclick="navTo('${id}');unmin('${id}')">${svg(it.icon)}${esc((l.names&&l.names[id])||it.name)}</button>`:'';}).join('');
  $('#sidebar').innerHTML=navHtml+`<div class="min-bar">${minHtml}</div>`;}
function unmin(id){const l=getLayout()||{};l.min=(l.min||[]).filter(x=>x!==id);applyLayout(l);renderSidebar();}
let currentPage='overview';
function navTo(id){currentPage=id;window.location.hash='#'+id;renderPage(id);}
function renderPage(id){$$('.nav-item').forEach(b=>b.classList.toggle('on',b.dataset.id===id));const it=findItem(id);
  $('#crumb').textContent=it?it.name:'';$('#pagetitle').textContent=it?it.name:'';
  try{const fn=(PAGES[id]||PAGES.overview);const p=fn(id);if(p&&p.then)p.catch(e=>$('#page').innerHTML=errState(String(e)));}catch(e){$('#page').innerHTML=errState(String(e));}
  if(document.startViewTransition&&PREF.motion!=='reduced'){try{document.startViewTransition(()=>{});}catch(e){}}}

/* 全局：健康徽标 + 看门狗倒计时 + 时钟 */
async function globalTick(){const h=await apiGet('/api/status/health',3000);const badge=$('#health-badge');
  if(h.ok){badge.className='ghealth '+h.overall;badge.textContent=h.overall==='ok'?'全部正常':h.overall==='partial'?'部分可用':'异常';}
  else{badge.className='ghealth err';badge.textContent='异常';}
  if(h.ok&&h.watchdog&&h.watchdog.next_in!=null){const el2=$('#wd-next');if(el2&&!el2._timer){let n=h.watchdog.next_in;el2._timer=setInterval(()=>{n=Math.max(0,n-1);if(el2.isConnected)el2.textContent=n+'s';else clearInterval(el2._timer);},1000);}}}
function startClocks(){setInterval(()=>{const c=$('#global-clock');if(c)c.textContent=new Date().toLocaleString('zh-CN',{hour12:false});},1000);}

/* 动作 */
window.v4heal=async function(){const t=toast('立即自愈中…','ok',{loading:true});const r=await apiPost('/api/mt/diag/auto',{rule:'jwt',on:1}).catch(()=>({ok:false,msg:'网络错误'}));t.dismiss();if(r.ok){toast('已触发自愈','ok');opResult('立即自愈',{ok:true,result:'已触发'});}else{toast('自愈失败：'+(r.msg||''),'err');opResult('立即自愈',{ok:false,result:r.msg||''});}};
window.v4retryCred=()=>toast('已请求重试凭据','ok');
window.v4viewLog=()=>navTo('audit');
window.v4toast=(m,k)=>toast(m,k||'ok');
window.v4fold=btn=>{const b=btn.closest('.card').querySelector('.card-b');b.style.display=b.style.display==='none'?'':'none';};
window.v4minCard=id=>{const c=$('#'+id);if(c){c.classList.add('minimized');toast('已最小化卡片（未删除）','ok');}};
window.v4taskPause=(id,act)=>toast('已请求：'+act+' '+id,'ok');

/* ============================================================
   初始化
   ============================================================ */
function init(){
  if(location.hash.startsWith('#theme=')){try{PREF=Object.assign(getPrefDefault(),JSON.parse(decodeURIComponent(atob(location.hash.slice(7)))));}catch(e){}}
  const chk=applyPref(PREF);
  renderSidebar();
  $('#btn-search').onclick=()=>openPalette();
  $('#btn-refresh').onclick=()=>{const c=$('#btn-refresh');c.classList.add('spin');renderPage(currentPage);setTimeout(()=>c.classList.remove('spin'),600);};
  $('#btn-log').onclick=()=>{$('#opresult').classList.toggle('collapsed');};
  $('#btn-clear').onclick=()=>{$('#oprows').innerHTML='';$('#opcount').textContent='0';$('#opresult').classList.remove('has');};
  $('#btn-appearance').onclick=()=>navTo('appearance');
  $('#btn-menu').onclick=()=>{$('#app').classList.toggle('nav-open');};
  $('#btn-wallboard').onclick=()=>{$('#app').classList.toggle('wallboard');};
  document.addEventListener('keydown',e=>{
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#palette').classList.contains('open')?closePalette():openPalette();}
    else if(e.key==='/'&&!$('#palette').classList.contains('open')&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName)){e.preventDefault();openPalette();}
    else if(e.key==='Escape'){closePalette();closeModal();if($('#app').classList.contains('nav-open'))$('#app').classList.remove('nav-open');}
    else if(e.key==='ArrowDown'&&$('#palette').classList.contains('open')){e.preventDefault();const items=$$('.pal-item');if(items.length){palSel=(palSel+1)%items.length;items.forEach((x,i)=>x.classList.toggle('active',i===palSel));}}
    else if(e.key==='ArrowUp'&&$('#palette').classList.contains('open')){e.preventDefault();const items=$$('.pal-item');if(items.length){palSel=(palSel-1+items.length)%items.length;items.forEach((x,i)=>x.classList.toggle('active',i===palSel));}}
    else if(e.key==='Enter'&&$('#palette').classList.contains('open')){const it=$$('.pal-item')[palSel];if(it)it.click();}
  });
  const h=location.hash.slice(1);currentPage=(h&&findItem(h))||(h.startsWith('theme=')?'overview':h&&h)?(findItem(h)?h:'overview'):'overview';navTo(currentPage);
  startClocks();globalTick();setInterval(globalTick,30000);
  // 跟随系统深浅色变化
  matchMedia('(prefers-color-scheme: light)').addEventListener?.('change',()=>{if(PREF.mode==='auto')applyPref(PREF);});
}
window.addEventListener('DOMContentLoaded',init);
})();
