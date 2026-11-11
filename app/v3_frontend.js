/* ══════════ V3.0 插件平台前端（Plugin Platform）══════════
   插件中心 / 服务注册 / 事件总线 / 架构 —— 全部真实后端数据，零模拟。 */
const V3={view:'plugins',plugTab:'installed',svcType:'',timer:null};

async function renderV3(){
  const v=document.getElementById('view');
  if(V3.timer){clearInterval(V3.timer);V3.timer=null;}
  const tabs=[['plugins','🧩 插件中心'],['services','🔌 服务实例'],['events','📡 事件总线'],['arch','🏗 平台架构']];
  v.innerHTML=`<div class="card" style="margin-bottom:12px;background:linear-gradient(135deg,rgba(126,217,87,.10),transparent 60%)">
    <h3>🏗️ KFC Media Hub <span style="color:var(--hue-a)">V3.0</span> · 插件化平台
      <span style="font-weight:400;font-size:11px;color:var(--muted)">插件生态 · 统一服务注册 · 事件驱动 · 子进程隔离</span></h3>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
      ${tabs.map(t=>`<button class="btn ${V3.view===t[0]?'acc':''}" onclick="V3.view='${t[0]}';renderV3()">${t[1]}</button>`).join('')}
    </div><div id="v3_overview" style="margin-top:10px"></div></div>
    <div id="v3_body"><div class="empty"><span class="spin"></span> 加载…</div></div>`;
  try{
    const ov=await apiGet('/api/v3/overview',0);
    const ob=document.getElementById('v3_overview');
    if(ob&&ov&&ov.ok)ob.innerHTML=`<div style="display:flex;gap:8px;flex-wrap:wrap">
      <span class="tag ok">插件 ${ov.plugins.enabled}/${ov.plugins.total} 启用</span>
      <span class="tag">服务 ${ov.services.healthy}/${ov.services.total} 健康</span>
      <span class="tag ${ov.events.pending?'warn':''}">事件待处理 ${ov.events.pending}</span>
      <span class="tag ${ov.events.dead?'err':''}">死信 ${ov.events.dead}</span>
      <span class="tag">平台 v${ov.version}</span></div>`;
    if(V3.view==='plugins')await v3Plugins();
    else if(V3.view==='services')await v3Services();
    else if(V3.view==='events')await v3Events();
    else v3Arch();
  }catch(e){const b=document.getElementById('v3_body');if(b)b.innerHTML='<div class="empty">平台接口异常：'+esc(String(e.message||e)).slice(0,100)+'</div>';}
}

function v3Modal(html){
  const ov=document.createElement('div');
  ov.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.62);z-index:999;display:flex;align-items:center;justify-content:center;padding:18px';
  ov.innerHTML='<div style="background:var(--bg2);border:1px solid var(--bd);border-radius:14px;padding:18px;max-width:560px;width:100%;max-height:85vh;overflow:auto">'+html+'</div>';
  document.body.appendChild(ov);
  ov.onclick=e=>{if(e.target===ov)ov.remove();};
  return ov;
}

/* ── 插件中心 ── */
async function v3Plugins(){
  const b=document.getElementById('v3_body');
  const tabs=[['installed','已安装'],['market','插件市场'],['perms','权限中心'],['history','安装历史']];
  b.innerHTML=`<div class="card"><div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
    ${tabs.map(t=>`<button class="btn sm ${V3.plugTab===t[0]?'acc':''}" onclick="V3.plugTab='${t[0]}';v3Plugins()">${t[1]}</button>`).join('')}
    <span style="flex:1"></span><button class="btn sm" onclick="v3InstallDialog()">➕ 安装插件</button>
    <button class="btn sm" onclick="renderV3()">↻</button></div>
    <div id="v3_plugbody"><div class="sub"><span class="spin"></span> 加载…</div></div></div>`;
  const box=document.getElementById('v3_plugbody');
  if(V3.plugTab==='installed'){
    const d=await apiGet('/api/v3/plugins');
    const hl=await apiGet('/api/v3/plugins/health');
    const hm={};((hl&&hl.health)||[]).forEach(h=>hm[h.plugin_id]=h.status);
    box.innerHTML=((d&&d.plugins)||[]).length?d.plugins.map(p=>{
      const st=hm[p.plugin_id]||p.status||'unknown';
      const c=st==='running'?'ok':(st==='crashed'||st==='error'?'err':'warn');
      const label={running:'● 运行中',stopped:'○ 已停止',crashed:'✗ 崩溃',installed:'· 未启用',error:'✗ 错误'}[st]||st;
      return `<div style="border:1px solid var(--bd);border-radius:12px;padding:12px;margin:8px 0;background:var(--bg2)">
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <span style="font-size:24px">🧩</span><div style="flex:1;min-width:150px">
            <b>${esc(p.name)}</b> <span class="sub">v${esc(p.version)} · ${esc(p.author||'—')}</span>
            <div class="sub" style="font-size:11px;margin-top:3px">${esc(p.description||'').slice(0,100)}</div></div>
          <span class="tag ${c}">${label}</span></div>
        <div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap">
          ${p.enabled?`<button class="btn sm" onclick="v3Act('disable','${esc(p.plugin_id)}')">⏹ 停用</button>
            <button class="btn sm" onclick="v3Act('restart','${esc(p.plugin_id)}')">🔄 重启</button>`:
            `<button class="btn sm acc" onclick="v3Perm('${esc(p.plugin_id)}')">🔐 审核权限并启用</button>`}
          <button class="btn sm" onclick="v3Detail('${esc(p.plugin_id)}')">📋 详情/日志</button>
          <button class="btn sm warn" onclick="if(confirm('卸载 ${esc(p.name)}？'))v3Act('uninstall','${esc(p.plugin_id)}')">🗑 卸载</button>
        </div></div>`;}).join(''):'<div class="empty">还没有插件 · 到「插件市场」装内置示例验证运行时</div>';
  } else if(V3.plugTab==='market'){
    const s=await apiGet('/api/v3/sdk/schema');
    box.innerHTML=`<div style="border:1px solid color-mix(in srgb,var(--hue-a) 40%,transparent);border-radius:12px;padding:13px;margin:8px 0">
      <b>🏗 内置示例 · Heartbeat 心跳插件</b>
      <div class="sub" style="font-size:11px;margin:4px 0">真进程隔离运行，验证 日志/配置/独立KV/事件发布订阅/服务读取，不改任何业务状态。</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap"><span class="tag">7 项权限</span><span class="tag">v1.0.0</span><span class="tag">官方</span></div>
      <button class="btn acc" style="margin-top:10px" onclick="v3InstallDemo()">📦 安装内置示例</button></div>
      <div class="sub" style="margin-top:12px"><b>第三方来源</b>：本地 ZIP / Git 仓库由「➕ 安装插件」提交 Manifest 接入。
      宿主 v${esc((s&&s.host_version)||'3.0.0')}，必需字段 ${esc(((s&&s.required)||[]).join(' / '))}。</div>`;
  } else if(V3.plugTab==='perms'){
    const d=await apiGet('/api/v3/permissions');
    box.innerHTML=`<div class="sub" style="margin-bottom:8px">细粒度权限点：插件安装后须在「审核权限并启用」逐项授权。</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:7px">
      ${((d&&d.permissions)||[]).map(p=>`<div style="padding:8px 11px;border:1px solid var(--bd);border-radius:9px;background:var(--bg2)">
        <b style="font-size:11px">${esc(p.key)}</b><div class="sub" style="font-size:10px">${esc(p.label)}</div></div>`).join('')}</div>`;
  } else {
    const d=await apiGet('/api/v3/history');
    box.innerHTML=((d&&d.history)||[]).length?d.history.map(h=>`<div style="padding:6px 0;border-bottom:1px dashed var(--bd);font-size:12px">
      <span class="tag">${esc(h.action)}</span> <b>${esc(h.plugin_id)}</b> <span class="sub">${esc(h.detail||'')} · ${esc(h.ts)}</span></div>`).join(''):'<div class="empty">暂无安装历史</div>';
  }
}

function v3DemoManifest(){return {plugin_id:'heartbeat_demo',name:'心跳演示插件',version:'1.0.0',author:'KFC Media Hub 官方',
  description:'内置示例：验证插件运行时（进程隔离/日志/配置/KV/事件/服务读取），不改变业务状态。',
  min_host_version:'3.0.0',max_host_version:'9.9.9',entrypoint:'plugin.py',category:'系统工具',
  permissions:['log.write','config.read','kv.read','kv.write','event.publish','event.subscribe','service.read'],
  configuration_schema:{interval:{type:'number',label:'心跳间隔(秒)',default:30}},
  navigation:[{id:'v3_heartbeat',name:'心跳演示',icon:'💓',page:'plugin:heartbeat_demo'}],
  dashboard_widgets:[{id:'heartbeat_status',title:'插件心跳',type:'stat'}],
  capabilities:['plugin_heartbeat'],event_subscriptions:[{event_type:'plugin.heartbeat'}],
  background_tasks:[{id:'heartbeat_loop',type:'persistent'}],api_routes:[],lifecycle_hooks:{},dependencies:[]};}

function v3InstallDemo(){apiPost('/api/v3/plugin/install',{manifest:v3DemoManifest(),source:'builtin'}).then(r=>{
  toast((r&&r.msg)||'失败',r&&r.ok?'ok':'err');v3Plugins();});}

function v3InstallDialog(){
  const ov=v3Modal(`<h3>➕ 安装插件</h3>
    <div class="sub" style="margin:6px 0">粘贴标准 Manifest JSON（含 plugin_id/name/version/entrypoint/permissions）。本地 ZIP / Git 仓库走后续上传通道。</div>
    <textarea id="v3_mf" rows="11" placeholder='{"plugin_id":"my_plugin","name":"我的插件","version":"1.0.0","entrypoint":"plugin.py","permissions":["log.write"]}' style="width:100%;padding:10px;border-radius:9px;background:var(--bg);color:var(--fg);border:1px solid var(--bd);font-family:monospace;font-size:11px"></textarea>
    <div style="display:flex;gap:8px;margin-top:10px;justify-content:flex-end">
      <button class="btn" onclick="this.closest('[style*=fixed]').remove()">取消</button>
      <button class="btn acc" onclick="v3DoInstall()">📦 安装</button></div>`);
}
function v3DoInstall(){
  let m;try{m=JSON.parse(document.getElementById('v3_mf').value);}catch(e){return toast('Manifest JSON 格式错误','err');}
  apiPost('/api/v3/plugin/install',{manifest:m,source:'manual'}).then(r=>{
    toast((r&&r.msg)||'失败',r&&r.ok?'ok':'err');if(r&&r.ok){document.querySelector('[style*=fixed]')?.remove();v3Plugins();}});
}

async function v3Perm(pid){
  const d=await apiGet('/api/v3/plugins');
  const p=((d&&d.plugins)||[]).find(x=>x.plugin_id===pid);
  const perms=Array.isArray(p&&p.permissions)?p.permissions:[];
  v3Modal(`<h3>🔐 权限审核 · ${esc(p?p.name:pid)}</h3>
    <div class="sub" style="margin:6px 0">插件声明以下权限，请逐项确认授权（最小权限原则）。</div>
    ${perms.map(x=>`<label style="display:flex;gap:8px;align-items:center;padding:6px 0">
      <input type="checkbox" checked class="v3-perm" value="${esc(x)}" style="accent-color:var(--hue-a)"><b style="font-size:12px">${esc(x)}</b></label>`).join('')||'<div class="sub">未声明权限</div>'}
    <div style="display:flex;gap:8px;margin-top:12px;justify-content:flex-end">
      <button class="btn" onclick="this.closest('[style*=fixed]').remove()">取消</button>
      <button class="btn acc" onclick="v3Enable('${esc(pid)}')">✅ 授权并启用</button></div>`);
}
function v3Enable(pid){
  const granted=[...document.querySelectorAll('.v3-perm:checked')].map(c=>c.value);
  apiPost('/api/v3/plugin/enable',{plugin_id:pid,granted_permissions:granted}).then(r=>{
    toast((r&&r.msg)||'失败',r&&r.ok?'ok':'err');if(r&&r.ok){document.querySelector('[style*=fixed]')?.remove();v3Plugins();}});
}
function v3Act(act,pid){apiPost('/api/v3/plugin/'+act,{plugin_id:pid}).then(r=>{
  toast((r&&r.msg)||'完成',r&&r.ok?'ok':'err');v3Plugins();});}

async function v3Detail(pid){
  const d=await apiGet('/api/v3/plugins');
  const p=((d&&d.plugins)||[]).find(x=>x.plugin_id===pid);
  const cap=await apiGet('/api/v3/plugins/capabilities');
  const caps=((cap&&cap.capabilities)||[]).filter(c=>c.plugin_id===pid);
  v3Modal(`<h3>📋 ${esc(p?p.name:pid)}</h3>
    <div class="sub">ID <b>${esc(pid)}</b> · v${esc(p?p.version:'')} · ${esc(p?p.author:'')}</div>
    <div style="margin-top:10px"><b style="font-size:12px">已注册能力（自动联动）</b>
      ${caps.length?caps.map(c=>`<div style="padding:4px 0;font-size:11px;border-bottom:1px dashed var(--bd)"><span class="tag">${esc(c.kind)}</span> ${esc(c.key)}</div>`).join(''):'<div class="sub">无</div>'}</div>
    <div style="margin-top:10px"><b style="font-size:12px">运行状态</b><pre style="background:var(--bg);padding:10px;border-radius:8px;font-size:10px;max-height:180px;overflow:auto">${esc((p&&p.error)||'无错误')}</pre></div>
    <button class="btn" style="margin-top:10px" onclick="this.closest('[style*=fixed]').remove()">关闭</button>`);
}

/* ── 服务注册中心 ── */
async function v3Services(){
  const b=document.getElementById('v3_body');
  const d=await apiGet('/api/v3/services'+(V3.svcType?'?type='+encodeURIComponent(V3.svcType):''));
  const types=['emby','jellyfin','plex','qbittorrent','moviepilot','mediatidy','clouddrive2','openlist','resourcetool','telegram','ai'];
  b.innerHTML=`<div class="card"><div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
    <select class="btn sm" onchange="V3.svcType=this.value;v3Services()">
      <option value="">全部类型</option>${types.map(t=>`<option value="${t}" ${V3.svcType===t?'selected':''}>${t}</option>`).join('')}</select>
    <span style="flex:1"></span><button class="btn sm acc" onclick="v3SvcDialog()">➕ 添加实例</button>
    <button class="btn sm" onclick="v3Services()">↻</button></div>
    <div class="sub" style="font-size:11px;margin-bottom:8px">插件=功能扩展，实例=你接入的外部服务，二者解耦：装一个 Emby 插件可加 N 个实例。</div>
    ${((d&&d.instances)||[]).length?d.instances.map(s=>`<div style="border:1px solid var(--bd);border-radius:11px;padding:11px;margin:7px 0;background:var(--bg2)">
      <div style="display:flex;gap:9px;align-items:center;flex-wrap:wrap"><b>${esc(s.name)}</b><span class="tag">${esc(s.type)}</span>
        <span class="tag ${s.health==='healthy'?'ok':(s.health==='unhealthy'?'err':'warn')}">${{healthy:'● 健康',unhealthy:'✗ 异常',unknown:'○ 未测'}[s.health]||s.health}</span>
        <span class="sub">${esc(s.endpoint||'')}</span><span style="flex:1"></span>
        <span class="sub" style="font-size:10px">${(s.capabilities||[]).join(' · ')||'能力未识别'}</span></div>
      <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
        <button class="btn sm" onclick="v3SvcTest('${esc(s.id)}')">🔍 测试连接</button>
        <button class="btn sm" onclick="v3SvcDialog('${esc(s.id)}')">✏️ 编辑</button>
        <button class="btn sm warn" onclick="if(confirm('删除实例 ${esc(s.name)}？'))v3SvcDel('${esc(s.id)}')">🗑</button></div>
      ${s.last_error?`<div class="sub" style="font-size:10px;color:#f87171;margin-top:5px">${esc(s.last_error)}</div>`:''}
    </div>`).join(''):'<div class="empty">还没有服务实例 · 点「➕ 添加实例」接入你的第一个服务</div>'}</div>`;
}

function v3SvcDialog(id){
  v3Modal(`<h3>${id?'✏️ 编辑':'➕ 添加'}服务实例</h3>
    <div style="display:flex;flex-direction:column;gap:8px;margin-top:10px">
      <input id="v3s_id" class="btn sm" placeholder="实例 ID（英文数字，如 emby-home）" value="${esc(id||'')}" ${id?'readonly':''}>
      <input id="v3s_name" class="btn sm" placeholder="显示名称（如 主号 Emby）">
      <select id="v3s_type" class="btn sm">${['emby','jellyfin','plex','qbittorrent','moviepilot','mediatidy','clouddrive2','openlist','resourcetool','telegram','ai'].map(t=>`<option>${t}</option>`).join('')}</select>
      <input id="v3s_ep" class="btn sm" placeholder="API 地址（如 http://192.168.x.x:8096）">
      <input id="v3s_key" class="btn sm" type="password" placeholder="API Key / Token（加密落盘、脱敏显示）"></div>
    <div style="display:flex;gap:8px;margin-top:12px;justify-content:flex-end">
      <button class="btn" onclick="this.closest('[style*=fixed]').remove()">取消</button>
      <button class="btn acc" onclick="v3SvcSave()">💾 保存</button></div>`);
}
function v3SvcSave(){
  const arg={id:document.getElementById('v3s_id').value.trim()||undefined,
    name:document.getElementById('v3s_name').value.trim(),type:document.getElementById('v3s_type').value,
    endpoint:document.getElementById('v3s_ep').value.trim(),config:{},enabled:1};
  const k=document.getElementById('v3s_key').value.trim();if(k)arg.config.api_key=k;
  apiPost('/api/v3/service/upsert',arg).then(r=>{toast((r&&r.msg)||'失败',r&&r.ok?'ok':'err');
    if(r&&r.ok){document.querySelector('[style*=fixed]')?.remove();v3Services();}});
}
function v3SvcTest(id){toast('正在真实测试连接…','ok');apiPost('/api/v3/service/test',{id}).then(r=>{
  toast((r&&r.msg||'').slice(0,100),r&&r.ok?'ok':'err');v3Services();});}
function v3SvcDel(id){apiPost('/api/v3/service/delete',{id}).then(r=>{toast((r&&r.msg)||'已删除','ok');v3Services();});}

/* ── 事件总线 ── */
async function v3Events(){
  const b=document.getElementById('v3_body');
  const d=await apiGet('/api/v3/events?limit=100');
  b.innerHTML=`<div class="card"><div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
    <h4 style="margin:0">📡 事件总线 <span class="sub" style="font-weight:400;font-size:11px">唯一 ID · 去重 · 重试 · 死信</span></h4>
    <span style="flex:1"></span><button class="btn sm" onclick="v3PubTest()">📤 发测试事件</button>
    <button class="btn sm" onclick="v3Events()">↻</button></div>
    ${((d&&d.events)||[]).length?d.events.map(e=>`<div style="padding:8px 0;border-bottom:1px dashed var(--bd);font-size:11px">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span class="tag ${e.status==='processed'?'ok':(e.status==='dead'?'err':'warn')}">${esc(e.status)}</span>
        <b>${esc(e.type)}</b><span class="sub">${esc(e.created_at)} · 尝试 ${e.attempts}</span><span class="sub">来源 ${esc(e.source)}</span></div>
      <div class="sub" style="font-size:10px;margin-top:3px">${esc(JSON.stringify(e.data||{}).slice(0,140))}</div>
      ${e.last_error?`<div style="color:#f87171;font-size:10px">${esc(e.last_error)}</div>`:''}</div>`).join(''):'<div class="empty">暂无事件 · 点「发测试事件」验证总线</div>'}</div>`;
}
function v3PubTest(){apiPost('/api/v3/event/publish',{type:'v3.test.event',data:{msg:'面板测试事件',t:Date.now()},
  source:'ui',dedupe_key:'v3test-'+Date.now()}).then(r=>{toast((r&&r.msg)||'失败',r&&r.ok?'ok':'err');v3Events();});}

/* ── 架构说明（真实已落地） ── */
function v3Arch(){
  const b=document.getElementById('v3_body');
  b.innerHTML=`<div class="card"><h4>🏗️ V3.0 已落地的底层能力</h4>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:9px;margin-top:10px">
    ${[['🧩','插件 Manifest 标准','plugin_id/版本兼容/依赖/权限/导航/组件/路由/任务/事件订阅 全字段校验'],
       ['⚙️','插件生命周期','安装→权限审核→启用→健康检查→重启→停用→卸载（独立子进程，崩溃不影响宿主）'],
       ['🔐','细粒度权限','22 个权限点，逐项授权；SDK 回调 token 校验，插件只看见自己的数据'],
       ['🔌','服务注册中心','实例与插件解耦，N 实例；CRUD+能力发现+真实连通测试（非伪造）'],
       ['📡','事件总线','唯一 ID/去重键/重试 3 次/死信；插件按订阅权限拉取'],
       ['🧠','统一数据总线','导航/组件/路由/任务从能力注册表动态读取，新插件自动联动'],
       ['🗄','独立数据库','/dockroot/media-hub/v3/hub_v3.db —— 与 3.7GB MT 库完全隔离'],
       ['🛡','复用既有安全','sec_* 鉴权 + license 写闸；插件独立 token，不碰你的现有凭据']]
      .map(x=>`<div style="border:1px solid var(--bd);border-radius:11px;padding:12px;background:var(--bg2)">
        <div style="font-size:22px">${x[0]}</div><b style="font-size:13px">${x[1]}</b>
        <div class="sub" style="font-size:11px;margin-top:4px;line-height:1.6">${x[2]}</div></div>`).join('')}</div>
    <div class="sub" style="margin-top:12px;font-size:11px;line-height:1.8"><b>分阶段路线</b>：① 审计 ✅ → ② 架构 ✅ → ③ <b>插件运行时+服务注册（当前包）</b> →
      ④ 业务迁移（43 菜单/327 API 逐步接入，不删原功能）→ ⑤ 自动化任务中心 → ⑥ 液态玻璃 UI → ⑦ 全量测试 → ⑧ 部署交付。</div>
  </div>`;
}
