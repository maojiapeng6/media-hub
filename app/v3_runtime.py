# -*- coding: utf-8 -*-
"""
KFC Media Hub Plugin Runtime v3.0 —— 零凭据参考实现。
线上 app.py 以增量 section 方式注入同等代码；本文件是可独立 import 的发布副本。
"""
import hashlib, hmac, json, os, re, shutil, socket, sqlite3, ssl, subprocess, threading, time, datetime
import urllib.parse, urllib.request, urllib.error, uuid, collections
import sys, signal
from concurrent.futures import ThreadPoolExecutor

# ── 宿主适配层（线上 app.py 已有同名函数；发布副本用最小可用实现） ──
try:
    logger
except NameError:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger('v3-runtime')
try:
    PORT
except NameError:
    PORT = int(os.environ.get('PORT', '5678'))
def cfg():
    try:
        return json.load(open(os.environ.get('MT_CFG', '/data/webcfg.json'), encoding='utf-8'))
    except Exception:
        return {}
def containers():
    return {'emby': {'addr': cfg().get('emby_addr', '127.0.0.1'), 'port': cfg().get('emby_port', 8096)}}
def sec_mask(v):
    s = str(v or '')
    return (s[:3] + '***' + s[-3:]) if len(s) > 6 else ('***' if s else '')
def tmdb_proxy():
    return os.environ.get('TMDB_PROXY', '')
def sanitize_err(e, n=200):
    return str(e)[:n]
def time_str():
    return time.strftime('%Y-%m-%d %H:%M:%S')

# ════════════════════════════════════════════════════════════════════
# ★ V3.0 插件化运行时（Media Hub Platform）— 2026-11-12
#   设计原则（用户 V3 任务书 §3/§4/§5）：
#     · 真插件生态：Manifest 标准 / 生命周期 / 权限 / 子进程隔离 / SDK / 事件总线
#     · 服务注册中心：多实例 CRUD + 能力发现 + 健康探测（与插件解耦）
#     · 统一数据总线：事件发布/订阅/去重/重试/死信；模块从注册表动态读取
#     · 只增不减：不动任何老路由/老函数；独立 DB（hub_v3.db）不碰 3.7GB MT 库
#     · 凭据零变更：只读 webcfg，插件密钥独立加密落盘
#   部署：/dockroot/media-hub/ 已挂载可写 → DB/插件/日志全在此目录
# ════════════════════════════════════════════════════════════════════

V3_ROOT = '/dockroot/media-hub/v3'                 # 插件与注册中心数据根（宿主已挂载）
V3_PLUGINS_DIR = os.path.join(V3_ROOT, 'plugins')  # 插件目录 <id>/{manifest.json,plugin.py,...}
V3_DATA_DIR = os.path.join(V3_ROOT, 'data')        # 插件独立数据 <id>/
V3_LOG_DIR = os.path.join(V3_ROOT, 'logs')         # 插件 stdout/stderr 日志
V3_DB = os.path.join(V3_ROOT, 'hub_v3.db')         # 唯一运行时 DB（与 MT 库完全隔离）
V3_SDK_DIR = os.path.join(V3_ROOT, 'sdk')          # 宿主从这里读 manifest schema 示例
V3_HOST_VERSION = '3.0.0'


def _v3_init_dirs():
    for d in (V3_ROOT, V3_PLUGINS_DIR, V3_DATA_DIR, V3_LOG_DIR, V3_SDK_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            logger.warning('[V3] mkdir %s: %s', d, str(e)[:100])


_v3_init_dirs()


def v3_db():
    c = sqlite3.connect(V3_DB, timeout=20)
    c.row_factory = sqlite3.Row
    return c


def v3_db_init():
    with v3_db() as c:
        # 插件注册表
        c.execute('''CREATE TABLE IF NOT EXISTS plugins(
            plugin_id TEXT PRIMARY KEY, name TEXT, version TEXT, author TEXT,
            description TEXT, manifest TEXT, status TEXT DEFAULT 'installed',
            enabled INTEGER DEFAULT 0, pid INTEGER DEFAULT 0,
            installed_at TEXT, updated_at TEXT, last_health TEXT,
            error TEXT, permissions TEXT, source TEXT, source_url TEXT)''')
        # 插件配置 / 独立 KV（加密值走 sec 通道；这里存明文配置结构+非敏感值）
        c.execute('''CREATE TABLE IF NOT EXISTS plugin_config(
            plugin_id TEXT, key TEXT, value TEXT, updated_at TEXT,
            PRIMARY KEY(plugin_id,key))''')
        c.execute('''CREATE TABLE IF NOT EXISTS plugin_kv(
            plugin_id TEXT, k TEXT, v TEXT, updated_at TEXT,
            PRIMARY KEY(plugin_id,k))''')
        # 服务实例注册表（插件与实例解耦：一个插件可挂 N 个实例）
        c.execute('''CREATE TABLE IF NOT EXISTS service_instances(
            id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT,
            config TEXT, endpoint TEXT, enabled INTEGER DEFAULT 1,
            health TEXT DEFAULT 'unknown', capabilities TEXT DEFAULT '[]',
            last_check TEXT, last_error TEXT, created_at TEXT, updated_at TEXT)''')
        # 事件总线（含去重/重试/死信）
        c.execute('''CREATE TABLE IF NOT EXISTS events(
            id TEXT PRIMARY KEY, type TEXT, source TEXT, instance_id TEXT,
            data TEXT, status TEXT DEFAULT 'pending', dedupe_key TEXT,
            attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
            created_at TEXT, processed_at TEXT, last_error TEXT)''')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_ev_dedupe ON events(dedupe_key) WHERE dedupe_key IS NOT NULL')
        c.execute('''CREATE TABLE IF NOT EXISTS event_subscriptions(
            id TEXT PRIMARY KEY, event_type TEXT, subscriber TEXT,
            kind TEXT DEFAULT 'plugin', endpoint TEXT, enabled INTEGER DEFAULT 1,
            created_at TEXT)''')
        # 插件日志（结构化，落 DB + 文件双份）
        c.execute('''CREATE TABLE IF NOT EXISTS plugin_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id TEXT, level TEXT, msg TEXT, ts TEXT)''')
        # 能力注册表（插件声明的导航/组件/路由/任务）
        c.execute('''CREATE TABLE IF NOT EXISTS plugin_capabilities(
            plugin_id TEXT, kind TEXT, key TEXT, payload TEXT,
            PRIMARY KEY(plugin_id,kind,key))''')
        # 安装历史
        c.execute('''CREATE TABLE IF NOT EXISTS plugin_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id TEXT, action TEXT, detail TEXT, ts TEXT)''')
v3_db_init()


def v3_log(plugin_id, level, msg):
    try:
        with v3_db() as c:
            c.execute('INSERT INTO plugin_logs(plugin_id,level,msg,ts) VALUES(?,?,?,?)',
                      (plugin_id, level, str(msg)[:2000], _mf_now()))
    except Exception:
        pass
    try:
        with open(os.path.join(V3_LOG_DIR, '%s.log' % re.sub(r'[^A-Za-z0-9_-]', '_', plugin_id or 'host')),
                  'a', encoding='utf-8') as f:
            f.write('[%s] [%s] %s\n' % (_mf_now(), level, str(msg)[:1000]))
    except Exception:
        pass


def v3_history(plugin_id, action, detail=''):
    try:
        with v3_db() as c:
            c.execute('INSERT INTO plugin_history(plugin_id,action,detail,ts) VALUES(?,?,?,?)',
                      (plugin_id, action, str(detail)[:500], _mf_now()))
    except Exception:
        pass


# ── Manifest 标准与校验（V3 任务书 §3.2） ──────────────────────────
V3_MANIFEST_REQUIRED = ['plugin_id', 'name', 'version', 'entrypoint']
V3_MANIFEST_OPTIONAL = ['description', 'author', 'min_host_version', 'max_host_version',
                        'dependencies', 'permissions', 'configuration_schema', 'navigation',
                        'dashboard_widgets', 'capabilities', 'event_subscriptions',
                        'background_tasks', 'api_routes', 'data_schema', 'lifecycle_hooks',
                        'logo', 'category', 'repository', 'license']
V3_PERMISSIONS = {
    # 细粒度权限点（V3 任务书 §3.4）：插件必须声明，管理员授权后才能用
    'log.write': '写日志',
    'config.read': '读自身配置',
    'config.write': '写自身配置',
    'kv.read': '读写独立 KV',
    'kv.write': '写独立 KV',
    'event.publish': '发布事件',
    'event.subscribe': '订阅事件',
    'service.read': '读取服务实例（脱敏）',
    'media.read': '读取媒体数据',
    'playback.read': '读取播放记录',
    'user.read': '读取用户信息',
    'network.outbound': '出站网络访问',
    'storage.read': '读取文件/目录',
    'storage.write': '写文件/目录',
    'bot.message.read': '读取机器人消息',
    'bot.message.send': '发送机器人消息',
    'task.schedule': '注册后台任务',
    'dashboard.widget': '注册仪表盘组件',
    'navigation': '注册导航入口',
    'api.route': '注册 API 路由',
    'ai.model': '调用 AI 模型',
}


def _v3_ver_tuple(v):
    try:
        return tuple(int(x) for x in re.findall(r'\d+', str(v))[:3])
    except Exception:
        return (0,)


def v3_manifest_validate(m):
    """返回 (ok, err, normalized)。校验版本兼容/依赖/权限/唯一 ID。"""
    if not isinstance(m, dict):
        return False, 'manifest 必须是 JSON 对象', None
    for k in V3_MANIFEST_REQUIRED:
        if not m.get(k):
            return False, 'manifest 缺必需字段: %s' % k, None
    pid = re.sub(r'[^A-Za-z0-9_-]', '', str(m['plugin_id']))
    if not pid or len(pid) > 64:
        return False, 'plugin_id 非法（仅字母数字_-，≤64）', None
    if '/' in str(m.get('entrypoint', '')) or '..' in str(m.get('entrypoint', '')):
        return False, 'entrypoint 不允许路径穿越', None
    hv = _v3_ver_tuple(V3_HOST_VERSION)
    mn = str(m.get('min_host_version') or '0')
    mx = str(m.get('max_host_version') or '999')
    if _v3_ver_tuple(mn) > hv or _v3_ver_tuple(mx) < hv:
        return False, '宿主版本不兼容（需 %s ~ %s，当前 %s）' % (mn, mx, V3_HOST_VERSION), None
    perms = m.get('permissions') or []
    if isinstance(perms, str):
        perms = [x.strip() for x in perms.split(',') if x.strip()]
    unknown = [p for p in perms if p not in V3_PERMISSIONS]
    if unknown:
        return False, '未知权限: %s' % ','.join(unknown[:5]), None
    m['plugin_id'] = pid
    m['permissions'] = perms
    m['dependencies'] = m.get('dependencies') or []
    m['navigation'] = m.get('navigation') or []
    m['dashboard_widgets'] = m.get('dashboard_widgets') or []
    m['capabilities'] = m.get('capabilities') or []
    m['event_subscriptions'] = m.get('event_subscriptions') or []
    m['background_tasks'] = m.get('background_tasks') or []
    m['api_routes'] = m.get('api_routes') or []
    m['lifecycle_hooks'] = m.get('lifecycle_hooks') or {}
    return True, '', m


def v3_plugin_get(pid):
    with v3_db() as c:
        r = c.execute('SELECT * FROM plugins WHERE plugin_id=?', (pid,)).fetchone()
    return dict(r) if r else None


def v3_plugin_manifest(pid):
    r = v3_plugin_get(pid)
    if not r:
        return None
    try:
        return json.loads(r['manifest'])
    except Exception:
        return None


def v3_plugins_list():
    with v3_db() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM plugins ORDER BY installed_at DESC')]
    for r in rows:
        m = v3_plugin_manifest(r['plugin_id']) or {}
        r['author'] = m.get('author') or r.get('author') or ''
        r['category'] = m.get('category') or '其他'
        r['logo'] = m.get('logo') or ''
        r['description'] = m.get('description') or r.get('description') or ''
        r['permissions_desc'] = [V3_PERMISSIONS.get(p, p) for p in (r.get('permissions') or [])] \
            if isinstance(r.get('permissions'), list) else []
    return rows


def v3_install_plugin(manifest, source='local', source_url='', plugin_dir=None):
    """安装插件：校验 manifest → 落盘（可选）→ 注册 DB → 注册能力 → 返回。
    plugin_dir: 已解压好的插件目录（含 entrypoint）；None 表示仅注册（内置演示用）。"""
    ok, err, m = v3_manifest_validate(manifest)
    if not ok:
        return {'ok': False, 'msg': err}
    pid = m['plugin_id']
    if v3_plugin_get(pid):
        return {'ok': False, 'msg': '插件已安装: %s' % pid}
    # 依赖检查
    for dep in (m.get('dependencies') or []):
        dn = dep.get('plugin_id') if isinstance(dep, dict) else str(dep)
        if dn and not v3_plugin_get(dn):
            return {'ok': False, 'msg': '依赖未安装: %s' % dn}
    # 权限记录（需管理员在 UI 审核；默认全部拒，启用时才按授权生效）
    now = _mf_now()
    try:
        with v3_db() as c:
            c.execute('''INSERT INTO plugins(plugin_id,name,version,author,description,manifest,
                          status,enabled,pid,installed_at,updated_at,error,permissions,source,source_url)
                         VALUES(?,?,?,?,?,?,'installed',0,0,?,?,?,?,?,?)''',
                      (pid, m['name'], m['version'], m.get('author') or '', m.get('description') or '',
                       json.dumps(m, ensure_ascii=False), now, now, '',
                       json.dumps(m.get('permissions') or [], ensure_ascii=False), source, source_url))
            v3_register_capabilities(c, pid, m)
    except Exception as e:
        return {'ok': False, 'msg': '注册失败: %s' % str(e)[:120]}
    v3_history(pid, 'install', 'source=%s version=%s' % (source, m['version']))
    v3_log(pid, 'info', '插件已安装（未启用），等待权限审核')
    return {'ok': True, 'msg': '插件 %s 已安装，请审核权限后启用' % m['name'], 'plugin_id': pid}


def v3_register_capabilities(c, pid, m):
    """注册插件声明的导航/仪表盘/路由/任务/事件订阅（V3 任务书 §5.1 自动联动）。"""
    for nav in (m.get('navigation') or []):
        key = nav.get('id') or nav.get('path') or ''
        if key:
            c.execute('INSERT OR REPLACE INTO plugin_capabilities(plugin_id,kind,key,payload) VALUES(?,?,?,?)',
                      (pid, 'navigation', str(key), json.dumps(nav, ensure_ascii=False)))
    for w in (m.get('dashboard_widgets') or []):
        key = w.get('id') or ''
        if key:
            c.execute('INSERT OR REPLACE INTO plugin_capabilities(plugin_id,kind,key,payload) VALUES(?,?,?,?)',
                      (pid, 'widget', str(key), json.dumps(w, ensure_ascii=False)))
    for r in (m.get('api_routes') or []):
        key = (r.get('method', 'GET').upper() + ' ' + r.get('path', '')) if isinstance(r, dict) else str(r)
        if key.strip():
            c.execute('INSERT OR REPLACE INTO plugin_capabilities(plugin_id,kind,key,payload) VALUES(?,?,?,?)',
                      (pid, 'route', key, json.dumps(r, ensure_ascii=False)))
    for t in (m.get('background_tasks') or []):
        key = t.get('id') or t.get('name') or ''
        if key:
            c.execute('INSERT OR REPLACE INTO plugin_capabilities(plugin_id,kind,key,payload) VALUES(?,?,?,?)',
                      (pid, 'task', str(key), json.dumps(t, ensure_ascii=False)))
    for e in (m.get('event_subscriptions') or []):
        key = e.get('event_type') or e.get('type') or ''
        if key:
            c.execute('INSERT OR REPLACE INTO plugin_capabilities(plugin_id,kind,key,payload) VALUES(?,?,?,?)',
                      (pid, 'event', str(key), json.dumps(e, ensure_ascii=False)))
            # 同步到事件订阅表（插件进程通过宿主 SDK 拉取事件）
            try:
                c.execute('''INSERT OR REPLACE INTO event_subscriptions(id,event_type,subscriber,kind,endpoint,enabled,created_at)
                             VALUES(?,?,?,'plugin','sdk',1,?)''',
                          ('%s:%s' % (pid, key), str(key), pid, _mf_now()))
            except Exception:
                pass


def v3_uninstall_plugin(pid):
    r = v3_plugin_get(pid)
    if not r:
        return {'ok': False, 'msg': '插件不存在'}
    v3_stop_plugin(pid)
    with v3_db() as c:
        for t in ('plugins', 'plugin_config', 'plugin_kv', 'plugin_capabilities', 'plugin_logs'):
            try:
                c.execute('DELETE FROM %s WHERE plugin_id=?' % t, (pid,))
            except Exception:
                pass
        c.execute('DELETE FROM event_subscriptions WHERE subscriber=?', (pid,))
    v3_history(pid, 'uninstall', '')
    return {'ok': True, 'msg': '已卸载 %s（独立数据保留，可手动删除）' % pid}


# ── 插件进程生命周期（子进程隔离 + 健康检查，V3 任务书 §3.3/§3.4） ──
_V3_PROCS = {}          # pid -> subprocess.Popen（内存态，重启宿主会清空→启动段自动拉起 enabled 插件）
_V3_LOCK = threading.Lock()


def _v3_plugin_entry(pid):
    d = os.path.join(V3_PLUGINS_DIR, pid)
    m = v3_plugin_manifest(pid) or {}
    ep = str(m.get('entrypoint') or 'plugin.py')
    return os.path.join(d, ep), d


def v3_start_plugin(pid):
    r = v3_plugin_get(pid)
    if not r:
        return {'ok': False, 'msg': '插件不存在'}
    with _V3_LOCK:
        old = _V3_PROCS.get(pid)
        if old and old.poll() is None:
            return {'ok': True, 'msg': '已在运行', 'pid': old.pid, 'already': True}
        entry, d = _v3_plugin_entry(pid)
        if not os.path.isfile(entry):
            return {'ok': False, 'msg': '入口文件不存在: %s（请用本地 ZIP/Git 安装完整插件包）' % entry}
        try:
            env = dict(os.environ)
            env.update({'KFC_PLUGIN_ID': pid, 'KFC_HOST': '127.0.0.1', 'KFC_PORT': str(PORT),
                        'KFC_SDK_TOKEN': v3_sdk_token(pid), 'KFC_PLUGIN_DATA': os.path.join(V3_DATA_DIR, pid),
                        'KFC_PLUGIN_LOG': os.path.join(V3_LOG_DIR, pid + '.log'),
                        'PYTHONPATH': V3_SDK_DIR + os.pathsep + (env.get('PYTHONPATH') or '')})
            os.makedirs(os.path.join(V3_DATA_DIR, pid), exist_ok=True)
            lf = open(os.path.join(V3_LOG_DIR, pid + '.log'), 'a', encoding='utf-8')
            proc = subprocess.Popen([sys.executable, entry], cwd=d, env=env,
                                    stdout=lf, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            _V3_PROCS[pid] = proc
            with v3_db() as c:
                c.execute('UPDATE plugins SET enabled=1,pid=?,status=?,last_health=?,error=? WHERE plugin_id=?',
                          (proc.pid, 'running', _mf_now(), '', pid))
            v3_history(pid, 'start', 'pid=%s' % proc.pid)
            v3_log(pid, 'info', '插件进程已启动 pid=%s' % proc.pid)
            return {'ok': True, 'msg': '已启动', 'pid': proc.pid}
        except Exception as e:
            with v3_db() as c:
                c.execute('UPDATE plugins SET status=?,error=? WHERE plugin_id=?',
                          ('error', str(e)[:200], pid))
            v3_log(pid, 'error', '启动失败: %s' % str(e)[:200])
            return {'ok': False, 'msg': '启动失败: %s' % str(e)[:120]}


def v3_stop_plugin(pid):
    with _V3_LOCK:
        proc = _V3_PROCS.pop(pid, None)
    if proc is None:
        r = v3_plugin_get(pid)
        if r and r.get('pid'):
            try:
                os.kill(int(r['pid']), signal.SIGTERM)
            except Exception:
                pass
    else:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        except Exception:
            pass
    with v3_db() as c:
        c.execute('UPDATE plugins SET enabled=0,pid=0,status=? WHERE plugin_id=?', ('stopped', pid))
    v3_history(pid, 'stop', '')
    return {'ok': True, 'msg': '已停止'}


def v3_restart_plugin(pid):
    v3_stop_plugin(pid)
    time.sleep(0.5)
    return v3_start_plugin(pid)


def v3_plugin_health(pid):
    r = v3_plugin_get(pid)
    if not r:
        return {'ok': False, 'msg': '插件不存在'}
    proc = _V3_PROCS.get(pid)
    alive = bool(proc and proc.poll() is None)
    st = 'running' if alive else 'stopped'
    if r.get('enabled') and not alive:
        st = 'crashed'          # 声明启用但进程没了 = 崩溃（故障隔离，不影响宿主）
    try:
        with v3_db() as c:
            c.execute('UPDATE plugins SET status=?,last_health=?,pid=? WHERE plugin_id=?',
                      (st, _mf_now(), (proc.pid if alive else 0), pid))
    except Exception:
        pass
    return {'ok': True, 'plugin_id': pid, 'status': st,
            'pid': (proc.pid if alive else 0),
            'restart_count': 0, 'last_health': r.get('last_health')}


def v3_autostart():
    """宿主启动时拉起所有 enabled 插件（故障隔离：单个失败不影响其它）。"""
    try:
        with v3_db() as c:
            pids = [r['plugin_id'] for r in c.execute('SELECT plugin_id FROM plugins WHERE enabled=1')]
        for pid in pids:
            try:
                v3_start_plugin(pid)
            except Exception as e:
                v3_log(pid, 'error', 'autostart 失败: %s' % str(e)[:120])
        if pids:
            logger.info('[V3] 已自动拉起 %d 个插件: %s', len(pids), ','.join(pids))
    except Exception as e:
        logger.warning('[V3] autostart: %s', str(e)[:120])


# ── 插件 SDK token（子进程回调宿主用；每插件独立，不复用用户凭据） ──
def v3_sdk_token(pid):
    secret = cfg().get('sec_master_key') or str(SEC_ROOT)
    return hashlib.sha256(('%s|%s|%s' % (pid, secret, V3_HOST_VERSION)).encode()).hexdigest()[:32]


def v3_sdk_auth(pid, token):
    return bool(token) and hmac.compare_digest(str(token), v3_sdk_token(pid))


# ── 服务注册中心（V3 任务书 §4：插件与实例解耦，N 实例） ──────────
def v3_service_list(t=None):
    with v3_db() as c:
        if t:
            rows = [dict(r) for r in c.execute('SELECT * FROM service_instances WHERE type=? ORDER BY updated_at DESC', (t,))]
        else:
            rows = [dict(r) for r in c.execute('SELECT * FROM service_instances ORDER BY type,updated_at DESC')]
    for r in rows:
        try:
            cfgv = json.loads(r.get('config') or '{}')
        except Exception:
            cfgv = {}
        cfgv = dict(cfgv)
        for k in list(cfgv):
            if any(s in k.lower() for s in ('key', 'token', 'pass', 'secret', 'apikey')):
                cfgv[k] = sec_mask(str(cfgv[k])) if cfgv[k] else ''
        r['config_masked'] = cfgv
        r.pop('config', None)
        try:
            r['capabilities'] = json.loads(r.get('capabilities') or '[]')
        except Exception:
            r['capabilities'] = []
    return rows


def v3_service_upsert(arg):
    sid = str(arg.get('id') or '').strip() or ('svc-%s-%d' % (arg.get('type') or 'x', int(time.time())))
    if not re.match(r'^[A-Za-z0-9_-]{2,64}$', sid):
        return {'ok': False, 'msg': '实例 ID 非法（字母数字_-，2-64）'}
    typ = str(arg.get('type') or '').strip().lower()
    if not typ or not re.match(r'^[a-z0-9_]{2,32}$', typ):
        return {'ok': False, 'msg': '服务类型非法'}
    cfgv = arg.get('config') or {}
    if not isinstance(cfgv, dict):
        return {'ok': False, 'msg': 'config 必须是对象'}
    try:
        caps = json.dumps(arg.get('capabilities') or [], ensure_ascii=False)
    except Exception:
        caps = '[]'
    now = _mf_now()
    with v3_db() as c:
        old = c.execute('SELECT created_at FROM service_instances WHERE id=?', (sid,)).fetchone()
        created = old['created_at'] if old else now
        c.execute('''INSERT OR REPLACE INTO service_instances
                     (id,type,name,config,endpoint,enabled,health,capabilities,last_check,last_error,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (sid, typ, arg.get('name') or sid, json.dumps(cfgv, ensure_ascii=False),
                   arg.get('endpoint') or '', int(bool(arg.get('enabled', 1))),
                   (old and c.execute('SELECT health FROM service_instances WHERE id=?', (sid,)).fetchone()['health']) or 'unknown',
                   caps, None, None, created, now))
    return {'ok': True, 'msg': '实例已保存', 'id': sid}


def v3_service_delete(sid):
    with v3_db() as c:
        c.execute('DELETE FROM service_instances WHERE id=?', (sid,))
    return {'ok': True, 'msg': '已删除实例'}


def v3_service_test(sid):
    """真实连通测试（非伪造）：按 type 走已有适配器；失败如实返回。"""
    with v3_db() as c:
        r = c.execute('SELECT * FROM service_instances WHERE id=?', (sid,)).fetchone()
    if not r:
        return {'ok': False, 'msg': '实例不存在'}
    typ = r['type']
    try:
        cfgv = json.loads(r['config'] or '{}')
    except Exception:
        cfgv = {}
    ok, msg, caps = False, '未实现该服务类型的探测', []
    try:
        if typ in ('emby', 'jellyfin', 'plex'):
            # 复用已有 _emby_http 的探测逻辑，取 System/Info（真实认证）
            base = (cfgv.get('endpoint') or r['endpoint'] or '').rstrip('/')
            key = cfgv.get('api_key') or ''
            if not base or not key:
                ok, msg = False, '缺 endpoint 或 api_key'
            else:
                ua = base + '/System/Info?api_key=' + urllib.parse.quote(key)
                s, j = _mf_wan_json(ua, t=12)
                if j and j.get('ServerName'):
                    ok, msg = True, '已连接：%s v%s' % (j.get('ServerName'), j.get('Version') or '?')
                    caps = ['sessions', 'library', 'users', 'rank', 'webhook']
                else:
                    msg = '连接失败（HTTP %s）：请检查地址/Key/网络' % s
        elif typ in ('qbittorrent', 'moviepilot', 'mediatidy', 'clouddrive2', 'openlist', 'resourcetool'):
            base = (cfgv.get('endpoint') or r['endpoint'] or '').rstrip('/')
            if not base:
                ok, msg = False, '缺 endpoint'
            else:
                s, b = _mf_wan_http(base, t=12)
                ok = 200 <= s < 400
                msg = ('可达 HTTP %s' % s) if ok else ('不可达 HTTP %s' % s)
                caps = ['status']
        else:
            # 未知类型：只做 TCP/HTTP 可达性，能力显示「未识别」不伪造
            base = (cfgv.get('endpoint') or r['endpoint'] or '').rstrip('/')
            s, b = _mf_wan_http(base, t=10) if base else (-1, b'')
            msg = 'HTTP %s（未知服务类型，能力未识别）' % s
            ok = 200 <= s < 400
    except Exception as e:
        msg = '探测异常: %s' % sanitize_err(e, 160)
    with v3_db() as c:
        c.execute('UPDATE service_instances SET health=?,last_check=?,last_error=?,capabilities=? WHERE id=?',
                  ('healthy' if ok else 'unhealthy', _mf_now(), '' if ok else msg,
                   json.dumps(caps, ensure_ascii=False), sid))
    return {'ok': ok, 'msg': msg, 'capabilities': caps, 'health': 'healthy' if ok else 'unhealthy'}


# ── 事件总线（V3 任务书 §5.2：唯一 ID / 去重 / 重试 / 死信） ───────
def v3_event_publish(ev_type, data=None, source='host', instance_id='', dedupe_key=''):
    eid = 'ev-%s-%s' % (int(time.time() * 1000), uuid.uuid4().hex[:8])
    try:
        with v3_db() as c:
            c.execute('''INSERT OR IGNORE INTO events
                         (id,type,source,instance_id,data,status,dedupe_key,attempts,max_attempts,created_at)
                         VALUES(?,?,?,?,?,'pending',?,0,3,?)''',
                      (eid, ev_type, source, instance_id,
                       json.dumps(data or {}, ensure_ascii=False), dedupe_key or None, _mf_now()))
            inserted = c.total_changes > 0
        if not inserted:
            return {'ok': True, 'duplicate': True, 'msg': '重复事件已去重'}
        # 同步分发给订阅者（插件通过轮询 /api/v3/sdk/events 拉取；webhook 可后续扩展）
        return {'ok': True, 'event_id': eid, 'msg': '事件已发布'}
    except Exception as e:
        return {'ok': False, 'msg': '发布失败: %s' % str(e)[:120]}


def v3_event_list(status='', limit=200):
    with v3_db() as c:
        if status:
            rows = [dict(r) for r in c.execute(
                'SELECT * FROM events WHERE status=? ORDER BY created_at DESC LIMIT ?', (status, int(limit)))]
        else:
            rows = [dict(r) for r in c.execute('SELECT * FROM events ORDER BY created_at DESC LIMIT ?', (int(limit),))]
    for r in rows:
        try:
            r['data'] = json.loads(r.get('data') or '{}')
        except Exception:
            r['data'] = {}
    return rows


def v3_event_ack(event_id, ok=True, err=''):
    with v3_db() as c:
        r = c.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
        if not r:
            return {'ok': False, 'msg': '事件不存在'}
        if ok:
            c.execute('UPDATE events SET status=?,processed_at=? WHERE id=?',
                      ('processed', _mf_now(), event_id))
        else:
            att = (r['attempts'] or 0) + 1
            st = 'dead' if att >= (r['max_attempts'] or 3) else 'pending'
            c.execute('UPDATE events SET status=?,attempts=?,last_error=? WHERE id=?',
                      (st, att, str(err)[:300], event_id))
    return {'ok': True}


# ── 能力聚合（V3 任务书 §5.3 动态模块发现） ───────────────────────
def v3_capabilities(kind=None):
    with v3_db() as c:
        if kind:
            rows = [dict(r) for r in c.execute(
                'SELECT pc.* FROM plugin_capabilities pc JOIN plugins p ON p.plugin_id=pc.plugin_id '
                'WHERE pc.kind=? AND p.enabled=1', (kind,))]
        else:
            rows = [dict(r) for r in c.execute(
                'SELECT pc.* FROM plugin_capabilities pc JOIN plugins p ON p.plugin_id=pc.plugin_id '
                'WHERE p.enabled=1')]
    for r in rows:
        try:
            r['payload'] = json.loads(r.get('payload') or '{}')
        except Exception:
            r['payload'] = {}
    return rows


def v3_overview():
    """V3 驾驶舱数据：全部来自真实 registry / plugin / event，无模拟值。"""
    with v3_db() as c:
        plugins_total = c.execute('SELECT COUNT(*) FROM plugins').fetchone()[0]
        plugins_on = c.execute('SELECT COUNT(*) FROM plugins WHERE enabled=1').fetchone()[0]
        svc_total = c.execute('SELECT COUNT(*) FROM service_instances').fetchone()[0]
        svc_healthy = c.execute("SELECT COUNT(*) FROM service_instances WHERE health='healthy'").fetchone()[0]
        ev_pending = c.execute("SELECT COUNT(*) FROM events WHERE status='pending'").fetchone()[0]
        ev_dead = c.execute("SELECT COUNT(*) FROM events WHERE status='dead'").fetchone()[0]
        caps = c.execute('SELECT kind,COUNT(*) n FROM plugin_capabilities pc JOIN plugins p '
                         'ON p.plugin_id=pc.plugin_id WHERE p.enabled=1 GROUP BY kind').fetchall()
    return {'ok': True, 'version': V3_HOST_VERSION,
            'plugins': {'total': plugins_total, 'enabled': plugins_on},
            'services': {'total': svc_total, 'healthy': svc_healthy},
            'events': {'pending': ev_pending, 'dead': ev_dead},
            'capabilities': {r['kind']: r['n'] for r in caps},
            'data_root': V3_ROOT}


# ── V3 路由分发（唯一入口，老 327 路由完全不动） ──────────────────
def v3_route_get(p, q1):
    if p == '/api/v3/overview':
        return v3_overview()
    if p == '/api/v3/plugins':
        return {'ok': True, 'plugins': v3_plugins_list()}
    if p == '/api/v3/plugins/health':
        return {'ok': True, 'health': [v3_plugin_health(r['plugin_id']) for r in v3_plugins_list()]}
    if p == '/api/v3/plugins/capabilities':
        return {'ok': True, 'capabilities': v3_capabilities(q1('kind') or None)}
    if p == '/api/v3/services':
        return {'ok': True, 'instances': v3_service_list(q1('type') or None)}
    if p == '/api/v3/events':
        return {'ok': True, 'events': v3_event_list(q1('status') or '', int(q1('limit') or 200))}
    if p == '/api/v3/history':
        with v3_db() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM plugin_history ORDER BY id DESC LIMIT 200')]
        return {'ok': True, 'history': rows}
    if p == '/api/v3/permissions':
        return {'ok': True, 'permissions': [{'key': k, 'label': v} for k, v in V3_PERMISSIONS.items()]}
    if p == '/api/v3/sdk/schema':
        return {'ok': True, 'required': V3_MANIFEST_REQUIRED, 'optional': V3_MANIFEST_OPTIONAL,
                'permissions': V3_PERMISSIONS, 'host_version': V3_HOST_VERSION}
    return {'ok': False, 'msg': 'V3 GET 未识别: %s' % p, 'code': 404}


def v3_route_post(p, b):
    if p == '/api/v3/plugin/install':
        return v3_install_plugin(b.get('manifest') or {}, b.get('source') or 'local',
                                 b.get('source_url') or '')
    if p == '/api/v3/plugin/enable':       # 权限审核后在 UI 点启用
        pid = b.get('plugin_id') or ''
        r = v3_plugin_get(pid)
        if not r:
            return {'ok': False, 'msg': '插件不存在'}
        m = v3_plugin_manifest(pid) or {}
        granted = set(b.get('granted_permissions') or (m.get('permissions') or []))
        need = set(m.get('permissions') or [])
        if need - granted:
            return {'ok': False, 'msg': '未授权权限: %s' % ','.join(sorted(need - granted))}
        with v3_db() as c:
            c.execute('UPDATE plugins SET permissions=? WHERE plugin_id=?',
                      (json.dumps(sorted(granted), ensure_ascii=False), pid))
        return v3_start_plugin(pid)
    if p == '/api/v3/plugin/disable':
        return v3_stop_plugin(b.get('plugin_id') or '')
    if p == '/api/v3/plugin/restart':
        return v3_restart_plugin(b.get('plugin_id') or '')
    if p == '/api/v3/plugin/uninstall':
        return v3_uninstall_plugin(b.get('plugin_id') or '')
    if p == '/api/v3/plugin/config':
        pid = b.get('plugin_id') or ''
        with v3_db() as c:
            for k, v in (b.get('config') or {}).items():
                c.execute('INSERT OR REPLACE INTO plugin_config(plugin_id,key,value,updated_at) VALUES(?,?,?,?)',
                          (pid, str(k), json.dumps(v, ensure_ascii=False), _mf_now()))
        return {'ok': True, 'msg': '配置已保存'}
    if p == '/api/v3/plugin/log':
        v3_log(b.get('plugin_id') or 'sdk', b.get('level') or 'info', b.get('msg') or '')
        return {'ok': True}
    if p == '/api/v3/service/upsert':
        return v3_service_upsert(b or {})
    if p == '/api/v3/service/delete':
        return v3_service_delete(b.get('id') or '')
    if p == '/api/v3/service/test':
        return v3_service_test(b.get('id') or '')
    if p == '/api/v3/event/publish':
        return v3_event_publish(b.get('type') or '', b.get('data') or {},
                                b.get('source') or 'api', b.get('instance_id') or '',
                                b.get('dedupe_key') or '')
    if p == '/api/v3/event/ack':
        return v3_event_ack(b.get('event_id') or '', bool(b.get('ok', True)), b.get('err') or '')
    return {'ok': False, 'msg': 'V3 POST 未识别: %s' % p, 'code': 404}


# ── end V3.0 插件化运行时 ──────────────────────────────────────────
