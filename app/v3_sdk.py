# -*- coding: utf-8 -*-
"""
KFC Media Hub Plugin SDK v3.0 —— 插件开发者唯一需要 import 的模块。
插件通过环境变量 KFC_HOST / KFC_PORT / KFC_PLUGIN_ID / KFC_SDK_TOKEN 与宿主通信。
所有调用走宿主本地 HTTP API（带 token 鉴权），插件不接触宿主进程内存/凭据。

用法：
    from kfc_sdk import SDK
    sdk = SDK()
    sdk.log('hello')
    cfg = sdk.get_config()
    sdk.emit('media.playback.started', {'item': 'xxx'})
    sdk.kv_set('counter', 1)
    for ev in sdk.poll_events(): ...
"""
import os
import json
import time
import urllib.request
import urllib.parse


class SDK:
    def __init__(self, host=None, port=None, plugin_id=None, token=None, timeout=10):
        self.host = host or os.environ.get('KFC_HOST', '127.0.0.1')
        self.port = int(port or os.environ.get('KFC_PORT', '5678'))
        self.plugin_id = plugin_id or os.environ.get('KFC_PLUGIN_ID', '')
        self.token = token or os.environ.get('KFC_SDK_TOKEN', '')
        self.data_dir = os.environ.get('KFC_PLUGIN_DATA', '.')
        self.timeout = timeout
        self.base = 'http://%s:%d' % (self.host, self.port)

    def _call(self, path, method='GET', body=None):
        url = self.base + path
        if method == 'GET':
            sep = '&' if '?' in url else '?'
            url += sep + 'plugin_id=' + urllib.parse.quote(self.plugin_id)
            url += '&token=' + urllib.parse.quote(self.token)
        data = json.dumps(body or {}, ensure_ascii=False).encode('utf-8') if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            'Content-Type': 'application/json',
            'X-KFC-Plugin': self.plugin_id,
            'X-KFC-Token': self.token,
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            return {'ok': False, 'msg': 'SDK 调用失败: %s' % str(e)[:120]}

    # ── 日志 ──
    def log(self, msg, level='info'):
        return self._call('/api/v3/plugin/log', 'POST',
                          {'plugin_id': self.plugin_id, 'level': level, 'msg': str(msg)})

    # ── 配置管理 ──
    def get_config(self, defaults=None):
        r = self._call('/api/v3/sdk/config?plugin_id=' + urllib.parse.quote(self.plugin_id))
        d = dict(defaults or {})
        if r.get('ok'):
            d.update(r.get('config') or {})
        return d

    def set_config(self, cfg):
        return self._call('/api/v3/plugin/config', 'POST',
                          {'plugin_id': self.plugin_id, 'config': cfg})

    # ── 插件独立 KV ──
    def kv_get(self, k, default=None):
        r = self._call('/api/v3/sdk/kv?plugin_id=%s&k=%s' %
                       (urllib.parse.quote(self.plugin_id), urllib.parse.quote(str(k))))
        if not r.get('ok'):
            return default
        v = r.get('value')
        return default if v is None else v

    def kv_set(self, k, v):
        return self._call('/api/v3/sdk/kv', 'POST',
                          {'plugin_id': self.plugin_id, 'k': str(k), 'v': v})

    # ── 事件 ──
    def emit(self, event_type, data=None, dedupe_key=''):
        """发布事件（宿主去重/重试/死信）。"""
        return self._call('/api/v3/event/publish', 'POST', {
            'type': event_type, 'data': data or {}, 'source': 'plugin:' + self.plugin_id,
            'dedupe_key': dedupe_key})

    def poll_events(self, limit=20):
        """拉取待处理事件（处理完必须 ack）。"""
        r = self._call('/api/v3/sdk/events?plugin_id=%s&limit=%d' %
                       (urllib.parse.quote(self.plugin_id), int(limit)))
        return r.get('events', []) if r.get('ok') else []

    def ack_event(self, event_id, ok=True, err=''):
        return self._call('/api/v3/event/ack', 'POST',
                          {'event_id': event_id, 'ok': ok, 'err': err})

    # ── 服务实例读取（脱敏） ──
    def services(self, svc_type=None):
        q = ('?type=' + urllib.parse.quote(svc_type)) if svc_type else ''
        r = self._call('/api/v3/services' + q)
        return r.get('instances', []) if r.get('ok') else []

    # ── 文件名安全写入插件数据目录 ──
    def data_path(self, name):
        return os.path.join(self.data_dir, os.path.basename(str(name)))


__all__ = ['SDK']
