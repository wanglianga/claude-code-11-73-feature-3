"""归属计算器：在指定时点为会员计算预算期内的服务门店或服务份额。

两条路线（均可归集全部权益成本，策略与组织版本冻结在结算数据集中）：
- single_store 单店归属：每个预算期选取证据等级最高的一家服务门店，
  成本守恒、门店结算直观；接近的证据会竞争同一归属。
- apportion 权重分摊：按咨询/消费/履约等证据的可解释基权分摊到多家门店，
  协同服务不被单店吞没；需要维护权重并处理小数残差。

红线：
- 门店并购在预算期中生效时，按每条证据的服务发生日解析组织节点，禁止月末组织树回填；
- 最高等级证据指向多家不同门店（如会员选择与履约门店同等级冲突）=> 待裁定，不任选；
- 分摊缺少某类服务证据 => 报告未分配范围，缺口不平均摊给现有门店。
"""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models
from . import RELATION_WEIGHTS, org


@dataclass
class Evidence:
    event_id: int
    relation_type: str
    grade: int
    biz_time: datetime
    ingested_at: datetime
    raw_store: str
    resolved: dict

    def to_json(self) -> dict:
        return {
            "event_id": self.event_id,
            "relation_type": self.relation_type,
            "evidence_grade": self.grade,
            "biz_time": self.biz_time.isoformat(),
            "ingested_at": self.ingested_at.isoformat(),
            "raw_store": self.raw_store,
            "resolved_store": self.resolved["store_key"],
            "region_code": self.resolved["region_code"],
            "org_trail": self.resolved["trail"],
        }


def visible_evidence(db: Session, member: str, biz_from: datetime, biz_to: datetime, sys_t: datetime) -> list[Evidence]:
    """预算期 [biz_from, biz_to) 内、系统时点 T_s 可见且未被（可见）撤销的服务证据。"""
    rows = db.execute(
        select(models.ServiceRelation).where(models.ServiceRelation.member_key == member)
    ).scalars().all()
    out: list[Evidence] = []
    for r in rows:
        if r.ingested_at > sys_t:
            continue
        if not (biz_from <= r.biz_time < biz_to):
            continue
        if r.status == "revoked" and r.revoked_at and r.revoked_at <= sys_t:
            continue  # 撤销在 T_s 前可见 => 证据失效；撤销事件本身留痕于服务关系账
        resolved = org.resolve_store(db, r.store_key, r.biz_time, sys_t)
        out.append(
            Evidence(
                event_id=r.event_id,
                relation_type=r.relation_type,
                grade=r.evidence_grade,
                biz_time=r.biz_time,
                ingested_at=r.ingested_at,
                raw_store=r.store_key,
                resolved=resolved,
            )
        )
    out.sort(key=lambda e: (e.biz_time, e.event_id))
    return out


def coverage_of(evidence: list[Evidence]) -> tuple[float, list[str]]:
    """证据覆盖率 = 已出现证据类别的可解释基权之和（五类基权总和为 1）。"""
    present = {e.relation_type for e in evidence}
    covered = sum(w for typ, w in RELATION_WEIGHTS.items() if typ in present)
    missing = [typ for typ in RELATION_WEIGHTS if typ not in present]
    return round(covered, 6), missing


def _choice_conflict(evidence: list[Evidence]) -> tuple[bool, list[Evidence]]:
    """红线冲突：最高等级证据中，会员选择门店与另一同等级证据指向的门店不同。

    会员选择与履约/消费证据同等级冲突 => 待裁定，不任选。
    不含会员选择的同等级并列不触发本红线（单店路线走确定性平局打破并留痕）。
    """
    if not evidence:
        return False, []
    top_grade = max(e.grade for e in evidence)
    top = [e for e in evidence if e.grade == top_grade]
    choice_stores = {e.resolved["store_key"] for e in top if e.relation_type == "member_choice"}
    other_stores = {e.resolved["store_key"] for e in top if e.relation_type != "member_choice"}
    if choice_stores and (other_stores - choice_stores):
        return True, top
    return False, []


def choose_strategy(budget: models.BudgetPeriod, evidence: list[Evidence], requested: str | None) -> tuple[str, str]:
    """根据预算是否允许分摊、证据覆盖率与结算精度选择路线。"""
    coverage, _ = coverage_of(evidence)
    if requested == "apportion":
        if not budget.allow_apportion:
            return "single_store", "预算不允许分摊，回退单店归属"
        if coverage < float(budget.coverage_threshold):
            return "single_store", f"证据覆盖率 {coverage:.2f} 低于门槛 {float(budget.coverage_threshold):.2f}，回退单店归属"
        return "apportion", "显式要求且预算允许分摊"
    if requested == "single_store":
        return "single_store", "显式要求单店归属"
    # 自动选择
    if budget.allow_apportion and coverage >= float(budget.coverage_threshold):
        return "apportion", f"预算允许分摊且覆盖率 {coverage:.2f} 达门槛，采用权重分摊"
    return "single_store", "默认单店归属（预算不允许分摊或覆盖率不足）"


def calculate(
    db: Session,
    member: str,
    budget: models.BudgetPeriod,
    sys_t: datetime,
    requested_strategy: str | None = None,
) -> dict:
    """计算单会员在预算期的归属（不含金额；金额由成本归因器套用）。"""
    evidence = visible_evidence(db, member, budget.biz_from, budget.biz_to, sys_t)
    coverage, missing = coverage_of(evidence)
    strategy, reason = choose_strategy(budget, evidence, requested_strategy)
    conflict, top = _choice_conflict(evidence)

    result = {
        "member_key": member,
        "period_key": budget.period_key,
        "strategy": strategy,
        "strategy_reason": reason,
        "status": "allocated",
        "coverage": coverage,
        "missing_families": missing,
        "evidence": [e.to_json() for e in evidence],
        "conflicts": [],
        "assignments": [],
        "org_graph_version": org.org_graph_version(db, sys_t),
        "sys_as_of": sys_t.isoformat(),
    }

    if not evidence:
        result["status"] = "unallocated"
        result["reason"] = "预算期内无任何可见服务证据"
        return result

    if conflict:
        result["status"] = "pending_adjudication"
        result["conflicts"] = [e.to_json() for e in top]
        result["reason"] = (
            f"最高证据等级 {top[0].grade} 的证据指向多家门店，"
            "会员选择与履约证据同等级冲突，标记待裁定，不任选门店"
        )
        return result

    if strategy == "single_store":
        winner_grade = max(e.grade for e in evidence)
        winners = [e for e in evidence if e.grade == winner_grade]
        # 证据已按 (biz_time, event_id) 全序排序：同等级非红线并列时确定性取最早证据门店
        winner = winners[0]
        store = winner.resolved["store_key"]
        region = winner.resolved["region_code"]
        result["assignments"] = [{
            "store_key": store,
            "region_code": region,
            "share": 1.0,
            "evidence_event_ids": [e.event_id for e in evidence],
            "winning_grade": winner_grade,
            "tie_break": "earliest_biz_time" if len({e.resolved["store_key"] for e in winners}) > 1 else None,
        }]
    else:
        # 权重分摊：每个证据家族的可解释基权只计一次；同家族多条证据按可信等级
        # 在门店间分配该家族基权（确定性、可解释）。覆盖率按出现的家族集合计算，
        # 缺失家族的基权不参与任何门店份额（进入未分配范围）。
        family_grades: dict[str, dict[str, int]] = {}
        family_ids: dict[str, list[int]] = {}
        family_region: dict[str, dict[str, str | None]] = {}
        for e in evidence:
            sk = e.resolved["store_key"]
            family_grades.setdefault(e.relation_type, {}).setdefault(sk, 0)
            family_grades[e.relation_type][sk] += e.grade
            family_ids.setdefault(e.relation_type, []).append(e.event_id)
            family_region.setdefault(e.relation_type, {})[sk] = e.resolved["region_code"]
        by_store: dict[str, dict] = {}
        for family, stores in family_grades.items():
            base = RELATION_WEIGHTS[family]
            grade_total = sum(stores.values())
            for sk, grade_sum in stores.items():
                bucket = by_store.setdefault(
                    sk, {"store_key": sk, "region_code": family_region[family][sk],
                         "weight": 0.0, "evidence_event_ids": [], "by_family": {}}
                )
                w = base * grade_sum / grade_total
                bucket["weight"] += w
                bucket["by_family"][family] = round(w, 6)
                bucket["evidence_event_ids"].extend(
                    eid for eid in family_ids[family] if eid not in bucket["evidence_event_ids"]
                )
        assignments = []
        for sk in sorted(by_store):
            b = by_store[sk]
            assignments.append({
                "store_key": sk,
                "region_code": b["region_code"],
                "share": round(b["weight"], 6),  # 注意：不做归一化补缺口
                "evidence_event_ids": sorted(b["evidence_event_ids"]),
                "by_family": {k: round(v, 6) for k, v in b["by_family"].items()},
            })
        result["assignments"] = assignments
    return result
