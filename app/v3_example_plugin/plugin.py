# -*- coding: utf-8 -*-
"""V3 内置示例插件：Heartbeat —— 验证插件运行时全链路（真实运行，非原型）。
演示能力：日志 / 配置读取 / 独立 KV 计数 / 事件发布 / 事件订阅轮询 / 服务实例读取。
不访问任何真实业务数据，不改变系统状态，只做自证。"""
import os
import sys
import time
import json

sys.path.insert(0, os.environ.get('PYTHONPATH', ''))
try:
    from kfc_sdk import SDK
except ImportError:
    # 宿主子进程 PYTHONPATH 已注入 sdk 目录；若缺失则直接退出（故障隔离）
    print('[FATAL] kfc_sdk 未找到，请检查 KFC_SDK_DIR', flush=True)
    sys.exit(2)

sdk = SDK()
TICK = int(sdk.get_config({'interval': 30}).get('interval') or 30)


def main():
    sdk.log('Heartbeat 插件启动（进程隔离运行）', 'info')
    n = 0
    while True:
        n = int(sdk.kv_get('heartbeat_count', 0)) + 1
        sdk.kv_set('heartbeat_count', n)
        sdk.log('心跳 #%d · %s' % (n, time.strftime('%H:%M:%S')), 'info')
        # 演示事件发布（带去重键，宿主自动去重）
        if n % 5 == 1:
            sdk.emit('plugin.heartbeat', {'count': n, 'plugin': sdk.plugin_id},
                     dedupe_key='hb-%s-%d' % (sdk.plugin_id, n // 5))
        # 演示事件订阅消费
        for ev in sdk.poll_events(limit=5):
            if ev.get('type', '').startswith('plugin.'):
                sdk.log('收到事件 %s: %s' % (ev.get('type'), str(ev.get('data'))[:60]), 'info')
            sdk.ack_event(ev.get('id'), True)
        time.sleep(max(5, TICK))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sdk.log('Heartbeat 收到退出信号', 'info')
    except Exception as e:
        sdk.log('插件异常: %s' % str(e)[:200], 'error')
        sys.exit(1)
