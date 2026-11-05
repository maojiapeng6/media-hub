"""数据层：kfcgame 独立库 + 跨库读写 EmbyBoss 的 V积分"""
from __future__ import annotations

import datetime as dt
import re
import threading
from typing import Any

import pymysql
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from . import settings as S

_engine: Engine | None = None
_lock = threading.RLock()


def _dsn(db: str) -> str:
    return (f"mysql+pymysql://{S.DB['user']}:{S.DB['password']}"
            f"@{S.DB['host']}:{S.DB['port']}/{db}?charset=utf8mb4")


def init() -> Engine:
    """建库 + 建表，返回引擎"""
    global _engine
    if _engine is not None:
        return _engine

    # 1) 确保游戏库存在（用 pymysql 直连，因为 SQLAlchemy 不能在连接时建库）
    raw = pymysql.connect(
        host=S.DB["host"], port=int(S.DB["port"]), user=S.DB["user"],
        password=S.DB["password"], charset="utf8mb4", autocommit=True,
    )
    try:
        with raw.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{S.GAME_DB}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        logger.info(f"游戏库 {S.GAME_DB} 就绪")
    finally:
        raw.close()

    _engine = create_engine(_dsn(S.GAME_DB), pool_pre_ping=True, pool_recycle=1800,
                            pool_size=5, max_overflow=10, future=True)
    _create_tables(_engine)
    return _engine


def _create_tables(eng: Engine) -> None:
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS kfc_cultivator (
            tg              BIGINT       NOT NULL PRIMARY KEY,
            daohao          VARCHAR(32)  NOT NULL,
            sect            VARCHAR(16)  NOT NULL,
            realm           TINYINT      NOT NULL DEFAULT 1,
            cultivation     INT          NOT NULL DEFAULT 0,
            sign_streak     INT          NOT NULL DEFAULT 0,
            last_sign       DATE         NULL,
            last_train      DATETIME     NULL,
            last_duel       DATETIME     NULL,
            sign_buff       INT          NOT NULL DEFAULT 0,
            duel_buff       TINYINT      NOT NULL DEFAULT 0,
            perm_sign_bonus INT          NOT NULL DEFAULT 0,
            shield_until    DATETIME     NULL,
            rob_win         INT          NOT NULL DEFAULT 0,
            duel_win        INT          NOT NULL DEFAULT 0,
            visit_set       VARCHAR(255) NULL,
            created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_realm (realm),
            INDEX idx_cult (cultivation)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS kfc_daily_task (
            tg        BIGINT      NOT NULL,
            day       DATE        NOT NULL,
            task_key  VARCHAR(24) NOT NULL,
            cnt       INT         NOT NULL DEFAULT 0,
            done      TINYINT     NOT NULL DEFAULT 0,
            PRIMARY KEY (tg, day, task_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS kfc_achievement (
            tg          BIGINT      NOT NULL,
            ach_key     VARCHAR(32) NOT NULL,
            unlocked_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tg, ach_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS kfc_action_log (
            id        BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
            kind      VARCHAR(16) NOT NULL,
            src_tg    BIGINT      NOT NULL,
            dst_tg    BIGINT      NULL,
            amount    INT         NOT NULL DEFAULT 0,
            detail    VARCHAR(128) NULL,
            ts        DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_dst (dst_tg, ts),
            INDEX idx_src (src_tg, ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS kfc_reward_claim (
            tg      BIGINT      NOT NULL,
            period  VARCHAR(16) NOT NULL,   -- 形如 2026-W41 / 2026-10
            rank_no INT         NOT NULL,
            amount  INT         NOT NULL,
            ts      DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tg, period)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        # 求片登记
        # 群友自愿绑定：TG ID <-> Emby 用户名（用于用户榜显示头像与主页链接）
        """
        CREATE TABLE IF NOT EXISTS kfc_bind (
            tg        BIGINT      NOT NULL PRIMARY KEY,
            emby_name VARCHAR(64) NOT NULL,
            ts        DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS kfc_redeem_code (
            id      BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
            kind    VARCHAR(16)  NOT NULL,
            code    VARCHAR(64)  NOT NULL UNIQUE,
            used_by BIGINT       NULL,
            used_at DATETIME     NULL,
            ts      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_kind_used (kind, used_by)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS kfc_activity (
            tg   BIGINT NOT NULL,
            day  DATE   NOT NULL,
            msgs INT    NOT NULL DEFAULT 0,   -- 当天在群里发言条数
            acts INT    NOT NULL DEFAULT 0,   -- 当天完成修仙/兑换行为次数
            PRIMARY KEY (tg, day),
            INDEX idx_day (day)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS kfc_request (
            id        BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
            tg        BIGINT      NOT NULL,
            title     VARCHAR(128) NOT NULL,
            year      VARCHAR(8)  NULL,
            tmdb_id   VARCHAR(24) NULL,
            media     VARCHAR(16) NULL,
            status    VARCHAR(16) NOT NULL DEFAULT 'pending',
            note      VARCHAR(255) NULL,
            ts        DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_tg (tg, ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        # ── PT 全自动化 ──
        """
        CREATE TABLE IF NOT EXISTS kfc_wishlist (
            id       BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
            tg_id    VARCHAR(32)  DEFAULT '',
            tg_name  VARCHAR(128) DEFAULT '',
            keyword  VARCHAR(512) NOT NULL,
            title    VARCHAR(512) DEFAULT '',
            year     VARCHAR(16)  DEFAULT '',
            status   VARCHAR(32)  DEFAULT 'subscribed',
            created_at DATETIME   DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_keyword (keyword(191))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        # ── 求片审批（v4）：群友提交 → 管理员通过/驳回 → 才允许 MoviePilot 动手 ──
        """
        CREATE TABLE IF NOT EXISTS kfc_req_audit (
            id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
            tg           BIGINT       NOT NULL,
            tg_name      VARCHAR(128) DEFAULT '',
            tg_username  VARCHAR(64)  DEFAULT '',
            title        VARCHAR(255) NOT NULL,
            year         VARCHAR(16)  DEFAULT '',
            mtype        VARCHAR(16)  DEFAULT '',
            poster       VARCHAR(512) DEFAULT '',
            backdrop     VARCHAR(512) DEFAULT '',
            overview     TEXT,
            genres       VARCHAR(255) DEFAULT '',
            vote         VARCHAR(16)  DEFAULT '',
            release_date VARCHAR(32)  DEFAULT '',
            original     VARCHAR(255) DEFAULT '',
            link         VARCHAR(512) DEFAULT '',
            tmdb_id      VARCHAR(32)  DEFAULT '',
            media_source VARCHAR(32)  DEFAULT '',
            season       INT          DEFAULT 0,
            start_ep     INT          DEFAULT 0,
            total_ep     INT          DEFAULT 0,
            ep_count     INT          DEFAULT 0,
            scope        VARCHAR(64)  DEFAULT '',
            status       VARCHAR(16)  DEFAULT 'auditing',
            admin        BIGINT       DEFAULT 0,
            note         VARCHAR(255) DEFAULT '',
            sub_id       INT          DEFAULT 0,
            created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
            decided_at   DATETIME     NULL,
            INDEX idx_status (status, created_at),
            INDEX idx_tg (tg, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        # ── 投稿（2026-10-13）：群友直接投链接 / 磁力，审核通过后自动转给 MT 转存机器人 ──
        #   kind   magnet 磁力 / link 链接 / ed2k 电驴 / torrent 种子 / other 其他
        #   aid    关联的求片申请号（从求片卡片点「投稿」进来时带上，可为空）
        #   fwd_*  是否已转发给 @KFCMTZY_bot 及失败原因（转发失败要能重试）
        """
        CREATE TABLE IF NOT EXISTS kfc_contrib (
            id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
            tg           BIGINT       NOT NULL,
            tg_name      VARCHAR(128) DEFAULT '',
            tg_username  VARCHAR(64)  DEFAULT '',
            kind         VARCHAR(16)  DEFAULT 'other',
            payload      TEXT         NOT NULL,
            title        VARCHAR(255) DEFAULT '',
            year         VARCHAR(16)  DEFAULT '',
            size_txt     VARCHAR(64)  DEFAULT '',
            note         VARCHAR(512) DEFAULT '',
            aid          BIGINT       DEFAULT 0,
            status       VARCHAR(16)  DEFAULT 'auditing',
            admin        BIGINT       DEFAULT 0,
            admin_note   VARCHAR(255) DEFAULT '',
            prio         TINYINT      NOT NULL DEFAULT 0,
            fwd_ok       TINYINT      NOT NULL DEFAULT 0,
            fwd_err      VARCHAR(255) DEFAULT '',
            created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
            decided_at   DATETIME     NULL,
            INDEX idx_status (status, created_at),
            INDEX idx_tg (tg, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS kfc_pt_notify (
            id        BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
            kind      VARCHAR(32)  NOT NULL DEFAULT 'inbox',
            title     VARCHAR(512) NOT NULL,
            body      TEXT,
            site      VARCHAR(64)  DEFAULT '',
            category  VARCHAR(64)  DEFAULT '',
            origin    VARCHAR(32)  DEFAULT '',
            chat_id   VARCHAR(64)  DEFAULT '',
            status    VARCHAR(16)  DEFAULT 'pending',
            created_at DATETIME    DEFAULT CURRENT_TIMESTAMP,
            sent_at   DATETIME     NULL,
            INDEX idx_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS kfc_admin (
            tg          BIGINT       NOT NULL PRIMARY KEY,
            grants      VARCHAR(512) NOT NULL DEFAULT '',
            note        VARCHAR(64)  NOT NULL DEFAULT '',
            created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        # Telegram 账号绑定（Emby 账号 + 密码验证）
        # status: none 未开始 / pending 流程中 / failed 验证失败 /
        #         bound 已绑定 / disabled 已禁用 / unbound 已解绑
        # 密码用 MySQL AES_ENCRYPT 加密存储（密钥在 /app/keepalive/ka.key，600）
        """
        CREATE TABLE IF NOT EXISTS kfc_emby_bind (
            tg            BIGINT       NOT NULL PRIMARY KEY,
            tg_name       VARCHAR(64)  NOT NULL DEFAULT '',
            tg_username  VARCHAR(64)  NOT NULL DEFAULT '',
            status        VARCHAR(16)  NOT NULL DEFAULT 'none',
            emby_name     VARCHAR(64)  NOT NULL DEFAULT '',
            emby_uid      VARCHAR(32)  NOT NULL DEFAULT '',
            pw_hex        VARCHAR(512) NOT NULL DEFAULT '',
            fail_count    INT          NOT NULL DEFAULT 0,
            last_fail_ts  BIGINT       NOT NULL DEFAULT 0,
            bound_ts      BIGINT       NOT NULL DEFAULT 0,
            updated_ts    BIGINT       NOT NULL DEFAULT 0
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        # ── 三机器人统一层（2026-10-15）：成员档案 + V积分派送流水 ──
        """
        CREATE TABLE IF NOT EXISTS kfc_members (
            tg          BIGINT       NOT NULL PRIMARY KEY,
            tg_name     VARCHAR(64)  NOT NULL DEFAULT '',
            tg_username VARCHAR(64)  NOT NULL DEFAULT '',
            first_ts    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_seen (last_seen)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS kfc_point_transfer (
            id         BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
            from_tg    BIGINT      NOT NULL,
            to_tg      BIGINT      NOT NULL,
            amount     INT         NOT NULL,
            to_balance INT         NOT NULL DEFAULT 0,
            note       VARCHAR(128) NOT NULL DEFAULT '',
            ts         DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_from (from_tg, ts),
            INDEX idx_to (to_tg, ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ]
    with eng.begin() as c:
        for d in ddl:
            c.execute(text(d))
        # 灵石（游戏货币）单独成列，与 V积分(emby.us) 解耦：
        # 灵石只能在本游戏内流转（打坐/闭关/任务/红包/发言获得，坊市/求片消耗），
        # V积分 才是与 RT bot 1:1 互通、专用于兑换阁换码的那一个。
        try:
            c.execute(text(
                "ALTER TABLE kfc_cultivator ADD COLUMN ls INT NOT NULL DEFAULT 0"))
        except Exception:
            pass  # 列已存在
        # 求片申请：记录「已播报过站点暂无资源」的时间戳。
        # 单独一列而不是塞note —— note 会被后续状态变更覆盖，
        # 塞进去会导致同一部片被反复播报「暂无资源」。
        try:
            c.execute(text(
                "ALTER TABLE kfc_req_audit "
                "ADD COLUMN idle_notified_at DATETIME NULL"))
        except Exception:
            pass  # 列已存在
        # 求片申请：勾选式选集产生的「集数清单」（逗号分隔，如 "1,2,3,7,9"）。
        # 中间缺集时用户只勾想要的几集，MoviePilot 只支持「连续区间」订阅，
        # 所以下发时按连续段拆成多条订阅（见 request.cb_audit）。
        try:
            c.execute(text(
                "ALTER TABLE kfc_req_audit "
                "ADD COLUMN ep_list VARCHAR(512) NOT NULL DEFAULT ''"))
        except Exception:
            pass  # 列已存在
        # 求片申请：优先级（管理员「⭐ 优先处理」用）。
        #   0=普通；>0 的数值越大越靠前，待审核面板按 prio DESC 排。
        try:
            c.execute(text(
                "ALTER TABLE kfc_req_audit "
                "ADD COLUMN prio TINYINT NOT NULL DEFAULT 0"))
        except Exception:
            pass  # 列已存在
        # 求片申请：求片人/管理员填的**版本备注**（如「4K HDR 杜比视界」）。
        #   与 note 分开存：note 会被每次状态变更覆盖，备注要一直留着。
        try:
            c.execute(text(
                "ALTER TABLE kfc_req_audit "
                "ADD COLUMN remark VARCHAR(512) NOT NULL DEFAULT ''"))
        except Exception:
            pass  # 列已存在
    logger.info("游戏表结构检查完成")


# ============================================================================
#  V积分（共用 EmbyBoss 的 embyboss.emby.**iv**）
#  ★ 2026-10-15 重要更正：boss config money 名就叫「V积分」= iv 列；
#    us 列是 boss 自己的【注册天数池】（check_ex 每月自动续期扣 30us），
#    旧实现误把 us 当 V积分读写——会污染续期池！一律切到 iv。
# ============================================================================
def ensure_shared_user(tg: int) -> None:
    """保证 embyboss.emby 里有这个 TG 用户，否则 bot 加积分会无处可加"""
    with _engine.begin() as c:
        c.execute(text(
            f"INSERT IGNORE INTO `{S.SHARED_DB}`.emby (`tg`,`name`,`lv`,`us`,`iv`) "
            f"VALUES (:tg, NULL, 'd', 0, 0)"
        ), {"tg": tg})


def get_points(tg: int) -> int:
    with _engine.connect() as c:
        r = c.execute(text(
            f"SELECT iv FROM `{S.SHARED_DB}`.emby WHERE tg = :tg"), {"tg": tg}).first()
    return int(r[0] or 0) if r else 0


def points_map(tgs: list[int]) -> dict[int, int]:
    """批量取积分：一次查询拿回所有人，避免 for 循环里逐个查库"""
    ids = [int(t) for t in tgs]
    if not ids:
        return {}
    # 全部是 int，直接内联是安全的（且比 expanding bindparam 兼容性更好）
    inlist = ",".join(str(i) for i in ids)
    with _engine.connect() as c:
        rows = c.execute(text(
            f"SELECT tg, iv FROM `{S.SHARED_DB}`.emby WHERE tg IN ({inlist})")).all()
    return {int(r[0]): int(r[1] or 0) for r in rows}


def add_points(tg: int, delta: int) -> int:
    """增减 V积分（iv），返回新余额；余额不会低于 0"""
    ensure_shared_user(tg)
    with _engine.begin() as c:
        if delta >= 0:
            c.execute(text(
                f"UPDATE `{S.SHARED_DB}`.emby SET iv = iv + :d WHERE tg = :tg"),
                {"d": delta, "tg": tg})
        else:
            c.execute(text(
                f"UPDATE `{S.SHARED_DB}`.emby SET iv = GREATEST(iv + :d, 0) WHERE tg = :tg"),
                {"d": delta, "tg": tg})
        r = c.execute(text(
            f"SELECT iv FROM `{S.SHARED_DB}`.emby WHERE tg = :tg"), {"tg": tg}).first()
    return int(r[0] or 0)


def try_spend(tg: int, cost: int) -> bool:
    """原子扣费：够则扣并返回 True（V积分 / emby.iv —— boss 的樱花币/V积分列）

    ★ 2026-10-15：群主 V积分同样无限（转赠/兑换放行）。
    """
    tg = int(tg)
    ensure_shared_user(tg)
    try:
        from .settings import OWNER as _OWNER
        if _OWNER and tg == int(_OWNER):
            return True
    except Exception:
        pass
    with _lock, _engine.begin() as c:
        r = c.execute(text(
            f"UPDATE `{S.SHARED_DB}`.emby SET iv = iv - :c WHERE tg = :tg AND iv >= :c"),
            {"c": cost, "tg": tg})
        return r.rowcount > 0


def add_iv(tg: int, delta: int) -> int:
    """增减 V积分（emby.iv / boss 的 V积分钱包）。返回新余额。无角色者自动建一条。"""
    tg = int(tg)
    ensure_shared_user(tg)
    with _lock, _engine.begin() as c:
        if delta >= 0:
            c.execute(text(f"UPDATE `{S.SHARED_DB}`.emby SET iv = iv + :d WHERE tg = :tg"), {"d": delta, "tg": tg})
        else:
            c.execute(text(f"UPDATE `{S.SHARED_DB}`.emby SET iv = GREATEST(iv + :d, 0) WHERE tg = :tg"), {"d": delta, "tg": tg})
        r = c.execute(text(f"SELECT iv FROM `{S.SHARED_DB}`.emby WHERE tg = :tg"), {"tg": tg}).first()
    return int(r[0] or 0) if r else 0


# ============================================================================
#  灵石（游戏内货币，kfc_cultivator.ls，与 V积分 完全独立）
# ============================================================================
def get_ls(tg: int) -> int:
    with _engine.connect() as c:
        r = c.execute(text("SELECT ls FROM kfc_cultivator WHERE tg = :tg"),
                      {"tg": tg}).first()
    return int(r[0] or 0) if r else 0


def add_ls(tg: int, delta: int) -> int:
    """增减灵石，返回新余额；余额不会低于 0。无角色者自动建一条（道友/蜀山）。"""
    with _engine.begin() as c:
        c.execute(text(
            "INSERT IGNORE INTO kfc_cultivator (tg, daohao, sect) "
            "VALUES (:tg, '道友', 'shushan')"), {"tg": int(tg)})
        if delta >= 0:
            c.execute(text("UPDATE kfc_cultivator SET ls = ls + :d WHERE tg = :tg"),
                      {"d": delta, "tg": int(tg)})
        else:
            c.execute(text(
                "UPDATE kfc_cultivator SET ls = GREATEST(ls + :d, 0) WHERE tg = :tg"),
                {"d": delta, "tg": int(tg)})
        r = c.execute(text("SELECT ls FROM kfc_cultivator WHERE tg = :tg"),
                      {"tg": int(tg)}).first()
    return int(r[0] or 0)


def try_spend_ls(tg: int, cost: int) -> bool:
    """原子扣灵石：够则扣并返回 True。

    ★ 2026-10-15 用户拍板：群主灵石**无限**、可自由转赠 —— 群主扣款直接放行
    （不扣库存，只记流水），其他人照常原子扣减。
    """
    tg = int(tg)
    try:
        from .settings import OWNER as _OWNER
        if _OWNER and tg == int(_OWNER):
            return True
    except Exception:
        pass
    with _lock, _engine.begin() as c:
        r = c.execute(text(
            "UPDATE kfc_cultivator SET ls = ls - :c WHERE tg = :tg AND ls >= :c"),
            {"c": cost, "tg": tg})
        return r.rowcount > 0


def ls_map(tgs: list[int]) -> dict[int, int]:
    """批量取灵石余额"""
    ids = [int(t) for t in tgs]
    if not ids:
        return {}
    inlist = ",".join(str(i) for i in ids)
    with _engine.connect() as c:
        rows = c.execute(text(
            f"SELECT tg, ls FROM kfc_cultivator WHERE tg IN ({inlist})")).all()
    return {int(r[0]): int(r[1] or 0) for r in rows}


def convert_ls_to_us(tg: int, ls_amount: int) -> tuple[bool, int]:
    """灵石 → V积分（**emby.iv 钱包**）。按 S.LS_PER_V 换算，返回 (成功, 获得V积分)。
    只扣整数倍，零头留在灵石里，绝不丢失。

    ★ 2026-10-15 修复：旧实现误写 `us` 列——us 是 boss 的【注册天数池】
    （check_ex 自动续期扣 30us），往里面加积分会造成 boss 误自动开号/续期。
    V积分真身是 iv（boss config money 名即「V积分」）。
    """
    if ls_amount <= 0:
        return False, 0
    v_gain = ls_amount // int(S.LS_PER_V)
    if v_gain <= 0:
        return False, 0
    ls_spent = v_gain * int(S.LS_PER_V)
    with _lock, _engine.begin() as c:
        r = c.execute(text(
            "UPDATE kfc_cultivator SET ls = ls - :a WHERE tg = :tg AND ls >= :a"),
            {"a": ls_spent, "tg": int(tg)})
        if r.rowcount == 0:
            return False, 0
        c.execute(text(
            f"UPDATE `{S.SHARED_DB}`.emby SET iv = iv + :v WHERE tg = :tg"),
            {"v": v_gain, "tg": int(tg)})
    return True, v_gain


def convert_us_to_ls(tg: int, v_amount: int) -> tuple[bool, int]:
    """V积分 → 灵石（反向，1 V积分 = LS_PER_V 灵石）。返回 (成功, 获得灵石)。
    双向互转由用户拍板（2026-10-15）；仍原子：先扣 iv 成功才加 ls。"""
    if v_amount <= 0:
        return False, 0
    ensure_shared_user(tg)
    ls_gain = v_amount * int(S.LS_PER_V)
    with _lock, _engine.begin() as c:
        r = c.execute(text(
            f"UPDATE `{S.SHARED_DB}`.emby SET iv = iv - :v WHERE tg = :tg AND iv >= :v"),
            {"v": v_amount, "tg": int(tg)})
        if r.rowcount == 0:
            return False, 0
        c.execute(text(
            "INSERT IGNORE INTO kfc_cultivator (tg, daohao, sect) "
            "VALUES (:tg, '道友', 'shushan')"), {"tg": int(tg)})
        c.execute(text(
            "UPDATE kfc_cultivator SET ls = ls + :a WHERE tg = :tg"),
            {"a": ls_gain, "tg": int(tg)})
    return True, ls_gain


# ============================================================================
#  修士
# ============================================================================
def get_cultivator(tg: int) -> dict | None:
    with _engine.connect() as c:
        row = c.execute(text("SELECT * FROM kfc_cultivator WHERE tg = :tg"), {"tg": tg}).mappings().first()
    return dict(row) if row else None


def create_cultivator(tg: int, daohao: str, sect: str) -> None:
    with _engine.begin() as c:
        c.execute(text(
            "INSERT INTO kfc_cultivator (tg, daohao, sect, realm, cultivation) "
            "VALUES (:tg, :d, :s, 1, 0) "
            "ON DUPLICATE KEY UPDATE daohao = VALUES(daohao), sect = VALUES(sect)"
        ), {"tg": tg, "d": daohao, "s": sect})


def update_cultivator(tg: int, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"`{k}` = :{k}" for k in fields)
    params = dict(fields, tg=tg)
    with _engine.begin() as c:
        c.execute(text(f"UPDATE kfc_cultivator SET {sets} WHERE tg = :tg"), params)


def tg_by_emby_name(name: str) -> int | None:
    """按 Emby 用户名反查 TG ID（用户自愿 /bind 绑定的那张表）"""
    if not name:
        return None
    with _engine.connect() as c:
        r = c.execute(text("SELECT tg FROM kfc_bind WHERE emby_name = :n LIMIT 1"),
                      {"n": str(name).strip()}).first()
    return int(r[0]) if r else None


def top_cultivators(limit: int = 20, order: str = "realm DESC, cultivation DESC") -> list[dict]:
    with _engine.connect() as c:
        rows = c.execute(text(
            f"SELECT * FROM kfc_cultivator ORDER BY {order} LIMIT :n"), {"n": limit}).mappings().all()
    return [dict(r) for r in rows]


def rank_of(tg: int) -> int | None:
    cv = get_cultivator(tg)
    if not cv:
        return None
    with _engine.connect() as c:
        r = c.execute(text(
            "SELECT COUNT(*) + 1 FROM kfc_cultivator "
            "WHERE realm > :r OR (realm = :r AND cultivation > :c)"),
            {"r": cv["realm"], "c": cv["cultivation"]}).first()
    return int(r[0])


def all_ids() -> list[int]:
    with _engine.connect() as c:
        rows = c.execute(text("SELECT tg FROM kfc_cultivator")).all()
    return [int(r[0]) for r in rows]


# ============================================================================
#  每日任务 / 成就 / 日志
# ============================================================================
def bump_task(tg: int, key: str, inc: int = 1) -> None:
    today = dt.date.today()
    need = 2 if key == "train" else (2 if key == "visit" else 1)
    with _engine.begin() as c:
        c.execute(text(
            "INSERT INTO kfc_daily_task (tg, day, task_key, cnt, done) VALUES (:tg,:d,:k,:i,0) "
            "ON DUPLICATE KEY UPDATE cnt = cnt + :i, done = IF(cnt + :i >= :need, 1, 0)"
        ), {"tg": tg, "d": today, "k": key, "i": inc, "need": need})


def task_state(tg: int) -> dict[str, tuple[int, int]]:
    today = dt.date.today()
    with _engine.connect() as c:
        rows = c.execute(text(
            "SELECT task_key, cnt, done FROM kfc_daily_task WHERE tg = :tg AND day = :d"),
            {"tg": tg, "d": today}).all()
    return {r[0]: (int(r[1]), int(r[2])) for r in rows}


def unlock_ach(tg: int, key: str) -> bool:
    """返回 True 表示本次新解锁"""
    with _engine.begin() as c:
        r = c.execute(text(
            "INSERT IGNORE INTO kfc_achievement (tg, ach_key) VALUES (:tg, :k)"),
            {"tg": tg, "k": key})
        return r.rowcount > 0


def my_achievements(tg: int) -> set[str]:
    with _engine.connect() as c:
        rows = c.execute(text("SELECT ach_key FROM kfc_achievement WHERE tg = :tg"), {"tg": tg}).all()
    return {r[0] for r in rows}


def log_action(kind: str, src: int, dst: int | None = None, amount: int = 0, detail: str = "") -> None:
    with _engine.begin() as c:
        c.execute(text(
            "INSERT INTO kfc_action_log (kind, src_tg, dst_tg, amount, detail) "
            "VALUES (:k, :s, :d, :a, :t)"),
            {"k": kind, "s": src, "d": dst, "a": amount, "t": detail})


def count_action(tg: int, kind: str) -> int:
    with _engine.connect() as c:
        r = c.execute(text(
            "SELECT COUNT(*) FROM kfc_action_log WHERE src_tg = :tg AND kind = :k "
            "AND detail = 'win'"), {"tg": tg, "k": kind}).first()
    return int(r[0]) if r else 0


def record_claim(tg: int, period: str, rank_no: int, amount: int) -> bool:
    with _engine.begin() as c:
        r = c.execute(text(
            "INSERT IGNORE INTO kfc_reward_claim (tg, period, rank_no, amount) "
            "VALUES (:tg, :p, :r, :a)"),
            {"tg": tg, "p": period, "r": rank_no, "a": amount})
        return r.rowcount > 0


def has_claimed(tg: int, period: str) -> bool:
    with _engine.connect() as c:
        r = c.execute(text(
            "SELECT 1 FROM kfc_reward_claim WHERE tg = :tg AND period = :p"),
            {"tg": tg, "p": period}).first()
    return bool(r)


# ============================================================================
#  自愿绑定：TG ID <-> Emby 用户名
# ============================================================================
def set_bind(tg: int, emby_name: str) -> None:
    with _engine.begin() as c:
        c.execute(text(
            "INSERT INTO kfc_bind (tg, emby_name) VALUES (:tg, :n) "
            "ON DUPLICATE KEY UPDATE emby_name = VALUES(emby_name), ts = NOW()"
        ), {"tg": tg, "n": emby_name})


def my_bind(tg: int) -> str | None:
    with _engine.connect() as c:
        r = c.execute(text(
            "SELECT emby_name FROM kfc_bind WHERE tg = :tg"), {"tg": tg}).first()
    return r[0] if r else None


def bind_map() -> dict[str, int]:
    """Emby 用户名 -> TG ID（用户自己绑定的，最可靠）"""
    with _engine.connect() as c:
        rows = c.execute(text("SELECT tg, emby_name FROM kfc_bind")).all()
    return {str(r[1]): int(r[0]) for r in rows}


# ============================================================================
#  兑换码库存（注册码 / 续期码 / 白名单）
# ============================================================================
def add_codes(kind: str, codes: list[str]) -> int:
    """批量入库，已存在的自动跳过；返回真正新增的条数"""
    n = 0
    with _engine.begin() as c:
        for code in codes:
            code = str(code).strip()
            if not code or len(code) > 64:
                continue
            r = c.execute(text(
                "INSERT IGNORE INTO kfc_redeem_code (kind, code) VALUES (:k, :c)"),
                {"k": kind, "c": code})
            n += int(r.rowcount or 0)
    return n


def take_code(kind: str, tg: int) -> str | None:
    """原子地领走一枚未使用的码；库存为空返回 None"""
    with _lock, _engine.begin() as c:
        r = c.execute(text(
            "SELECT id, code FROM kfc_redeem_code "
            "WHERE kind = :k AND used_by IS NULL ORDER BY id LIMIT 1"),
            {"k": kind}).first()
        if not r:
            return None
        u = c.execute(text(
            "UPDATE kfc_redeem_code SET used_by = :tg, used_at = NOW() "
            "WHERE id = :i AND used_by IS NULL"), {"tg": tg, "i": r[0]})
        if u.rowcount != 1:
            return None
        return str(r[1])


def code_stock() -> dict[str, int]:
    """每种兑换物的剩余库存（来自 kfc_stock 预铸表）。
    返回 REDEEM_ITEMS 全部 key 的库存，缺省 0。表不存在时静默返回 0。"""
    out = {k: 0 for k in S.REDEEM_ITEMS}
    try:
        with _engine.connect() as c:
            rows = c.execute(text("SELECT kind, stock FROM kfc_stock")).all()
        for r in rows:
            out[str(r[0])] = int(r[1])
    except Exception:
        pass
    return out


def stock_inc(kind: str, n: int) -> int:
    """补货：kfc_stock 指定类型 +n，返回补货后库存（类型自动建行）"""
    with _engine.begin() as c:
        c.execute(text(
            "INSERT INTO kfc_stock (kind, stock) VALUES (:k, :n) "
            "ON DUPLICATE KEY UPDATE stock = stock + :n"),
            {"k": str(kind), "n": int(n)})
        r = c.execute(text("SELECT stock FROM kfc_stock WHERE kind=:k"),
                      {"k": str(kind)}).first()
    return int(r[0]) if r else 0


def stock_dec(kind: str, n: int = 1) -> int:
    """消耗库存：kfc_stock 指定类型 -n（下限 0），返回剩余"""
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_stock SET stock = GREATEST(stock - :n, 0) "
            "WHERE kind = :k"), {"k": str(kind), "n": int(n)})
        r = c.execute(text("SELECT stock FROM kfc_stock WHERE kind=:k"),
                      {"k": str(kind)}).first()
    return int(r[0]) if r else 0


def code_used() -> dict[str, int]:
    """每种码已发放数量"""
    with _engine.connect() as c:
        rows = c.execute(text(
            "SELECT kind, COUNT(*) FROM kfc_redeem_code "
            "WHERE used_by IS NOT NULL GROUP BY kind")).all()
    return {str(r[0]): int(r[1]) for r in rows}


def release_code(code: str) -> None:
    """兑换失败时把码放回库存（避免浪费）"""
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_redeem_code SET used_by = NULL, used_at = NULL "
            "WHERE code = :c AND used_by IS NOT NULL"), {"c": code})


def my_codes(tg: int) -> list[dict]:
    """我领过的码"""
    with _engine.connect() as c:
        rows = c.execute(text(
            "SELECT kind, code, used_at FROM kfc_redeem_code "
            "WHERE used_by = :tg ORDER BY used_at DESC LIMIT 30"),
            {"tg": tg}).mappings().all()
    return [dict(r) for r in rows]


# ============================================================================
#  活跃度（兑换门槛用）：统计「持续发言 / 参与」天数
# ============================================================================
def bump_activity(tg: int, kind: str = "msg") -> None:
    """记录今天的活跃：kind='msg' 记发言，kind='act' 记行为。
    同一天多次只算一次活跃（PK 去重，msgs/acts 仅作计数）。
    发言（kind='msg'）每天前 SPEAK_CAP 条各给 SPEAK_LS 灵石（无角色者自动忽略）。"""
    today = dt.date.today()
    col = "msgs" if kind == "msg" else "acts"
    with _engine.begin() as c:
        c.execute(text(
            f"INSERT INTO kfc_activity (tg, day, {col}) VALUES (:tg, :d, 1) "
            f"ON DUPLICATE KEY UPDATE {col} = {col} + 1"),
            {"tg": int(tg), "d": today})
        if kind == "msg":
            r = c.execute(text(
                "SELECT msgs FROM kfc_activity WHERE tg = :tg AND day = :d"),
                {"tg": int(tg), "d": today}).first()
            if r and r[0] <= int(S.SPEAK_CAP):
                c.execute(text(
                    "UPDATE kfc_cultivator SET ls = ls + :g WHERE tg = :tg"),
                    {"g": int(S.SPEAK_LS), "tg": int(tg)})


def active_days(tg: int, window: int) -> int:
    """统计最近 window 天里「活跃」的天数。
    活跃 = 当天 msgs>0 或 acts>0。"""
    today = dt.date.today()
    start = today - dt.timedelta(days=max(int(window) - 1, 0))
    with _engine.connect() as c:
        r = c.execute(text(
            "SELECT COUNT(*) FROM kfc_activity "
            "WHERE tg = :tg AND day >= :s AND (msgs > 0 OR acts > 0)"),
            {"tg": int(tg), "s": start}).scalar()
    return int(r or 0)


def add_request(tg: int, title: str, year: str = "", tmdb_id: str = "",
                media: str = "", note: str = "") -> int:
    with _engine.begin() as c:
        r = c.execute(text(
            "INSERT INTO kfc_request (tg, title, year, tmdb_id, media, note) "
            "VALUES (:tg,:t,:y,:i,:m,:n)"),
            {"tg": tg, "t": title, "y": year, "i": tmdb_id, "m": media, "n": note})
        return int(r.lastrowid or 0)


def set_request_status(rid: int, status: str, note: str = "") -> None:
    with _engine.begin() as c:
        c.execute(text("UPDATE kfc_request SET status = :s, note = :n WHERE id = :i"),
                  {"s": status, "n": note, "i": rid})


# ════════════════════════════════════════════════════════════════════
#  求片审批：群友提交申请 → 管理员通过 / 驳回 → 才允许 MoviePilot 动手
#  status: auditing(待审核) / approved(已通过) / rejected(已驳回)
#          / done(已下发订阅) / failed(下发失败)
# ════════════════════════════════════════════════════════════════════
_AUDIT_COLS = (
    "id, tg, tg_name, tg_username, title, year, mtype, poster, backdrop, "
    "overview, genres, vote, release_date, original, link, tmdb_id, "
    "media_source, season, start_ep, total_ep, ep_count, ep_list, scope, status, "
    "admin, note, sub_id, created_at, decided_at, prio, remark"
)


def add_audit(**kw) -> int:
    """登记一条求片申请（默认待审核）"""
    cols = ["tg", "tg_name", "tg_username", "title", "year", "mtype", "poster",
            "backdrop", "overview", "genres", "vote", "release_date", "original",
            "link", "tmdb_id", "media_source", "season", "start_ep", "total_ep",
            "ep_count", "ep_list", "scope"]
    vals = {c: kw.get(c, "" if c not in ("season", "start_ep", "total_ep",
                                         "ep_count") else 0) for c in cols}
    for c in ("season", "start_ep", "total_ep", "ep_count"):
        try:
            vals[c] = int(vals[c] or 0)
        except Exception:
            vals[c] = 0
    for c, n in (("tg_name", 120), ("tg_username", 64), ("title", 255),
                 ("year", 16), ("mtype", 16), ("genres", 255), ("vote", 16),
                 ("release_date", 32), ("original", 255), ("tmdb_id", 32),
                 ("media_source", 32), ("scope", 64), ("ep_list", 512)):
        vals[c] = str(vals.get(c) or "")[:n]
    sql = (f"INSERT INTO kfc_req_audit ({','.join(cols)}) "
           f"VALUES ({','.join(':' + c for c in cols)})")
    with _engine.begin() as c:
        r = c.execute(text(sql), vals)
        return int(r.lastrowid or 0)


def get_audit(aid: int) -> dict | None:
    with _engine.connect() as c:
        r = c.execute(text(f"SELECT {_AUDIT_COLS} FROM kfc_req_audit WHERE id = :i"),
                      {"i": int(aid)}).mappings().first()
    return dict(r) if r else None


def set_audit(aid: int, status: str, admin: int = 0, note: str = "",
              sub_id: int = 0) -> None:
    """更新求片申请状态。

    sub_id 传 0 时保留原值（避免「撤回」这类只改状态的操作把订阅号抹掉）。
    """
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_req_audit SET status = :s, admin = :a, note = :n, "
            "sub_id = CASE WHEN :sid = 0 THEN sub_id ELSE :sid END, "
            "decided_at = NOW() WHERE id = :i"),
            {"s": status, "a": int(admin or 0), "n": (note or "")[:200],
             "sid": int(sub_id or 0), "i": int(aid)})


def set_audit_note(aid: int, note: str, admin: int = 0) -> None:
    """只改求片申请的**备注**，不动 status / decided_at。

    用于「求此影片其他质量版本」填备注：申请仍处于待审核，
    不能用 set_audit（它会把 decided_at 写成 NOW()，让待审核列表判断失真）。
    """
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_req_audit SET note = :n, "
            "admin = CASE WHEN :a = 0 THEN admin ELSE :a END WHERE id = :i"),
            {"n": (note or "")[:200], "a": int(admin or 0), "i": int(aid)})


def list_audits(status: str = "", limit: int = 20) -> list[dict]:
    """列出某个状态的求片申请。

    ★ 待审核列表按**优先级**排：管理员标了「⭐ 优先」的排在最前，
      其余按提交顺序（老的在前面，先求的先处理）。
      其他状态页签按 id 倒序（最近处理的在最上），符合直觉。
    """
    where = "WHERE status = :s" if status else ""
    order = ("ORDER BY prio DESC, id ASC" if status == "auditing"
             else "ORDER BY id DESC")
    with _engine.connect() as c:
        rows = c.execute(text(
            f"SELECT {_AUDIT_COLS} FROM kfc_req_audit {where} "
            f"{order} LIMIT :l"),
            ({"s": status, "l": int(limit)} if status else {"l": int(limit)})
        ).mappings().all()
    return [dict(r) for r in rows]


def set_audit_prio(aid: int, prio: int) -> None:
    """设置求片优先级（0=普通，1=优先，2=加急）"""
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_req_audit SET prio = :p WHERE id = :i"),
            {"p": max(0, min(9, int(prio or 0))), "i": int(aid)})


def set_audit_remark(aid: int, remark: str, admin: int = 0) -> None:
    """写入版本备注（求片人/管理员标注「要 4K HDR」这类指定版本）。

    ★ 与 note 分开：note 会被每次状态变更（通过/驳回/下发）覆盖，
      而备注必须一直跟着这条申请，管理员下发时才看得到要找什么版本。
    """
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_req_audit SET remark = :r, "
            "admin = CASE WHEN :a = 0 THEN admin ELSE :a END WHERE id = :i"),
            {"r": (remark or "")[:500], "a": int(admin or 0), "i": int(aid)})


def list_audits_all(limit: int = 200) -> list[dict]:
    """列出**所有状态**的求片申请（订阅与求片管理中心用）。

    与 list_audits("") 等价，但函数名自解释，避免调用方误以为要传状态。
    """
    with _engine.connect() as c:
        rows = c.execute(text(
            f"SELECT {_AUDIT_COLS} FROM kfc_req_audit "
            f"ORDER BY id DESC LIMIT :l"), {"l": int(limit)}
        ).mappings().all()
    return [dict(r) for r in rows]


def my_audits(tg: int, limit: int = 10) -> list[dict]:
    with _engine.connect() as c:
        rows = c.execute(text(
            f"SELECT {_AUDIT_COLS} FROM kfc_req_audit WHERE tg = :tg "
            f"ORDER BY id DESC LIMIT :l"), {"tg": int(tg), "l": int(limit)}
        ).mappings().all()
    return [dict(r) for r in rows]


def count_audits(status: str) -> int:
    with _engine.connect() as c:
        r = c.execute(text(
            "SELECT COUNT(*) FROM kfc_req_audit WHERE status = :s"),
            {"s": status}).scalar()
    return int(r or 0)


def today_audits(limit: int = 60) -> list[dict]:
    """今日（按 created_at）的全部求片申请 —— 管理员「今日总求片」面板用。

    ★ 时区坑（实测）：MySQL 容器跑在 **UTC**（`NOW()` 比北京时间慢 8 小时），
      而 bot 展示的是北京时间。直接用 `CURDATE()` 的话，
      北京时间 00:00–08:00 之间会把「今天」算成昨天，当天新求的片一条都不显示。
      所以两边都先 +8 小时再取日期。
    """
    with _engine.connect() as c:
        rows = c.execute(text(
            f"SELECT {_AUDIT_COLS} FROM kfc_req_audit "
            "WHERE DATE(created_at + INTERVAL 8 HOUR) "
            "    = DATE(NOW() + INTERVAL 8 HOUR) "
            "ORDER BY id DESC LIMIT :l"),
            {"l": int(limit)}).mappings().all()
    return [dict(r) for r in rows]


def idle_subscribed(minutes: int = 45, limit: int = 30) -> list[dict]:
    """已下发订阅、但过去 minutes 分钟内 pt2cloud 里查不到任何进度的求片。

    ★ 这就是「站点暂无资源」的判定依据 —— MoviePilot 搜不到资源时**不会**
    通知任何人（只自己标 cooldown 稍后继续），所以只能由 bot 主动比对：
        status = done（订阅已建立）且 idle_notified_at 为空（还没播报过）
      + pt_uploads 里既没有 downloading / uploaded，也没有 archived
      ⇒ 说明从下发到现在一个字节都没动起来 = 站点确实没资源

    只取 done 状态：auditing（还没批）、rejected（被拒）都不该播报。
    """
    with _engine.connect() as c:
        # ★ 时间基准用 COALESCE(decided_at, created_at) —— decided_at 是
        #   「审核通过/下发」的时刻，这才是等待的起点；老代码用 created_at
        #   且方向写成 >=，结果等价于「求片发生在最近 N 分钟内」，
        #   真正的老求片（等了几小时还没资源）反倒永远不播报。
        #   条件是「距下发已超过 minutes 分钟」。
        rows = c.execute(text(
            f"SELECT {_AUDIT_COLS} FROM kfc_req_audit "
            "WHERE status = 'done' AND idle_notified_at IS NULL "
            "AND COALESCE(decided_at, created_at) "
            "<= NOW() - INTERVAL :m MINUTE "
            "ORDER BY id DESC LIMIT :l"),
            {"m": int(minutes), "l": int(limit)}).mappings().all()
    out = []
    for r in (dict(x) for x in rows):
        keys = [k for k in (r.get("title"), r.get("original")) if k and len(k) >= 2]
        if not keys:
            continue
        if any(wish_progress([k]) for k in keys):
            continue                       # 有进度 ⇒ 资源已找到，不该播报
        out.append(r)
    return out


def mark_idle_notified(aid: int) -> None:
    """标记「已播报过暂无资源」—— 独立列，不污染 note。"""
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_req_audit SET idle_notified_at = NOW() WHERE id = :i"),
            {"i": int(aid)})


def clear_idle_notified(aid: int) -> None:
    """重新下发订阅后清掉标记 —— 让新的一轮等待能再次播报。"""
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_req_audit SET idle_notified_at = NULL WHERE id = :i"),
            {"i": int(aid)})


def del_audit(aid: int) -> None:
    """彻底删除一条求片申请记录（不可恢复）。

    只给管理台的「🗑 彻底删除」用；日常「取消求片」应该走
    set_audit(..., "rejected"/"approved")，不要直接删行——
    删了就没法反悔，也查不到是谁求的。
    """
    with _engine.begin() as c:
        c.execute(text("DELETE FROM kfc_req_audit WHERE id = :i"),
                  {"i": int(aid)})


# ════════════════════════════════════════════════════════════════════
#  投稿：群友投链接 / 磁力 → 管理员审核 → 自动转给 MT 转存机器人
#  status: auditing(待审核) / approved(已通过·已转发) / rejected(已驳回)
#          / pending_fwd(已通过但转发失败，可重试)
# ════════════════════════════════════════════════════════════════════
_CONTRIB_COLS = (
    "id, tg, tg_name, tg_username, kind, payload, title, year, size_txt, "
    "note, aid, status, admin, admin_note, prio, fwd_ok, fwd_err, "
    "created_at, decided_at"
)


def add_contrib(**kw) -> int:
    """登记一条投稿（默认待审核）"""
    cols = ["tg", "tg_name", "tg_username", "kind", "payload", "title",
            "year", "size_txt", "note", "aid"]
    vals = {}
    for c in cols:
        v = kw.get(c, "" if c != "aid" else 0)
        vals[c] = int(v or 0) if c == "aid" else str(v or "")
    for c, n in (("tg_name", 120), ("tg_username", 64), ("kind", 16),
                 ("title", 255), ("year", 16), ("size_txt", 64)):
        vals[c] = str(vals.get(c) or "")[:n]
    vals["note"] = str(vals.get("note") or "")[:500]
    sql = (f"INSERT INTO kfc_contrib ({','.join(cols)}) "
           f"VALUES ({','.join(':' + c for c in cols)})")
    with _engine.begin() as c:
        r = c.execute(text(sql), vals)
        return int(r.lastrowid or 0)


def get_contrib(cid: int) -> dict | None:
    with _engine.connect() as c:
        r = c.execute(text(
            f"SELECT {_CONTRIB_COLS} FROM kfc_contrib WHERE id = :i"),
            {"i": int(cid)}).mappings().first()
    return dict(r) if r else None


def set_contrib(cid: int, status: str, admin: int = 0,
                admin_note: str = "") -> None:
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_contrib SET status = :s, admin = :a, "
            "admin_note = :n, decided_at = NOW() WHERE id = :i"),
            {"s": status, "a": int(admin or 0),
             "n": (admin_note or "")[:200], "i": int(cid)})


def set_contrib_note(cid: int, note: str) -> None:
    with _engine.begin() as c:
        c.execute(text("UPDATE kfc_contrib SET note = :n WHERE id = :i"),
                  {"n": (note or "")[:500], "i": int(cid)})


def set_contrib_title(cid: int, title: str) -> None:
    with _engine.begin() as c:
        c.execute(text("UPDATE kfc_contrib SET title = :t WHERE id = :i"),
                  {"t": (title or "")[:255], "i": int(cid)})


def set_contrib_prio(cid: int, prio: int) -> None:
    with _engine.begin() as c:
        c.execute(text("UPDATE kfc_contrib SET prio = :p WHERE id = :i"),
                  {"p": max(0, min(9, int(prio or 0))), "i": int(cid)})


def mark_contrib_fwd(cid: int, ok: bool, err: str = "") -> None:
    """记录「转发给 MT 机器人」的结果（失败要能一键重试）"""
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_contrib SET fwd_ok = :o, fwd_err = :e WHERE id = :i"),
            {"o": 1 if ok else 0, "e": (err or "")[:200], "i": int(cid)})


def list_contribs(status: str = "", limit: int = 20) -> list[dict]:
    """列出某状态的投稿。待审核按「优先在前、老的在先」，其余按最新在前。"""
    where = "WHERE status = :s" if status else ""
    order = ("ORDER BY prio DESC, id ASC" if status == "auditing"
             else "ORDER BY id DESC")
    with _engine.connect() as c:
        rows = c.execute(text(
            f"SELECT {_CONTRIB_COLS} FROM kfc_contrib {where} "
            f"{order} LIMIT :l"),
            ({"s": status, "l": int(limit)} if status else {"l": int(limit)})
        ).mappings().all()
    return [dict(r) for r in rows]


def my_contribs(tg: int, limit: int = 10) -> list[dict]:
    with _engine.connect() as c:
        rows = c.execute(text(
            f"SELECT {_CONTRIB_COLS} FROM kfc_contrib WHERE tg = :tg "
            f"ORDER BY id DESC LIMIT :l"), {"tg": int(tg), "l": int(limit)}
        ).mappings().all()
    return [dict(r) for r in rows]


def count_contribs(status: str) -> int:
    with _engine.connect() as c:
        r = c.execute(text(
            "SELECT COUNT(*) FROM kfc_contrib WHERE status = :s"),
            {"s": status}).scalar()
    return int(r or 0)


def del_contrib(cid: int) -> None:
    with _engine.begin() as c:
        c.execute(text("DELETE FROM kfc_contrib WHERE id = :i"),
                  {"i": int(cid)})


# ════════════════════════════════════════════════════════════════════
#  PT 全自动化：求片清单 + 入库通知队列
#
#  kfc_wishlist   —— 群友求片记录。pt2cloud 用它判断某个种子属于「求片」
#                    还是「订阅」，从而归档到 115 的不同子目录。
#  kfc_pt_notify  —— pt2cloud 写入、本 bot 消费的通知队列。
#                    kind: uploaded(已传云端) / organized(已入库) /
#                          failed(上传失败) / daily(保号日报)
# ════════════════════════════════════════════════════════════════════
_CTL_RE = None


def _clean_name(s) -> str:
    """剔除控制字符（\x00-\x1f\x7f）——替罪求片人 tg_name 存成 \x7f 乱码的根因。
    TG 的 first_name 理论上不含这些，但爬虫/剪贴板来源的数据会带。"""
    global _CTL_RE
    if _CTL_RE is None:
        import re as _re
        _CTL_RE = _re.compile(r"[\x00-\x1f\x7f]")
    return _CTL_RE.sub("", str(s or "")).strip()


def add_wishlist(tg: int, tg_name: str, keyword: str, title: str = "",
                 year: str = "") -> None:
    """求片成功后登记，供 pt2cloud 识别种子来源。

    同时存中文标题与英文原名（original），因为 PT 种子名多为英文，
    只存中文会匹配不上。
    """
    kw = (keyword or "").strip()
    if not kw:
        return
    tg_name = _clean_name(tg_name)[:120]
    title = _clean_name(title)
    with _engine.begin() as c:
        # 同一关键词重复求片只更新状态，不堆重复行
        r = c.execute(text(
            "SELECT id FROM kfc_wishlist WHERE keyword = :k LIMIT 1"),
            {"k": kw[:500]}).first()
        if r:
            c.execute(text(
                "UPDATE kfc_wishlist SET status = 'subscribed', "
                "tg_id = :tg, tg_name = :n, title = :t, year = :y WHERE id = :i"),
                {"tg": str(tg), "n": (tg_name or "")[:120],
                 "t": (title or kw)[:500], "y": (year or "")[:16], "i": r[0]})
        else:
            c.execute(text(
                "INSERT INTO kfc_wishlist (tg_id, tg_name, keyword, title, year, status) "
                "VALUES (:tg,:n,:k,:t,:y,'subscribed')"),
                {"tg": str(tg), "n": (tg_name or "")[:120], "k": kw[:500],
                 "t": (title or kw)[:500], "y": (year or "")[:16]})


def wish_keywords(limit: int = 500) -> list[str]:
    """返回求片关键词列表，供下载盘归类判定种子来源=求片。

    ★ 必须与 pt2cloud 的 db.wish_keywords() 规则保持一致
      （keyword + title 两列、按 id DESC 限条数、不过滤 status、长度 >= 2 才收），
      否则会出现「本地标求片、115 云端标订阅」的错位。
    """
    out: list[str] = []
    try:
        with _engine.connect() as c:
            rows = c.execute(text(
                "SELECT keyword, title FROM kfc_wishlist "
                "ORDER BY id DESC LIMIT :n"), {"n": int(limit)}).fetchall()
        for r in rows:
            for v in (r[0], r[1]):
                v = str(v or "").strip()
                if len(v) >= 2:
                    out.append(v)
    except Exception:
        pass
    return out


_PT_COLS = (
    "id, kind, title, body, site, category, origin, "
    "tmdb_id, poster, media_title, year, media_type, "
    "size_gb, quality, torrent_name, created_at, "
    "chat_id, msg_id, need_edit, progress, speed_bps, eta_sec, "
    "done_size, total_size, stage, bitrate, req_tg, req_name, req_username"
)


def pending_notifies(limit: int = 10) -> list[dict]:
    """取待播报的入库通知"""
    with _engine.connect() as c:
        rows = c.execute(text(
            f"SELECT {_PT_COLS} FROM kfc_pt_notify WHERE status = 'pending' "
            "ORDER BY id LIMIT :l"), {"l": int(limit)}).mappings().all()
    return [dict(r) for r in rows]


def editable_notifies(limit: int = 10) -> list[dict]:
    """需要原地刷新（进度变化）的群内消息"""
    with _engine.connect() as c:
        rows = c.execute(text(
            f"SELECT {_PT_COLS} FROM kfc_pt_notify "
            "WHERE need_edit = 1 AND msg_id > 0 AND chat_id <> '' "
            "ORDER BY updated_at LIMIT :l"), {"l": int(limit)}).mappings().all()
    return [dict(r) for r in rows]


def dl_cards(limit: int = 50) -> list[dict]:
    """群内已发出的「下载中」卡片（用于清理/批量刷新）"""
    with _engine.connect() as c:
        rows = c.execute(text(
            f"SELECT {_PT_COLS} FROM kfc_pt_notify "
            "WHERE kind = 'downloading' AND msg_id > 0 AND chat_id <> '' "
            "ORDER BY id DESC LIMIT :l"), {"l": int(limit)}).mappings().all()
    return [dict(r) for r in rows]


def clear_edit(ids: list[int]) -> None:
    if not ids:
        return
    with _engine.begin() as c:
        for i in ids:
            c.execute(text("UPDATE kfc_pt_notify SET need_edit = 0 WHERE id = :i"),
                      {"i": int(i)})


def pt_active(limit: int = 30) -> list[dict]:
    """在途任务（/dl 展示实时进度）"""
    with _engine.connect() as c:
        rows = c.execute(text(
            "SELECT torrent_hash, torrent_name, category, site, origin, "
            "       tmdb_id, poster, media_title, year, media_type, quality, "
            "       total_size, file_count, status, stage, "
            "       progress, speed_bps, eta_sec, dl_size, qb_state, "
            "       notify_id, start_at, done_at, updated_at "
            "FROM pt2cloud.pt_uploads "
            "WHERE status NOT IN ('archived', 'pruned', 'skipped', 'failed') "
            "ORDER BY id DESC LIMIT :l"), {"l": int(limit)}).mappings().all()
    return [dict(r) for r in rows]


def pt_recent_archived(limit: int = 10) -> list[dict]:
    """最近入库完成的（/dl 底部展示）"""
    with _engine.connect() as c:
        rows = c.execute(text(
            "SELECT media_title, year, quality, total_size, status, updated_at, "
            "       tmdb_id, poster "
            "FROM pt2cloud.pt_uploads "
            "WHERE status = 'archived' "
            "ORDER BY updated_at DESC LIMIT :l"), {"l": int(limit)}).mappings().all()
    return [dict(r) for r in rows]


def today_wishes(limit: int = 30) -> list[dict]:
    """今日求片记录"""
    with _engine.connect() as c:
        rows = c.execute(text(
            "SELECT id, tg_id, tg_name, keyword, title, year, status, created_at "
            "FROM kfc_wishlist WHERE DATE(created_at) = CURDATE() "
            "ORDER BY id DESC LIMIT :l"), {"l": int(limit)}).mappings().all()
    return [dict(r) for r in rows]


def wish_rows_by_keywords(keywords: list[str]) -> dict:
    """按关键词反查在途任务的最新状态（今日求片 -> 进度）"""
    if not keywords:
        return {}
    out = {}
    with _engine.connect() as c:
        for kw in keywords:
            if not kw:
                continue
            rows = c.execute(text(
                "SELECT status, progress, stage, speed_bps, eta_sec, "
                "       dl_size, total_size, media_title, year, quality "
                "FROM pt2cloud.pt_uploads "
                "WHERE torrent_name LIKE :k OR media_title LIKE :k "
                "ORDER BY id DESC LIMIT 1"),
                {"k": f"%{kw}%"}).mappings().all()
            if rows:
                out[kw] = dict(rows[0])
    return out


# 片名归一化：只留 数字/字母/CJK，其余（空格 . - _ : 等）全部丢弃。
# PT 种子名（Dune.2021.2160p...）与求片关键词（Dune）靠它才能对上。
_NA_RE = re.compile(r"[^0-9a-z一-鿿]+")


def _norm_name(s) -> str:
    return _NA_RE.sub("", str(s or "").lower())


def wish_owner(*names) -> dict:
    """按「片名 / 原名 / 种子名」反查求片人 —— 播报里要显示「谁求的」。

    返回 {"tg_id": int, "tg_name": str, "keyword": str}；查不到返回 {}。
    匹配规则：把候选名归一化后，看哪条 wishlist 的 keyword/title 是它的子串，
    取「最长的那条」作为命中（"沙丘" 与 "沙丘2" 同时存在时才不会张冠李戴）。
    """
    cands = [n for n in names if n]
    if not cands:
        return {}
    nc = [_norm_name(n) for n in cands if _norm_name(n)]
    if not nc:
        return {}
    try:
        with _engine.connect() as c:
            rows = c.execute(text(
                "SELECT tg_id, tg_name, keyword, title FROM kfc_wishlist "
                "ORDER BY id DESC LIMIT 500")).mappings().all()
    except Exception:
        return {}
    best, blen = None, 0
    for r in rows:
        for col in ("keyword", "title"):
            k = _norm_name(r[col])
            if len(k) < 2:
                continue
            for n in nc:
                if len(n) < 2:
                    continue
                # 正向：求片关键词 ⊂ 候选名（种子名通常比片名长，最常见）
                if k in n:
                    score = len(k)
                # 反向：候选名 ⊂ 关键词（Emby 回写的是「斗罗大陆」，
                #   而求片时填的是「斗罗大陆Ⅱ绝世唐门」）→ 短的那边至少 4 字
                #   或占长的 6 成以上，否则「沙丘」会命中「沙丘之外」这类误判。
                elif n in k and (len(n) >= 4 or len(n) / max(len(k), 1) >= 0.6):
                    score = len(n)
                else:
                    continue
                if score > blen:
                    best, blen = r, score
    if not best:
        return {}
    try:
        tg = int(str(best["tg_id"] or "0"))
    except Exception:
        tg = 0
    if not tg:
        return {}
    return {"tg_id": tg, "tg_name": str(best["tg_name"] or "")[:60],
            "keyword": str(best["keyword"] or "")[:200]}


def my_wishes(tg: int, limit: int = 20) -> list[dict]:
    """我自己的求片（tg_id 匹配），最新的在前"""
    try:
        with _engine.connect() as c:
            rows = c.execute(text(
                "SELECT id, tg_id, tg_name, keyword, title, year, status, created_at "
                "FROM kfc_wishlist WHERE tg_id = :t "
                "ORDER BY id DESC LIMIT :l"),
                {"t": str(tg), "l": int(limit)}).mappings().all()
    except Exception:
        return []
    return [dict(r) for r in rows]


def wish_progress(keywords: list[str]) -> dict:
    """按关键词批量反查在途进度（供「我的求片」展示每一部的阶段）"""
    out: dict[str, dict] = {}
    if not keywords:
        return out
    with _engine.connect() as c:
        for kw in keywords:
            k = str(kw or "").strip()
            if len(k) < 2:
                continue
            rows = c.execute(text(
                "SELECT torrent_name, media_title, year, quality, status, stage, "
                "       progress, speed_bps, eta_sec, dl_size, total_size, qb_state "
                "FROM pt2cloud.pt_uploads "
                "WHERE torrent_name LIKE :k OR media_title LIKE :k "
                "ORDER BY id DESC LIMIT 1"),
                {"k": f"%{k}%"}).mappings().all()
            if rows:
                out[k] = dict(rows[0])
    return out


def dm(s):
    """修复被错误连接写坏的双重编码中文（'ä¸‹è½½' -> '下载'）。

    库里同时存在两种脏数据：
      · latin-1 系（'æ±\\x82ç\\x89\\x87'）—— 直接 encode('latin-1') 就能还原；
      · cp1252 系（'æ±‚ç‰‡'，0x82/0x89/0x87 被映射成 U+201A/U+2030/U+2021）
        —— latin-1 编码会抛错，必须再试 cp1252，否则 origin 永远是乱码，
        播报分流（求片→群 / 订阅→私聊）就会全部判错。
    正常中文两种编码都会抛错，所以只有真正的脏数据会被改写。

    ★ 这是唯一权威实现，inbox.dm() 直接转调这里，别再复制一份。
    """
    if not s or not isinstance(s, str):
        return s or ""
    try:
        s.encode("ascii")
        return s                      # 纯 ASCII 无需处理
    except UnicodeEncodeError:
        pass
    for enc in ("latin-1", "cp1252"):
        try:
            fixed = s.encode(enc, errors="strict").decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed and fixed != s:
            return fixed
    return s


def _today_archived_rows(limit: int = 60) -> list[dict]:
    """今日入库完成的原始行（origin 已解码，供统计与清单共用同一口径）"""
    try:
        with _engine.connect() as c:
            rows = c.execute(text(
                "SELECT media_title, year, quality, total_size, tmdb_id, poster, "
                "       origin, site, updated_at, torrent_name "
                "FROM pt2cloud.pt_uploads "
                "WHERE status = 'archived' AND DATE(updated_at) = CURDATE() "
                "ORDER BY updated_at DESC LIMIT :l"),
                {"l": int(limit)}).mappings().all()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["origin_d"] = dm(d.get("origin") or "")
        d["title_d"] = dm(d.get("media_title") or "")
        out.append(d)
    return out


def today_req_stats() -> dict:
    """今日 PT 日报（群版）：求片数 / 求片入库数 / 在途数

    ★ 用户 2026-10-10 拍板：群版日报的「入库成功」只算**群友求片**，
      不含用户自己在 MP 订阅刷流的量（那部分只在私聊日报里出现）。
    """
    out = {"req": 0, "archived": 0, "doing": 0, "failed": 0}
    try:
        with _engine.connect() as c:
            out["req"] = int(c.execute(text(
                "SELECT COUNT(*) FROM kfc_wishlist WHERE DATE(created_at) = CURDATE()"
            )).scalar() or 0)
            r = c.execute(text(
                "SELECT COUNT(*) FROM pt2cloud.pt_uploads "
                "WHERE status NOT IN ('archived','pruned','skipped','failed')"
            )).scalar()
            out["doing"] = int(r or 0)
            r = c.execute(text(
                "SELECT COUNT(*) FROM pt2cloud.pt_uploads WHERE status = 'failed'"
            )).scalar()
            out["failed"] = int(r or 0)
    except Exception:
        pass
    # 与 today_archived() 同源同口径（pt_uploads.archived + origin='求片'），
    # 保证日报里「求片入库 N 部」和下面列出的条数对得上。
    out["archived"] = sum(1 for r in _today_archived_rows() if r["origin_d"] == "求片")
    return out


# 种子名里的季/集：S01E02 / s1e2 / S01.E02 / S01E102
_EP_RE = re.compile(r"[sS](\d{1,2})[ ._-]*[eE](\d{1,3})")
# 整季打包：S01.Complete / S01 / 第1季 合集
_SEASON_RE = re.compile(r"[sS](\d{1,2})(?!\d)")
_PACK_RE = re.compile(r"(complete|合集|全季|打包|all\.?eps|\bS\d{1,2}\b)", re.I)


def _ep_of(name: str):
    """从种子名/片名里取 (季, 集)；整季包（无单集号）返回 ('S01 全季', None)"""
    s = dm(name or "")
    m = _EP_RE.search(s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    if _PACK_RE.search(s):
        ms = _SEASON_RE.search(s)
        return (f"S{int(ms.group(1)):02d} 全季" if ms else "全季", None)
    return (None, None)


def _ep_ranges(pairs: list) -> str:
    """[(季,集)...] → 'S01E01-S01E03, S01E05-S01E07'（连续集号并成区间）"""
    ps = sorted({p for p in pairs if isinstance(p[0], int)})
    if not ps:
        return ""
    out, i = [], 0
    while i < len(ps):
        s0, e0 = ps[i]
        j = i
        while (j + 1 < len(ps) and ps[j + 1][0] == ps[j][0]
               and ps[j + 1][1] == ps[j][1] + 1):
            j += 1
        s1, e1 = ps[j]
        out.append(f"S{s0:02d}E{e0:02d}" if i == j
                   else f"S{s0:02d}E{e0:02d}-S{s1:02d}E{e1:02d}")
        i = j + 1
    return ", ".join(out)


def today_req_rank(limit: int = 12) -> list[dict]:
    """★ 2026-10-15 用户要求：观看/求片「用户日榜」——按人聚合今日求片+入库。

    返回 [{tg, name, n_req, n_in, titles:[...]}]，按 n_in+n_req 降序。
    只统计群友求片（origin='求片'）；tg 一定拿得到（求片必须登录），
    渲染端用三行标准卡（用户名/UID/Emby用户名，全粗体+超链接）。
    """
    out: list[dict] = []
    try:
        with _engine.connect() as c:
            # 今日求片申请（按人）
            rows = c.execute(text(
                "SELECT tg, IFNULL(MAX(NULLIF(tg_name,'')),'') AS name, "
                "       COUNT(*) AS n "
                "FROM kfc_req_audit "
                "WHERE DATE(created_at) = CURDATE() AND tg > 0 "
                "GROUP BY tg ORDER BY n DESC LIMIT 200")).all()
            req_map = {int(r[0]): {"name": str(r[1] or ""), "n": int(r[2] or 0)}
                       for r in rows}
            # 今日入库（群友求片，按人）
            rows2 = c.execute(text(
                "SELECT tg, IFNULL(MAX(NULLIF(tg_name,'')),'') AS name, "
                "       COUNT(*) AS n "
                "FROM kfc_req_audit "
                "WHERE DATE(decided_at) = CURDATE() AND status='approved' "
                " AND tg > 0 "
                "GROUP BY tg ORDER BY n DESC LIMIT 200")).all()
            in_map = {int(r[0]): {"name": str(r[1] or ""), "n": int(r[2] or 0)}
                      for r in rows2}
    except Exception as e:
        logger.debug(f"today_req_rank 失败: {e}")
        return out
    tgs = set(req_map) | set(in_map)
    for tg in tgs:
        rq = req_map.get(tg) or {"name": "", "n": 0}
        iN = in_map.get(tg) or {"name": "", "n": 0}
        out.append({"tg": tg,
                    "name": rq["name"] or iN["name"] or "",
                    "n_req": rq["n"], "n_in": iN["n"]})
    out.sort(key=lambda x: (-(x["n_in"] * 2 + x["n_req"]), -x["tg"]))
    return out[:limit]


def today_archived(limit: int = 10) -> list[dict]:
    """今日**群友求片**入库成功的片子（日报里逐条列出）

    只取 origin='求片'；同一部剧的多集按 (片名, 年份) 合并成一行 →
    {"media_title","year","n","eps","total_size","poster","tmdb_id"}
      · eps  —— 集数区间串，如 'S01E01-S01E03, S01E05-S01E07'；
                整季包是 'S01 全季'；解析不到为空（渲染端降级显示 ×N）
      · n    —— 文件（集）数；total_size 为累加体积
    用户 2026-10-10 拍板：不要只写 ×3，要显示第几集到第几集。
    """
    rows = [r for r in _today_archived_rows(80) if r["origin_d"] == "求片"]
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for r in rows:
        key = (r["title_d"].strip(), str(r.get("year") or "").strip())
        if key not in merged:
            merged[key] = {
                "media_title": r["title_d"],
                "year": r.get("year") or "",
                "n": 0,
                "eps": "",
                "_eps": [],
                "_pack": set(),
                "total_size": 0,
                "poster": r.get("poster") or "",
                "tmdb_id": r.get("tmdb_id") or "",
                "quality": dm(r.get("quality") or ""),
            }
            order.append(key)
        g = merged[key]
        g["n"] += 1
        s, e = _ep_of(r.get("torrent_name") or r["title_d"])
        if isinstance(s, int):
            g["_eps"].append((s, e))
        elif s:
            g["_pack"].add(s)
        try:
            g["total_size"] += int(r.get("total_size") or 0)
        except (TypeError, ValueError):
            pass
    out = []
    for k in order:
        g = merged[k]
        rng = _ep_ranges(g.pop("_eps"))
        pack = ", ".join(sorted(g.pop("_pack")))
        # 既有单集又有整季包 → 单集区间在前，整季包在后
        g["eps"] = "，".join(x for x in (rng, pack) if x)
        out.append(g)
    return out[:int(limit)]


def pt_stats() -> dict:
    """pt2cloud 各状态计数（/ptstatus 展示链路进度）"""
    with _engine.connect() as c:
        rows = c.execute(text(
            "SELECT status, COUNT(*) AS n FROM pt2cloud.pt_uploads "
            "GROUP BY status")).mappings().all()
    return {r["status"]: int(r["n"]) for r in rows}


# ════════════════════════════════════════════════════════════════════
#  管理员与权限（kfc_admin）
#
#  tg      —— 管理员 TG ID（owner 不入表，owner 恒有全部权限）
#  grants  —— 逗号分隔的「模块 key」，对应 perm.MODULES。
#             ★ 没勾选的模块，该管理员在私聊菜单里看不到、也执行不了。
# ════════════════════════════════════════════════════════════════════
def admins() -> list[dict]:
    """全部管理员（含授权模块）"""
    try:
        with _engine.connect() as c:
            rows = c.execute(text(
                "SELECT tg, grants, note, created_at FROM kfc_admin "
                "ORDER BY tg")).mappings().all()
    except Exception:
        return []
    return [dict(r) for r in rows]


def add_admin(tg: int, note: str = "") -> bool:
    try:
        with _engine.begin() as c:
            c.execute(text(
                "INSERT INTO kfc_admin (tg, grants, note) VALUES (:t,'',:n) "
                "ON DUPLICATE KEY UPDATE note = :n"),
                {"t": int(tg), "n": (note or "")[:64]})
        return True
    except Exception:
        return False


def del_admin(tg: int) -> bool:
    try:
        with _engine.begin() as c:
            c.execute(text("DELETE FROM kfc_admin WHERE tg = :t"), {"t": int(tg)})
        return True
    except Exception:
        return False


def set_grants(tg: int, mods: list[str]) -> bool:
    """整体覆盖某管理员的授权模块"""
    g = ",".join(sorted({str(m).strip() for m in (mods or []) if str(m).strip()}))[:512]
    try:
        with _engine.begin() as c:
            c.execute(text("UPDATE kfc_admin SET grants = :g WHERE tg = :t"),
                      {"g": g, "t": int(tg)})
        return True
    except Exception:
        return False


def grants_of(tg: int) -> set[str]:
    try:
        with _engine.connect() as c:
            r = c.execute(text("SELECT grants FROM kfc_admin WHERE tg = :t"),
                          {"t": int(tg)}).scalar()
    except Exception:
        return set()
    return {x.strip() for x in str(r or "").split(",") if x.strip()}


def mark_notify_sent(items) -> None:
    """标记已播报，并把群内 chat_id / msg_id 回填（供后续原地刷新进度条）

    items: [id, ...] 或 [(id, chat_id, msg_id), ...]
    """
    if not items:
        return
    with _engine.begin() as c:
        for it in items:
            if isinstance(it, (tuple, list)):
                i, chat, msg = int(it[0]), str(it[1] or ""), int(it[2] or 0)
                c.execute(text(
                    "UPDATE kfc_pt_notify SET status = 'sent', sent_at = NOW(), "
                    "chat_id = :c, msg_id = :m WHERE id = :i"),
                    {"i": i, "c": chat, "m": msg})
            else:
                c.execute(text(
                    "UPDATE kfc_pt_notify SET status = 'sent', sent_at = NOW() "
                    "WHERE id = :i"), {"i": int(it)})


# ============================================================================
#  Telegram 账号绑定（Emby 账号 + 密码验证）—— 状态机 + 加密存储
#
#  状态：none 未开始 / pending 流程中 / failed 验证失败 /
#        bound 已绑定 / disabled 已禁用 / unbound 已解绑
#  密码：MySQL AES_ENCRYPT 加密（密钥 /app/keepalive/ka.key，权限 600），
#        库里只存 HEX 密文，任何途径看不到明文。
# ============================================================================
import os as _os
import time as _time


def _ka_key() -> str:
    """读取（不存在则生成）AES 密钥。密钥文件只在 NAS 宿主，不进仓库。"""
    d = _os.environ.get("KA_DIR", "/app/keepalive")
    p = _os.path.join(d, "ka.key")
    try:
        _os.makedirs(d, exist_ok=True)
        if _os.path.exists(p) and _os.path.getsize(p) >= 16:
            return open(p, encoding="utf-8").read().strip()
        import secrets as _sec
        k = _sec.token_hex(24)                     # 48 hex 字符
        with open(p, "w", encoding="utf-8") as f:
            f.write(k)
        _os.chmod(p, 0o600)
        return k
    except Exception as e:
        logger.warning(f"AES 密钥文件异常，用进程内兜底钥匙: {e}")
        return "kfc-keepalive-fallback-key-do-not-use"


def bind_state(tg: int) -> dict | None:
    with _engine.connect() as c:
        r = c.execute(text(
            "SELECT * FROM kfc_emby_bind WHERE tg = :tg"), {"tg": tg}).first()
    return dict(r._mapping) if r else None


def bind_touch(tg: int, tg_name: str = "", tg_username: str = "") -> None:
    with _engine.begin() as c:
        c.execute(text(
            "INSERT INTO kfc_emby_bind (tg, tg_name, tg_username, status, updated_ts) "
            "VALUES (:tg, :n, :u, 'pending', :ts) "
            "ON DUPLICATE KEY UPDATE tg_name = VALUES(tg_name), "
            "tg_username = VALUES(tg_username), status = 'pending', "
            "updated_ts = VALUES(updated_ts)"),
            {"tg": tg, "n": tg_name[:64], "u": tg_username[:64],
             "ts": int(_time.time())})


def bind_ok(tg: int, emby_name: str, emby_uid: str, password: str) -> None:
    """验证通过：写绑定 + 密码密文（AES），保持与 kfc_bind 一致供榜单用"""
    k = _ka_key()
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_emby_bind SET status = 'bound', emby_name = :n, "
            "emby_uid = :uid, pw_hex = HEX(AES_ENCRYPT(:pw, :k)), "
            "fail_count = 0, bound_ts = :ts, updated_ts = :ts WHERE tg = :tg"),
            {"tg": tg, "n": emby_name[:64], "uid": str(emby_uid)[:32],
             "pw": password, "k": k, "ts": int(_time.time())})
        c.execute(text(
            "INSERT INTO kfc_bind (tg, emby_name) VALUES (:tg, :n) "
            "ON DUPLICATE KEY UPDATE emby_name = VALUES(emby_name), ts = NOW()"),
            {"tg": tg, "n": emby_name[:64]})
        # ★ 2026-11-03 回填共享台账 embyboss.emby：
        #   修复「kfc_emby_bind 已 bound 但共享台账 embyid 为空 → 续期被双 bot 拒」。
        #   这里把真实 Emby 凭据写进共享台账，后续续期/查账都不再缺 embyid。
        c.execute(text(
            f"INSERT INTO `{S.SHARED_DB}`.emby "
            f"(tg, embyid, name, pwd, pwd2, lv, cr, ex, us, iv) "
            f"VALUES (:tg, :eid, :n, :p, :p, 'b', NOW(), NULL, 0, 0) "
            f"ON DUPLICATE KEY UPDATE embyid=VALUES(embyid), name=VALUES(name), "
            f"pwd=VALUES(pwd), pwd2=VALUES(pwd2), lv=IF(lv='c','c','b')"),
            {"tg": tg, "eid": str(emby_uid)[:32], "n": emby_name[:64],
             "p": password})


def bind_fail(tg: int) -> int:
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_emby_bind SET status = 'failed', "
            "fail_count = fail_count + 1, last_fail_ts = :ts, updated_ts = :ts "
            "WHERE tg = :tg"),
            {"tg": tg, "ts": int(_time.time())})
        r = c.execute(text(
            "SELECT fail_count FROM kfc_emby_bind WHERE tg = :tg"),
            {"tg": tg}).first()
    return int(r[0]) if r else 0


def bind_unbind(tg: int) -> None:
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_emby_bind SET status = 'unbound', emby_name = '', "
            "pw_hex = '', bound_ts = 0, updated_ts = :ts WHERE tg = :tg"),
            {"tg": tg, "ts": int(_time.time())})
        c.execute(text("DELETE FROM kfc_bind WHERE tg = :tg"), {"tg": tg})


def bind_disable(tg: int, on: bool = True) -> None:
    with _engine.begin() as c:
        c.execute(text(
            "UPDATE kfc_emby_bind SET status = :st, updated_ts = :ts WHERE tg = :tg"),
            {"tg": tg, "st": "disabled" if on else "bound", "ts": int(_time.time())})


def bind_decrypt_pw(tg: int) -> str | None:
    """（仅排查/迁移用）解回明文。正常业务链路上没有任何地方调用它。"""
    k = _ka_key()
    with _engine.connect() as c:
        r = c.execute(text(
            "SELECT CAST(AES_DECRYPT(UNHEX(pw_hex), :k) AS CHAR) AS pw "
            "FROM kfc_emby_bind WHERE tg = :tg AND pw_hex <> ''"),
            {"tg": tg, "k": k}).first()
    return (r[0] if r else None) or None


def bind_counts() -> dict:
    with _engine.connect() as c:
        rows = c.execute(text(
            "SELECT status, COUNT(*) n FROM kfc_emby_bind GROUP BY status")).all()
    return {r[0]: int(r[1]) for r in rows}


def is_bound(tg: int) -> bool:
    with _engine.connect() as c:
        r = c.execute(text(
            "SELECT 1 FROM kfc_emby_bind WHERE tg = :tg AND status = 'bound'"),
            {"tg": tg}).first()
    return bool(r)
