"""结算差异发布器：组织修订或迟到服务证据影响既有（已锁定）预算时，
保留原结算并生成调整版本。

- 原结算行永不修改、永不删除：仅在原结算头标记 superseded 表示血缘，
  调整以新结算单承载，差额按门店/区域逐行给出；
- 触发原因：两次系统水位之间到达的组织事件（org_revision）或服务证据（late_evidence）；
- detect 只定位受影响预算并给出调整建议，不写数据；issue 才签发调整版本。
"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models
from . import ORG_EVENT_TYPES, SERVICE_EVENT_TYPES
from .cost import build_run


def _latest_run(db: Session, period_key: str) -> models.AttributionRun | None:
    rows = db.execute(
        select(models.AttributionRun)
        .where(models.AttributionRun.period_key == period_key)
        .order_by(models.AttributionRun.id.desc())
    ).scalars().all()
    return rows[0] if rows else None


def detect_triggers(db: Session, period: models.BudgetPeriod, since_sys: datetime, until_sys: datetime) -> list[models.Event]:
    """定位两个系统水位之间到达、且可能影响该预算期的事件。"""
    events = db.execute(select(models.Event).order_by(models.Event.id)).scalars().all()
    out = []
    for e in events:
        if not (since_sys < e.ingested_at <= until_sys):
            continue
        if e.event_type in ORG_EVENT_TYPES:
            out.append(e)  # 组织修订可能改变该期任一服务日的门店解析
        elif e.event_type in SERVICE_EVENT_TYPES:
            p = e.payload or {}
            # 迟到服务证据：业务时间落在预算期内（或撤销指向期内证据）
            if period.biz_from <= e.biz_time < period.biz_to or (
                e.event_type == "service_revoke" and p.get("period_key") == period.period_key
            ):
                out.append(e)
    return out


def detect_affected(db: Session, sys_t: datetime) -> list[dict]:
    """扫描已锁定预算期，定位被新到达证据/组织修订影响的既有结算，给出调整建议。"""
    periods = db.execute(
        select(models.BudgetPeriod).where(models.BudgetPeriod.status == "locked")
    ).scalars().all()
    suggestions = []
    for period in periods:
        run = _latest_run(db, period.period_key)
        if run is None:
            continue
        triggers = detect_triggers(db, period, run.sys_as_of, sys_t)
        if not triggers:
            continue
        kinds = sorted({("org_revision" if e.event_type in ORG_EVENT_TYPES else "late_evidence") for e in triggers})
        suggestions.append({
            "period_key": period.period_key,
            "original_run_id": run.id,
            "original_sys_as_of": run.sys_as_of.isoformat(),
            "trigger_event_ids": [e.id for e in triggers],
            "reason_kinds": kinds,
            "suggestion": "预算已锁定且存在新到达的组织修订/迟到服务证据，建议签发结算调整版本（原结算保留）",
        })
    return suggestions


def _store_totals(run_id: int, lines: list[models.AttributionLine]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for ln in lines:
        key = ln.store_key or f"UNALLOC:{ln.status}"
        totals[key] = totals.get(key, Decimal("0")) + Decimal(str(ln.amount))
    return totals


def issue_adjustment(
    db: Session,
    period_key: str,
    sys_t: datetime,
    strategy: str | None = None,
    *,
    note: str | None = None,
) -> models.SettlementAdjustment:
    """为已锁定预算签发调整版本：原结算保留，新结算承载重算结果，差额逐门店记录。"""
    period = db.execute(
        select(models.BudgetPeriod).where(models.BudgetPeriod.period_key == period_key)
    ).scalar_one_or_none()
    if period is None:
        raise ValueError(f"预算周期不存在: {period_key}")
    original = _latest_run(db, period_key)
    if original is None:
        raise ValueError("该预算期尚无结算单，无法签发调整版本")
    if period.status != "locked":
        raise ValueError("预算未锁定：请直接重新结算，无需调整版本")

    triggers = detect_triggers(db, period, original.sys_as_of, sys_t)
    if not triggers:
        raise ValueError("两个系统水位之间没有影响该预算期的新事件，无需调整")
    reason_kind = "org_revision" if any(e.event_type in ORG_EVENT_TYPES for e in triggers) else "late_evidence"

    # 重算为新版本（预算锁定状态下绕过 settle_period 的锁校验，直接构建）
    new_run = build_run(db, period, sys_t, strategy, note=f"调整版本：{note or reason_kind}")

    old_lines = db.execute(
        select(models.AttributionLine).where(models.AttributionLine.run_id == original.id)
    ).scalars().all()
    new_lines = db.execute(
        select(models.AttributionLine).where(models.AttributionLine.run_id == new_run.id)
    ).scalars().all()
    old_totals = _store_totals(original.id, old_lines)
    new_totals = _store_totals(new_run.id, new_lines)
    detail = []
    delta_total = Decimal("0")
    for key in sorted(set(old_totals) | set(new_totals)):
        delta = new_totals.get(key, Decimal("0")) - old_totals.get(key, Decimal("0"))
        delta_total += delta
        detail.append({
            "store_or_bucket": key,
            "before": float(old_totals.get(key, Decimal("0"))),
            "after": float(new_totals.get(key, Decimal("0"))),
            "delta": float(delta),
        })

    adjustment = models.SettlementAdjustment(
        period_key=period_key,
        original_run_id=original.id,
        adjustment_run_id=new_run.id,
        reason_kind=reason_kind,
        trigger_event_ids=[e.id for e in triggers],
        delta_amount=delta_total,
        detail=detail,
        created_at=datetime.now(timezone.utc),
        note=note,
    )
    original.status = "superseded"  # 仅标记血缘；原结算行保持不变
    db.add(adjustment)
    db.commit()
    db.refresh(adjustment)
    return adjustment
