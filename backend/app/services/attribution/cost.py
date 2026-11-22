"""成本归因器：把权益成本关联到门店、区域、规则版本和预算周期。

守恒约束：每条权益成本的归属行金额之和必须等于成本本身。
- single_store：整额归入最高证据门店；
- apportion：按服务份额量化到各门店，量化产生的小数残差单独成行（residual），
  归入份额最大的门店并标注，不静默修改任何门店份额；
- 缺少某类服务证据时，按缺失基权计算“未分配范围”，缺口不平均摊给现有门店；
- 证据冲突待裁定时，成本整额挂起（pending_adjudication），不任选门店。

归属策略与组织版本、规则集版本、证据水位一并冻结在结算单（AttributionRun）中。
"""
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models
from . import calculator
from .org import org_graph_version

RULE_TYPES = ("rule_upsert", "rule_withdraw")


def _q(amount: Decimal, precision: int) -> Decimal:
    return amount.quantize(Decimal("1").scaleb(-precision), rounding=ROUND_HALF_UP)


def _visible_costs(db: Session, period_key: str, sys_t: datetime) -> list[models.BenefitCost]:
    rows = db.execute(
        select(models.BenefitCost).where(models.BenefitCost.period_key == period_key)
    ).scalars().all()
    return [c for c in rows if c.created_at <= sys_t]


def _frozen_versions(db: Session, sys_t: datetime) -> tuple[int, int, int]:
    events = db.execute(select(models.Event)).scalars().all()
    visible = [e for e in events if e.ingested_at <= sys_t]
    watermark = max((e.id for e in visible), default=0)
    rule_ver = max((e.id for e in visible if e.event_type in RULE_TYPES), default=0)
    return watermark, org_graph_version(db, sys_t), rule_ver


def build_run(
    db: Session,
    budget: models.BudgetPeriod,
    sys_t: datetime,
    strategy: str | None = None,
    *,
    note: str | None = None,
) -> models.AttributionRun:
    """构建一张结算单（内部方法，不加锁校验；初次结算与调整版本共用）。"""
    costs = _visible_costs(db, budget.period_key, sys_t)
    watermark, org_ver, rule_ver = _frozen_versions(db, sys_t)

    run = models.AttributionRun(
        period_key=budget.period_key,
        strategy="pending",  # 以首个成功计算的策略回填
        sys_as_of=sys_t,
        org_graph_version=org_ver,
        rule_set_version=rule_ver,
        watermark_event_id=watermark,
        created_at=datetime.now(timezone.utc),
        note=note,
    )
    db.add(run)
    db.flush()

    precision = budget.precision_cents
    total = allocated = unallocated = Decimal("0")
    strategies_used: set[str] = set()
    members = sorted({c.member_key for c in costs})

    for member in members:
        calc = calculator.calculate(db, member, budget, sys_t, strategy)
        strategies_used.add(calc["strategy"])
        member_costs = [c for c in costs if c.member_key == member]
        for cost in member_costs:
            total += Decimal(str(cost.amount))
            base_basis = {
                "cost_id": cost.id,
                "strategy": calc["strategy"],
                "strategy_reason": calc["strategy_reason"],
                "coverage": calc["coverage"],
                "missing_families": calc["missing_families"],
                "evidence": calc["evidence"],
                "conflicts": calc["conflicts"],
            }
            if calc["status"] == "pending_adjudication":
                unallocated += Decimal(str(cost.amount))
                db.add(_line(run, cost, None, None, cost.amount, Decimal("0"),
                             "pending_adjudication", [], base_basis, calc.get("reason")))
                continue
            if calc["status"] == "unallocated":
                unallocated += Decimal(str(cost.amount))
                db.add(_line(run, cost, None, None, cost.amount, Decimal("0"),
                             "unallocated", [], base_basis, calc.get("reason")))
                continue

            if calc["strategy"] == "single_store":
                a = calc["assignments"][0]
                allocated += Decimal(str(cost.amount))
                db.add(_line(run, cost, a["store_key"], a["region_code"], cost.amount,
                             Decimal("1"), "allocated", a["evidence_event_ids"], base_basis))
                continue

            # 权重分摊：门店量化 + 未分配范围量化 + 纯舍入残差，三段严格守恒
            amount_dec = Decimal(str(cost.amount))
            quantized: list[tuple[dict, Decimal]] = []
            store_sum = Decimal("0")
            for a in calc["assignments"]:
                piece = _q(amount_dec * Decimal(str(a["share"])), precision)
                quantized.append((a, piece))
                store_sum += piece
            # 未分配范围：覆盖率缺口对应成本，独立量化，缺口不平均摊给现有门店
            missing_weight = Decimal("1") - Decimal(str(calc["coverage"]))
            unalloc = _q(amount_dec * missing_weight, precision) if missing_weight > 0 else Decimal("0")
            # 残差仅承载舍入误差（可为负），归入份额最大门店并单独标注
            residual = amount_dec - store_sum - unalloc
            for a, piece in quantized:
                allocated += piece
                db.add(_line(run, cost, a["store_key"], a["region_code"], piece,
                             Decimal(str(a["share"])), "allocated", a["evidence_event_ids"],
                             base_basis | {"by_family": a.get("by_family")}))
            if unalloc > 0:
                unallocated += unalloc
                db.add(_line(run, cost, None, None, unalloc, Decimal("0"), "unallocated",
                             [e["event_id"] for e in calc["evidence"]],
                             base_basis | {"missing_weight": float(missing_weight)},
                             f"缺少服务证据类别 {calc['missing_families']}，报告未分配范围，不平均摊给现有门店"))
            if residual != 0:
                host = max(calc["assignments"], key=lambda x: x["share"])
                allocated += residual
                db.add(_line(run, cost, host["store_key"], host["region_code"], residual,
                             Decimal("0"), "residual", host["evidence_event_ids"],
                             base_basis | {"residual_of": [a["store_key"] for a, _ in quantized]},
                             f"分摊量化小数残差（精度 {precision} 位，可为负），归入份额最大门店，不改变门店份额"))

    run.strategy = (
        strategies_used.pop() if len(strategies_used) == 1
        else "mixed" if strategies_used
        else (strategy or "auto")
    )
    run.total_cost = total
    run.allocated_cost = allocated
    run.unallocated_cost = unallocated
    db.flush()
    return run


def _line(run, cost: models.BenefitCost, store, region, amount, share, status, evidence_ids, basis, reason=None):
    return models.AttributionLine(
        run_id=run.id,
        period_key=cost.period_key,
        member_key=cost.member_key,
        benefit_type=cost.benefit_type,
        cost_id=cost.id,
        store_key=store,
        region_code=region,
        rule_version_tag=cost.rule_version_tag,
        amount=amount,
        share=share,
        status=status,
        evidence_event_ids=evidence_ids,
        basis=basis,
        reason=reason,
    )


def settle_period(
    db: Session,
    period_key: str,
    sys_t: datetime,
    strategy: str | None = None,
    *,
    lock: bool = False,
    note: str | None = None,
) -> models.AttributionRun:
    """预算期初次结算。已锁定预算拒绝直接重算，须走结算差异发布器。"""
    budget = db.execute(
        select(models.BudgetPeriod).where(models.BudgetPeriod.period_key == period_key)
    ).scalar_one_or_none()
    if budget is None:
        raise ValueError(f"预算周期不存在: {period_key}")
    if budget.status == "locked":
        raise ValueError("预算已锁定：原结算保留，请通过结算差异发布器生成调整版本")
    run = build_run(db, budget, sys_t, strategy, note=note)
    if lock:
        budget.status = "locked"
        budget.locked_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return run
