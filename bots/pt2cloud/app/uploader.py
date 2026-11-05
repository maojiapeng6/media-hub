"""本地下载文件 -> 115 云盘（经由 CD2 挂载点直写）"""
import logging
import os
import shutil

import config

log = logging.getLogger("pt2cloud")


def is_keep(rel_path):
    """判断相对路径是否值得上传"""
    low = rel_path.lower()
    if low.endswith(config.EXCLUDE_SUFFIX):
        return False
    base = os.path.basename(low)
    ext = os.path.splitext(base)[1]
    if ext not in config.KEEP_EXT:
        return False
    for hint in config.EXCLUDE_NAME_HINTS:
        if hint in low:
            return False
    return True


def scan_files(root):
    """扫描种子内容，返回 [(绝对路径, 相对路径, 大小)]"""
    out = []
    if os.path.isfile(root):
        if is_keep(os.path.basename(root)):
            out.append((root, os.path.basename(root), os.path.getsize(root)))
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in config.EXCLUDE_NAME_HINTS]
        for fn in filenames:
            abs_p = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_p, root)
            if not is_keep(rel):
                continue
            try:
                out.append((abs_p, rel, os.path.getsize(abs_p)))
            except OSError:
                continue
    return out


def cloud_target(origin, site, category, torrent_name):
    """计算云端落点：/CloudNAS/115open/待整理/PT/{来源}/{站点}/{分类}/{种子名}

    ★ 2026-10-14 目录结构最终拍板（用户要求）：
          待整理 → PT →（订阅|求片）→ 站点 → 分类 → 种子名
       与本地 qB 下载盘 /downloads/qBittorrent/{来源}/{站点}/{分类} 完全一致，
       本地/云端一一对应，不会出现「本地叫求片、云端叫订阅」的错位。

    层级含义：
      来源  求片/订阅（最稳定，wishlist 决定）
      站点  织梦/馒头/老师…（MP 站点表中文名）
      分类  电影/电视剧/动漫/纪录片/综艺/演唱会/其他（MT 在站点方案内按规则细分）
    """
    cat = category or "其他"
    safe = "".join(ch for ch in (torrent_name or "未命名")
                   if ch not in '/\\:*?"<>|').strip() or "未命名"
    # CAT_FIRST=1 是 2026-10-13 短暂试过、实测不好用的反向顺序，默认关闭。
    # 默认顺序（CAT_FIRST=0）：待整理/PT/{来源}/{站点}/{分类}/{种子名}
    parts = (origin, site, cat, safe) if not config.CAT_FIRST else \
            (cat, origin, site, safe)
    rel = os.path.join(config.DEST_BASE, *parts)
    return os.path.join(config.CLOUD_ROOT, rel), rel


def mt_path(cloud_rel):
    """把挂载点相对路径转成 MT 视角的云端路径"""
    return config.MT_CLOUD_PREFIX + "/" + cloud_rel.replace("\\", "/").lstrip("/")


def copy_to_cloud(files, src_root, dst_dir):
    """把文件按目录结构复制到云端挂载点；返回 (成功数, 已复制字节)"""
    os.makedirs(dst_dir, exist_ok=True)
    done, moved = 0, 0
    for abs_p, rel, size in files:
        dst = os.path.join(dst_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst) and os.path.getsize(dst) == size:
            done += 1
            moved += size
            continue
        if config.DRY_RUN:
            done += 1
            moved += size
            continue
        try:
            shutil.copyfile(abs_p, dst)
            # CD2 走 writeback 缓存，fsync 只对本地缓存生效；
            # 真正的落盘由后台上传完成，靠 mt.walk() 轮询校验
            done += 1
            moved += size
        except Exception as e:
            log.warning("复制失败 %s -> %s: %s", abs_p, dst, e)
    return done, moved
