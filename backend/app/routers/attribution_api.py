"""会员门店归属与权益成本归因 API。"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..security import current_user
from ..services import ledger
from ..services.attribution import (
    ORG_EVENT_TYPES,
    SERVICE_EVENT_TYPES,
    calculator,
    cost as cost_svc,
    org as org_svc,
    settlement as settle_svc,
)

router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


def parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fnum(v) -> float:
    return float(v) if v is not None else None


# ---------------- 门店目录 ----------------
@router.get("/stores")
def list_stores(db: Session = Depends(get_db)):
    rows = db.execute(select(models.Store).order_by(models.Store.store_key)).scalars().all()
    return [{"store_key": s.store_key, "display_name": s.display_name,
             "first_seen_event_id": s.first_seen_event_id} for s in rows]


# ---------------- 服务关系账 ----------------
class ServiceIn(BaseModel):
    event_type: str
    member_key: str
    store_key: str
    evidence_grade: int
    biz_time: str
    relation_type: str | None = None
    detail: dict = {}
    idempotency_key: str | None = None
    ingested_at: str | None = None


@router.post("/service-relations", status_code=201)
def post_service_relation(body: ServiceIn, db: Session = Depends(get_db)):
    if body.event_type not in SERVICE_EVENT_TYPES or body.event_type == "service_revoke":
        raise HTTPException(400, "event_type 必须是五类服务证据之一（撤销请用 /service-relations/revoke）")
    if not 1 <= body.evidence_grade <= 5:
        raise HTTPException(400, "证据等级取 1..5")
    payload = {"member_key": body.member_key, "store_key": body.store_key,
               "evidence_grade": body.evidence_grade, **body.detail}
    if body.relation_type:
        payload["relation_type"] = body.relation_type
    ev, created = ledger.ingest_event(
        db, source="service_desk", event_type=body.event_type,
        biz_time=parse_dt(body.biz_time),
        ingested_at=parse_dt(body.ingested_at) if body.ingested_at else None,
        payload=payload, idempotency_key=body.idempotency_key,
    )
    return {"created": created, "event_id": ev.id}


class RevokeIn(BaseModel):
    target_event_id: int
    biz_time: str
    reason: str | None = None
    idempotency_key: str | None = None
    ingested_at: str | None = None


@router.post("/service-relations/revoke", status_code=201)
def revoke_service_relation(body: RevokeIn, db: Session = Depends(get_db)):
    target = db.execute(
        select(models.ServiceRelation).where(models.ServiceRelation.event_id == body.target_event_id)
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "目标服务证据不存在")
    ev, created = ledger.ingest_event(
        db, source="service_desk", event_type="service_revoke",
        biz_time=parse_dt(body.biz_time),
        ingested_at=parse_dt(body.ingested_at) if body.ingested_at else None,
        payload={"target_event_id": body.target_event_id, "member_key": target.member_key,
                 "store_key": target.store_key, "reason": body.reason or "服务证据撤销"},
        idempotency_key=body.idempotency_key,
    )
    return {"created": created, "event_id": ev.id, "revoked_relation_id": target.id}


@router.get("/service-relations")
def list_service_relations(member: str | None = None, store: str | None = None,
                           status: str | None = None, db: Session = Depends(get_db)):
    q = select(models.ServiceRelation).order_by(models.ServiceRelation.id)
    rows = db.execute(q).scalars().all()
    out = []
    for r in rows:
        if member and r.member_key != member:
            continue
        if store and r.store_key != store:
            continue
        if status and r.status != status:
            continue
        out.append({
            "id": r.id, "event_id": r.event_id, "member_key": r.member_key,
            "store_key": r.store_key, "relation_type": r.relation_type,
            "evidence_grade": r.evidence_grade, "biz_time": r.biz_time.isoformat(),
            "ingested_at": r.ingested_at.isoformat(), "weight_hint": fnum(r.weight_hint),
            "status": r.status, "revoke_event_id": r.revoke_event_id,
            "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None, "detail": r.detail,
        })
    return out


# ---------------- 组织沿革图 ----------------
class OrgIn(BaseModel):
    event_type: str
    store_key: str
    biz_time: str
    owner_key: str | None = None
    region_code: str | None = None
    target_store: str | None = None
    store_name: str | None = None
    reason: str | None = None
    idempotency_key: str | None = None
    ingested_at: str | None = None


@router.post("/org/events", status_code=201)
def post_org_event(body: OrgIn, db: Session = Depends(get_db)):
    if body.event_type not in ORG_EVENT_TYPES:
        raise HTTPException(400, f"event_type 必须是 {ORG_EVENT_TYPES} 之一")
    payload = {"store_key": body.store_key}
    for k in ("owner_key", "region_code", "target_store", "store_name", "reason"):
        v = getattr(body, k)
        if v is not None:
            payload[k] = v
    ev, created = ledger.ingest_event(
        db, source="org_admin", event_type=body.event_type,
        biz_time=parse_dt(body.biz_time),
        ingested_at=parse_dt(body.ingested_at) if body.ingested_at else None,
        payload=payload, idempotency_key=body.idempotency_key,
    )
    return {"created": created, "event_id": ev.id}


@router.get("/org/intervals")
def list_org_intervals(store: str | None = None, db: Session = Depends(get_db)):
    rows = db.execute(select(models.OrgInterval).order_by(models.OrgInterval.id)).scalars().all()
    return [
        {
            "id": r.id, "event_id": r.event_id, "store_key": r.store_key, "change": r.change,
            "owner_key": r.owner_key, "region_code": r.region_code, "target_store": r.target_store,
            "biz_from": r.biz_from.isoformat(),
            "biz_to": r.biz_to.isoformat() if r.biz_to else None,
            "sys_from": r.sys_from.isoformat(),
            "sys_to": r.sys_to.isoformat() if r.sys_to else None,
            "status": r.status, "reason": r.reason,
        }
        for r in rows if not store or r.store_key == store
    ]


class ResolveIn(BaseModel):
    store_key: str
    biz_time: str
    sys_time: str | None = None


@router.post("/org/resolve")
def resolve_org(body: ResolveIn, db: Session = Depends(get_db)):
    sys_t = parse_dt(body.sys_time) if body.sys_time else datetime.now(timezone.utc)
    return org_svc.resolve_store(db, body.store_key, parse_dt(body.biz_time), sys_t)


# ---------------- 预算周期 / 权益成本 ----------------
class BudgetIn(BaseModel):
    period_key: str
    biz_from: str
    biz_to: str
    allow_apportion: bool = False
    precision_cents: int = 2
    coverage_threshold: float = 0.60


@router.post("/budget-periods", status_code=201)
def post_budget(body: BudgetIn, db: Session = Depends(get_db)):
    ev, _ = ledger.ingest_event(
        db, source="budget_admin", event_type="budget_period_define",
        biz_time=parse_dt(body.biz_from),
        payload={"period_key": body.period_key, "biz_from": body.biz_from, "biz_to": body.biz_to,
                 "allow_apportion": body.allow_apportion, "precision_cents": body.precision_cents,
                 "coverage_threshold": body.coverage_threshold},
        idempotency_key=f"budget-{body.period_key}",
    )
    row = db.execute(
        select(models.BudgetPeriod).where(models.BudgetPeriod.period_key == body.period_key)
    ).scalar_one()
    return budget_json(row)


@router.get("/budget-periods")
def list_budgets(db: Session = Depends(get_db)):
    rows = db.execute(select(models.BudgetPeriod).order_by(models.BudgetPeriod.period_key)).scalars().all()
    return [budget_json(b) for b in rows]


def budget_json(b: models.BudgetPeriod) -> dict:
    return {
        "period_key": b.period_key, "biz_from": b.biz_from.isoformat(),
        "biz_to": b.biz_to.isoformat(), "allow_apportion": b.allow_apportion,
        "precision_cents": b.precision_cents, "coverage_threshold": fnum(b.coverage_threshold),
        "status": b.status, "locked_at": b.locked_at.isoformat() if b.locked_at else None,
    }


@router.post("/budget-periods/{key}/lock")
def lock_budget(key: str, db: Session = Depends(get_db)):
    b = db.execute(select(models.BudgetPeriod).where(models.BudgetPeriod.period_key == key)).scalar_one_or_none()
    if not b:
        raise HTTPException(404, "预算周期不存在")
    b.status = "locked"
    b.locked_at = datetime.now(timezone.utc)
    db.commit()
    return budget_json(b)


class CostIn(BaseModel):
    period_key: str
    member_key: str
    benefit_type: str  # tier | points | birthday_coupon
    amount: float
    rule_version_tag: str
    source_event_id: int | None = None
    biz_time: str
    idempotency_key: str | None = None
    ingested_at: str | None = None


@router.post("/benefit-costs", status_code=201)
def post_cost(body: CostIn, db: Session = Depends(get_db)):
    ev, created = ledger.ingest_event(
        db, source="benefit_admin", event_type="benefit_cost",
        biz_time=parse_dt(body.biz_time),
        ingested_at=parse_dt(body.ingested_at) if body.ingested_at else None,
        payload={"period_key": body.period_key, "member_key": body.member_key,
                 "benefit_type": body.benefit_type, "amount": body.amount,
                 "rule_version_tag": body.rule_version_tag, "source_event_id": body.source_event_id},
        idempotency_key=body.idempotency_key,
    )
    return {"created": created, "event_id": ev.id}


@router.get("/benefit-costs")
def list_costs(period: str | None = None, member: str | None = None, db: Session = Depends(get_db)):
    rows = db.execute(select(models.BenefitCost).order_by(models.BenefitCost.id)).scalars().all()
    return [
        {"id": c.id, "period_key": c.period_key, "member_key": c.member_key,
         "benefit_type": c.benefit_type, "amount": fnum(c.amount),
         "rule_version_tag": c.rule_version_tag, "source_event_id": c.source_event_id,
         "created_at": c.created_at.isoformat()}
        for c in rows
        if (not period or c.period_key == period) and (not member or c.member_key == member)
    ]


# ---------------- 归属计算器（试算，不落库） ----------------
class PreviewIn(BaseModel):
    member_key: str
    period_key: str
    sys_time: str | None = None
    strategy: str | None = None  # single_store | apportion | None(自动)


@router.post("/attribution/preview")
def preview_attribution(body: PreviewIn, db: Session = Depends(get_db)):
    budget = db.execute(
        select(models.BudgetPeriod).where(models.BudgetPeriod.period_key == body.period_key)
    ).scalar_one_or_none()
    if not budget:
        raise HTTPException(404, "预算周期不存在")
    sys_t = parse_dt(body.sys_time) if body.sys_time else datetime.now(timezone.utc)
    return calculator.calculate(db, body.member_key, budget, sys_t, body.strategy)


# ---------------- 成本归因器（结算单） ----------------
class RunIn(BaseModel):
    period_key: str
    sys_time: str | None = None
    strategy: str | None = None
    lock: bool = False
    note: str | None = None


@router.post("/attribution/runs", status_code=201)
def create_run(body: RunIn, db: Session = Depends(get_db)):
    sys_t = parse_dt(body.sys_time) if body.sys_time else datetime.now(timezone.utc)
    try:
        run = cost_svc.settle_period(db, body.period_key, sys_t, body.strategy,
                                     lock=body.lock, note=body.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return run_json(run)


def run_json(r: models.AttributionRun) -> dict:
    return {
        "id": r.id, "period_key": r.period_key, "strategy": r.strategy,
        "sys_as_of": r.sys_as_of.isoformat(), "org_graph_version": r.org_graph_version,
        "rule_set_version": r.rule_set_version, "watermark_event_id": r.watermark_event_id,
        "total_cost": fnum(r.total_cost), "allocated_cost": fnum(r.allocated_cost),
        "unallocated_cost": fnum(r.unallocated_cost), "status": r.status,
        "created_at": r.created_at.isoformat(), "note": r.note,
    }


def line_json(ln: models.AttributionLine) -> dict:
    return {
        "id": ln.id, "run_id": ln.run_id, "period_key": ln.period_key,
        "member_key": ln.member_key, "benefit_type": ln.benefit_type, "cost_id": ln.cost_id,
        "store_key": ln.store_key, "region_code": ln.region_code,
        "rule_version_tag": ln.rule_version_tag, "amount": fnum(ln.amount),
        "share": fnum(ln.share), "status": ln.status,
        "evidence_event_ids": ln.evidence_event_ids, "basis": ln.basis, "reason": ln.reason,
    }


@router.get("/attribution/runs")
def list_runs(period: str | None = None, db: Session = Depends(get_db)):
    rows = db.execute(select(models.AttributionRun).order_by(models.AttributionRun.id)).scalars().all()
    return [run_json(r) for r in rows if not period or r.period_key == period]


@router.get("/attribution/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.AttributionRun, run_id)
    if not run:
        raise HTTPException(404, "结算单不存在")
    lines = db.execute(
        select(models.AttributionLine).where(models.AttributionLine.run_id == run_id)
        .order_by(models.AttributionLine.id)
    ).scalars().all()
    return run_json(run) | {"lines": [line_json(x) for x in lines]}


@router.get("/attribution/runs/{run_id}/lines")
def run_lines(run_id: int, store: str | None = None, status: str | None = None,
              member: str | None = None, db: Session = Depends(get_db)):
    q = select(models.AttributionLine).where(models.AttributionLine.run_id == run_id)
    rows = db.execute(q.order_by(models.AttributionLine.id)).scalars().all()
    out = []
    for ln in rows:
        if store and ln.store_key != store:
            continue
        if status and ln.status != status:
            continue
        if member and ln.member_key != member:
            continue
        out.append(line_json(ln))
    return out


# ---------------- 回查链路：门店成本 -> 服务关系/组织版本/归属策略/预算版本 ----------------
@router.get("/attribution/lines/{line_id}/trace")
def trace_line(line_id: int, db: Session = Depends(get_db)):
    ln = db.get(models.AttributionLine, line_id)
    if not ln:
        raise HTTPException(404, "归属行不存在")
    run = db.get(models.AttributionRun, ln.run_id)
    budget = db.execute(
        select(models.BudgetPeriod).where(models.BudgetPeriod.period_key == run.period_key)
    ).scalar_one()
    cost = db.get(models.BenefitCost, ln.cost_id)
    evidence_ids = ln.evidence_event_ids or []
    relations = db.execute(
        select(models.ServiceRelation).where(models.ServiceRelation.event_id.in_(evidence_ids or [-1]))
    ).scalars().all() if evidence_ids else []
    # 组织版本：结算单冻结水位以内、与证据门店相关的组织区间
    org_rows = db.execute(select(models.OrgInterval)).scalars().all()
    stores = {r.store_key for r in relations} | ({ln.store_key} if ln.store_key else set())
    org_snapshot = [
        {"event_id": r.event_id, "store_key": r.store_key, "change": r.change,
         "region_code": r.region_code, "target_store": r.target_store,
         "biz_from": r.biz_from.isoformat(), "biz_to": r.biz_to.isoformat() if r.biz_to else None,
         "sys_from": r.sys_from.isoformat(), "status": r.status}
        for r in org_rows
        if r.event_id <= run.org_graph_version and r.store_key in stores
    ]
    return {
        "line": line_json(ln),
        "settlement_run": run_json(run),
        "budget_period": budget_json(budget),
        "benefit_cost": None if cost is None else {
            "id": cost.id, "benefit_type": cost.benefit_type, "amount": fnum(cost.amount),
            "rule_version_tag": cost.rule_version_tag, "source_event_id": cost.source_event_id,
        },
        "service_relations": [
            {"event_id": r.event_id, "relation_type": r.relation_type, "store_key": r.store_key,
             "evidence_grade": r.evidence_grade, "biz_time": r.biz_time.isoformat(),
             "ingested_at": r.ingested_at.isoformat(), "status": r.status,
             "revoke_event_id": r.revoke_event_id}
            for r in relations
        ],
        "org_intervals_frozen": org_snapshot,
        "frozen_versions": {
            "org_graph_version": run.org_graph_version,
            "rule_set_version": run.rule_set_version,
            "watermark_event_id": run.watermark_event_id,
            "strategy": run.strategy,
        },
    }


# ---------------- 结算差异发布器 ----------------
class SuggestIn(BaseModel):
    sys_time: str | None = None


@router.post("/settlement/suggestions")
def suggestions(body: SuggestIn, db: Session = Depends(get_db)):
    sys_t = parse_dt(body.sys_time) if body.sys_time else datetime.now(timezone.utc)
    return settle_svc.detect_affected(db, sys_t)


class AdjustIn(BaseModel):
    period_key: str
    sys_time: str | None = None
    strategy: str | None = None
    note: str | None = None


@router.post("/settlement/adjustments", status_code=201)
def create_adjustment(body: AdjustIn, db: Session = Depends(get_db)):
    sys_t = parse_dt(body.sys_time) if body.sys_time else datetime.now(timezone.utc)
    try:
        adj = settle_svc.issue_adjustment(db, body.period_key, sys_t, body.strategy, note=body.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return adjustment_json(adj)


def adjustment_json(a: models.SettlementAdjustment) -> dict:
    return {
        "id": a.id, "period_key": a.period_key, "original_run_id": a.original_run_id,
        "adjustment_run_id": a.adjustment_run_id, "reason_kind": a.reason_kind,
        "trigger_event_ids": a.trigger_event_ids, "delta_amount": fnum(a.delta_amount),
        "detail": a.detail, "created_at": a.created_at.isoformat(), "note": a.note,
    }


@router.get("/settlement/adjustments")
def list_adjustments(period: str | None = None, db: Session = Depends(get_db)):
    rows = db.execute(select(models.SettlementAdjustment).order_by(models.SettlementAdjustment.id)).scalars().all()
    return [adjustment_json(a) for a in rows if not period or a.period_key == period]


@router.get("/settlement/adjustments/{adj_id}")
def get_adjustment(adj_id: int, db: Session = Depends(get_db)):
    a = db.get(models.SettlementAdjustment, adj_id)
    if not a:
        raise HTTPException(404, "调整版本不存在")
    return adjustment_json(a)
