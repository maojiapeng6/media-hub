#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
授权码签发器（厂商侧工具，G5）
=================================
与面板 license_gen/license_sign 完全同构的离线发码工具。
只有持有 KFC_VENDOR_SECRET 的人能签出可在部署实例上通过校验的授权码。

用法：
  export KFC_VENDOR_SECRET=<厂商密钥>        # 或 --secret-file .vendor-secret.txt
  python tools/license_issuer.py issue --licensee "张三" --plan pro --days 365
  python tools/license_issuer.py list

码结构（KFC-XXXX-XXXX-XXXX-XXXX，去连字符 16 字符 Base32）：
  [0:2]   功能位图(6bit)+max_nodes(4bit)   [2:6] 签发小时   [6:10] 到期小时   [10:16] HMAC 签名
"""
import argparse, hashlib, hmac, json, os, sys, time

B32 = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'          # Crockford：去 0/O/1/I/L
FEATURES = ['core', 'bot', 'pt', 'game', 'multibot', 'white_label']
PLANS = {
    'trial': {'name': '试用', 'days': 30,     'features': ['core', 'bot', 'pt', 'game']},
    'pro':   {'name': '专业', 'days': 365,    'features': ['core', 'bot', 'pt', 'game', 'multibot']},
    'biz':   {'name': '商用', 'days': 3650,   'features': FEATURES},
    'life':  {'name': '永久', 'days': 36500,  'features': FEATURES},
}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(REPO, '.license-ledger.json')


def b32(n, width=0):
    n = int(n) & 0xFFFFFFFF
    out = ''
    while n:
        out += B32[n & 31]
        n >>= 5
    return out[::-1].rjust(width, 'A')


def load_secret(explicit=None):
    s = (explicit or os.environ.get('KFC_VENDOR_SECRET') or '').strip()
    if not s:
        f = os.path.join(REPO, '.vendor-secret.txt')
        if os.path.exists(f):
            s = open(f, encoding='utf-8').read().strip()
    if not s:
        sys.exit('✘ 缺少厂商密钥：设 KFC_VENDOR_SECRET 环境变量，或把密钥写进 %s'
                 % os.path.join(REPO, '.vendor-secret.txt'))
    return s


def sign(payload, secret):
    # ★ 与面板 license_sign 同步的安全修复：取 HMAC 低 30bit 编 6 字符
    #（旧实现 b32(n,13)[:6] 因 32bit 截断+左侧补A，签名恒为 AAAAAA，可被伪造）
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return b32(int.from_bytes(mac[:8], 'big') & 0x3FFFFFFF, 6)


def gen_code(secret, features, max_nodes, issued, expire):
    fb = 0
    for f in FEATURES:
        fb = (fb << 1) | (1 if f in features else 0)
    packed = (fb << 4) | (int(max_nodes) & 15)
    body = b32(packed, 2) + b32(issued // 3600, 4) + b32(expire // 3600, 4)
    sig = sign(body, secret)
    return 'KFC-' + '-'.join([body[0:4], body[4:8], body[8:10] + sig[0:2], sig[2:6]])


def cmd_issue(a):
    secret = load_secret(a.secret)
    plan = a.plan if a.plan in PLANS else 'pro'
    pl = PLANS[plan]
    feats = ([x.strip() for x in a.features.split(',') if x.strip()]
             if a.features else list(pl['features']))
    bad = [f for f in feats if f not in FEATURES]
    if bad:
        sys.exit('✘ 未知功能位: %s（合法: %s）' % (','.join(bad), ','.join(FEATURES)))
    days = a.days if a.days is not None else pl['days']
    issued = int(time.time())
    expire = issued + int(days) * 86400
    code = gen_code(secret, feats, a.max_nodes, issued, expire)
    rec = {'code': code, 'licensee': a.licensee or '', 'plan': plan,
           'features': feats, 'max_nodes': a.max_nodes, 'days': days,
           'fingerprint': a.fingerprint or '', 'note': a.note or '',
           'issued_at': issued, 'expire_at': expire}
    led = []
    if os.path.exists(LEDGER):
        try:
            led = json.load(open(LEDGER, encoding='utf-8'))
        except Exception:
            led = []
    led.append(rec)
    json.dump(led, open(LEDGER, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print('✅ 授权码已签发（已记入本地台账 %s）' % LEDGER)
    print('   授权码   : %s' % code)
    print('   授权对象 : %s' % (a.licensee or '—'))
    print('   套餐/时长: %s / %d 天（至 %s）' % (pl['name'], days, time.strftime('%F', time.localtime(expire))))
    print('   功能位   : %s' % ','.join(feats))
    print('   节点上限 : %d' % a.max_nodes)
    if a.fingerprint:
        print('   设备指纹 : %s' % a.fingerprint)


def cmd_list(_a):
    if not os.path.exists(LEDGER):
        print('（台账为空）')
        return
    led = json.load(open(LEDGER, encoding='utf-8'))
    now = int(time.time())
    print('%-24s %-12s %-6s %-28s %-10s' % ('授权码', '对象', '套餐', '到期', '状态'))
    for r in led:
        st = '有效' if r['expire_at'] > now else '过期'
        print('%-24s %-12s %-6s %-28s %-10s' % (
            r['code'], r.get('licensee') or '—', r['plan'],
            time.strftime('%F', time.localtime(r['expire_at'])), st))


def main():
    ap = argparse.ArgumentParser(description='KFC 授权码签发器（厂商侧）')
    ap.add_argument('--secret', help='厂商密钥（优先级高于环境变量/密钥文件）')
    sub = ap.add_subparsers(dest='cmd', required=True)
    pi = sub.add_parser('issue', help='签发授权码')
    pi.add_argument('--licensee', default='', help='授权对象（名字/备注）')
    pi.add_argument('--plan', default='pro', choices=list(PLANS), help='套餐')
    pi.add_argument('--days', type=int, default=None, help='天数（缺省用套餐默认）')
    pi.add_argument('--features', default='', help='功能位逗号分隔（缺省用套餐默认）')
    pi.add_argument('--max-nodes', type=int, default=1, help='节点数上限（默认 1）')
    pi.add_argument('--fingerprint', default='', help='绑定设备指纹（可空=通用码）')
    pi.add_argument('--note', default='', help='备注')
    sub.add_parser('list', help='查看已发码台账')
    a = ap.parse_args()
    a.secret = a.secret  # noqa
    if a.cmd == 'issue':
        cmd_issue(a)
    else:
        cmd_list(a)


if __name__ == '__main__':
    main()
