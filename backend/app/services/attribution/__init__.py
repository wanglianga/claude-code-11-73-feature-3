"""会员门店归属历史与权益成本归因领域（事件账的可重建投影）。

子模块：
- 本文件：事件类型常量、可解释基权、服务关系/组织沿革/预算/成本投影应用器；
- org：组织沿革图任意双时点解析（含并购继承）；
- calculator：归属计算器（单店最高证据 / 可解释权重分摊，冲突待裁定）；
- cost：成本归因器（预算周期结算单，未分配范围与小数残差）；
- settlement：结算差异发布器（保留原结算，签发调整版本）。
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models

# 服务关系事件类型
SERVICE_EVENT_TYPES = (
    "service_choice",        # 会员主动选择服务门店
    "service_prescription",  # 处方服务（长期咨询/处方）
    "service_visit",         # 到店消费
    "service_online",        # 线上履约
    "service_confirmation",  # 门店确认
    "service_revoke",        # 服务证据撤销
)

# 组织沿革事件类型
ORG_EVENT_TYPES = (
    "store_open",
    "store_close",
    "franchise_join",
    "franchise_exit",
    "region_adjust",
    "store_merge",
    "store_split",
)

# 关系类型 -> 可解释基权（五类证据基权之和 = 1.00）
RELATION_WEIGHTS = {
    "member_choice": 0.20,       # 会员选择
    "prescription": 0.30,        # 处方/咨询服务
    "instore_visit": 0.30,       # 到店消费
    "online_fulfillment": 0.15,  # 线上履约
    "store_confirmation": 0.05,  # 门店确认
}

RELATION_TYPE_BY_EVENT = {
    "service_choice": "member_choice",
    "service_prescription": "prescription",
    "service_visit": "instore_visit",
    "service_online": "online_fulfillment",
    "service_confirmation": "store_confirmation",
}


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _upsert_store(db: Session, store_key: str, ev: models.Event, name: str | None = None) -> None:
    row = db.get(models.Store, store_key)
    if row is None:
        db.add(models.Store(store_key=store_key, display_name=name, first_seen_event_id=ev.id))
    elif name and not row.display_name:
        row.display_name = name


def apply_service_event(db: Session, ev: models.Event) -> None:
    """服务关系账投影：追加证据行；撤销只闭合旧行，不删除。"""
    p = ev.payload
    if ev.event_type == "service_revoke":
        target_id = p.get("target_event_id")
        if target_id is not None:
            target = db.execute(
                select(models.ServiceRelation).where(
                    models.ServiceRelation.event_id == target_id,
                    models.ServiceRelation.status == "active",
                )
            ).scalar_one_or_none()
            if target is not None:
                target.status = "revoked"
                target.revoke_event_id = ev.id
                target.revoked_at = ev.ingested_at
        return

    relation_type = p.get("relation_type") or RELATION_TYPE_BY_EVENT[ev.event_type]
    grade = int(p["evidence_grade"])
    db.add(
        models.ServiceRelation(
            event_id=ev.id,
            member_key=p["member_key"],
            store_key=p["store_key"],
            relation_type=relation_type,
            evidence_grade=grade,
            biz_time=ev.biz_time,
            ingested_at=ev.ingested_at,
            weight_hint=p.get("weight_hint", RELATION_WEIGHTS[relation_type]),
            status="active",
            detail={k: v for k, v in p.items()
                    if k not in ("member_key", "store_key", "relation_type", "evidence_grade", "weight_hint")},
        )
    )
    _upsert_store(db, p["store_key"], ev)
    if db.get(models.MemberDirectory, p["member_key"]) is None:
        db.add(models.MemberDirectory(member_key=p["member_key"],
                                      display_name=p.get("member_name"), first_seen_event_id=ev.id))


def _active_org_interval(db: Session, store_key: str) -> models.OrgInterval | None:
    return db.execute(
        select(models.OrgInterval).where(
            models.OrgInterval.store_key == store_key,
            models.OrgInterval.sys_to.is_(None),
        ).order_by(models.OrgInterval.id.desc())
    ).scalars().first()


def apply_org_event(db: Session, ev: models.Event) -> None:
    """组织沿革图投影：门店开闭/加盟/区域/合并拆分的双时间有效区间。

    状态变更闭合当前区间（biz_to/sys_to 双闭合）并开新区间；合并把被合并门店
    区间置 merged 并指向存续门店。所有闭合只写端点，历史行永不删除。
    """
    p = ev.payload
    store = p["store_key"]
    _upsert_store(db, store, ev, p.get("store_name"))
    if p.get("target_store"):
        _upsert_store(db, p["target_store"], ev)

    if ev.event_type == "store_open":
        db.add(_new_interval(ev, store, "open", p, status="active"))
        return

    if ev.event_type == "store_split":
        # 拆分：新门店开区间，指向前身；前身默认存续，payload 要求时闭合
        db.add(_new_interval(ev, store, "split", p, status="active"))
        if p.get("close_target"):
            pred = _active_org_interval(db, p["target_store"])
            if pred is not None:
                pred.biz_to, pred.sys_to, pred.status = ev.biz_time, ev.ingested_at, "split"
        return

    if ev.event_type == "store_merge":
        # 被合并门店区间在合并业务日闭合，状态 merged，指向存续门店
        merged = _active_org_interval(db, p["target_store"])
        if merged is not None:
            merged.biz_to, merged.sys_to, merged.status = ev.biz_time, ev.ingested_at, "merged"
            merged.target_store = store  # 继承指针：服务日晚于合并日时沿此找到存续门店
        db.add(_new_interval(ev, store, "merge", p, status="active"))
        return

    if ev.event_type == "store_close":
        cur = _active_org_interval(db, store)
        if cur is not None:
            cur.biz_to, cur.sys_to, cur.status = ev.biz_time, ev.ingested_at, "closed"
        db.add(_new_interval(ev, store, "close", p, status="closed"))
        return

    # franchise_join / franchise_exit / region_adjust：闭合旧区间，开新区间
    change = ev.event_type
    cur = _active_org_interval(db, store)
    if cur is not None:
        cur.biz_to = ev.biz_time
        cur.sys_to = ev.ingested_at
    db.add(_new_interval(ev, store, change, p, status="active"))


def _new_interval(db_ev: models.Event, store: str, change: str, p: dict, *, status: str) -> models.OrgInterval:
    return models.OrgInterval(
        event_id=db_ev.id,
        store_key=store,
        change=change,
        owner_key=p.get("owner_key"),
        region_code=p.get("region_code"),
        target_store=p.get("target_store"),
        biz_from=db_ev.biz_time,
        sys_from=db_ev.ingested_at,
        status=status,
        reason=p.get("reason"),
    )


def apply_budget_event(db: Session, ev: models.Event) -> None:
    p = ev.payload
    row = db.execute(
        select(models.BudgetPeriod).where(models.BudgetPeriod.period_key == p["period_key"])
    ).scalar_one_or_none()
    if row is None:
        db.add(
            models.BudgetPeriod(
                period_key=p["period_key"],
                biz_from=parse_dt(p["biz_from"]),
                biz_to=parse_dt(p["biz_to"]),
                allow_apportion=bool(p.get("allow_apportion", False)),
                precision_cents=int(p.get("precision_cents", 2)),
                coverage_threshold=p.get("coverage_threshold", 0.60),
            )
        )
    else:
        row.allow_apportion = bool(p.get("allow_apportion", row.allow_apportion))
        row.precision_cents = int(p.get("precision_cents", row.precision_cents))
        row.coverage_threshold = p.get("coverage_threshold", row.coverage_threshold)


def apply_benefit_cost_event(db: Session, ev: models.Event) -> None:
    p = ev.payload
    db.add(
        models.BenefitCost(
            period_key=p["period_key"],
            member_key=p["member_key"],
            benefit_type=p["benefit_type"],
            amount=p["amount"],
            rule_version_tag=p["rule_version_tag"],
            source_event_id=p.get("source_event_id"),
            created_at=ev.ingested_at,
        )
    )
