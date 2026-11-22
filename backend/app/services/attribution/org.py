"""组织沿革图：双时间有效区间解析。

解析语义（确定性）：
- 对门店 s、业务时点 T_b、系统时点 T_s，取满足
  sys_from <= T_s 且 biz_from <= T_b，且不存在“在 T_s 前已可见、
  且 biz_to <= T_b”的闭合 的最新一条区间（按 id 最大）。
- 并购（store_merge）在被合并门店的合并业务日生效：服务发生日早于合并日时
  归属仍落在原门店区间；发生日在合并日后，沿 merge 行的 target_store 继承到存续门店。
  因此归属必须按服务发生日拆分，禁止以月末组织树回填。
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models


def intervals_visible(db: Session, sys_t: datetime) -> list[models.OrgInterval]:
    rows = db.execute(select(models.OrgInterval)).scalars().all()
    return [r for r in rows if r.sys_from <= sys_t]


def interval_at(db: Session, store_key: str, biz_t: datetime, sys_t: datetime) -> models.OrgInterval | None:
    """门店在 (T_b, T_s) 口径下有效的最新区间。闭合在 T_s 之后才可见 => 当时仍有效。"""
    cands = []
    for r in intervals_visible(db, sys_t):
        if r.store_key != store_key:
            continue
        if r.biz_from > biz_t:
            continue
        if (
            r.sys_to is not None
            and r.sys_to <= sys_t
            and r.biz_to is not None
            and r.biz_to <= biz_t
        ):
            continue
        cands.append(r)
    return max(cands, key=lambda r: r.id) if cands else None


def _latest_visible_interval(db: Session, store_key: str, sys_t: datetime) -> models.OrgInterval | None:
    """门店在 T_s 前可见的最新区间（不论业务区间是否已闭合）。"""
    cands = [r for r in intervals_visible(db, sys_t) if r.store_key == store_key]
    return max(cands, key=lambda r: r.id) if cands else None


def resolve_store(db: Session, store_key: str, biz_t: datetime, sys_t: datetime) -> dict:
    """把服务发生门店解析到服务发生日的有效组织节点（含并购继承）。

    返回 {store_key, region_code, owner_key, change, status, inherited_from}。
    门店在服务日已关闭/被合并时，沿 merge 链找到当时的存续门店；
    链上每一跳都记录在 trail 中，供回查解释“为什么归到这家门店”。
    """
    trail: list[dict] = []
    cur = store_key
    for _ in range(16):  # 并购链深度保护
        iv = interval_at(db, cur, biz_t, sys_t)
        if iv is None:
            # 服务日没有有效区间：查最新可见区间，判断是否因并购/关闭而闭合
            latest = _latest_visible_interval(db, cur, sys_t)
            if latest is None:
                return {"store_key": cur, "region_code": None, "owner_key": None,
                        "change": "unknown", "status": "unknown", "trail": trail}
            trail.append(_trail_node(latest))
            if latest.status == "merged" and latest.target_store and latest.biz_to and biz_t >= latest.biz_to:
                cur = latest.target_store  # 并购在服务日之前生效：继承到存续门店
                continue
            return {"store_key": cur, "region_code": latest.region_code,
                    "owner_key": latest.owner_key, "change": latest.change,
                    "status": latest.status, "trail": trail}
        trail.append(_trail_node(iv))
        # 服务日落在被合并之后：继承到存续门店
        if iv.status == "merged" and iv.target_store and iv.biz_to and biz_t >= iv.biz_to:
            cur = iv.target_store
            continue
        return {
            "store_key": cur,
            "region_code": iv.region_code,
            "owner_key": iv.owner_key,
            "change": iv.change,
            "status": iv.status,
            "trail": trail,
        }
    return {"store_key": cur, "region_code": None, "owner_key": None,
            "change": "chain_too_deep", "status": "unknown", "trail": trail}


def _trail_node(iv: models.OrgInterval) -> dict:
    return {
        "store_key": iv.store_key,
        "change": iv.change,
        "status": iv.status,
        "region_code": iv.region_code,
        "owner_key": iv.owner_key,
        "biz_from": iv.biz_from.isoformat(),
        "biz_to": iv.biz_to.isoformat() if iv.biz_to else None,
        "event_id": iv.event_id,
    }


def org_graph_version(db: Session, sys_t: datetime) -> int:
    """组织沿革图冻结版本：T_s 前可见的最大组织事件 id。"""
    ids = [r.event_id for r in intervals_visible(db, sys_t)]
    return max(ids, default=0)


def stores_active_in(db: Session, biz_from: datetime, biz_to: datetime, sys_t: datetime) -> list[str]:
    """预算期 [biz_from, biz_to) 内任一时点有效过的门店集合（去重）。"""
    out = set()
    for r in intervals_visible(db, sys_t):
        if r.biz_from >= biz_to:
            continue
        if r.biz_to is not None and r.biz_to <= biz_from:
            continue
        out.add(r.store_key)
    return sorted(out)
