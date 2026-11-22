"""数据模型。

架构选型：追加式双时间事件账 + 可重建投影。
- events 为唯一事实来源（append-only，DB 触发器禁止 UPDATE/DELETE）。
- identity_edges / rule_versions / rule_withdrawals / member_directory 均为可重建投影，
  由事件重放生成，可随时通过 /api/admin/rebuild 全量重建。
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Event(Base):
    """接入账：追加式双时间事件账。

    每行保存：来源哈希(source_hash)、链式哈希(chain_hash)、两类时间
    (biz_time 业务发生时间 / ingested_at 平台接收时间)、修订链(supersedes_id)、
    幂等键(idempotency_key)。
    """

    __tablename__ = "events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    idempotency_key = Column(String(128), unique=True, nullable=False)
    source = Column(String(32), nullable=False)  # pos | online | franchise_offline | identity_desk | rule_admin
    event_type = Column(String(32), nullable=False)
    source_hash = Column(String(64), nullable=False)  # sha256(canonical payload)
    chain_hash = Column(String(64), nullable=False)  # sha256(prev chain_hash + row) 防篡改链
    biz_time = Column(DateTime(timezone=True), nullable=False)
    ingested_at = Column(DateTime(timezone=True), nullable=False)
    actor = Column(String(64))
    supersedes_id = Column(BigInteger, ForeignKey("events.id"), nullable=True)
    payload = Column(JSONB, nullable=False)


class IdentityEdge(Base):
    """身份关系图投影：合并/拆分/待核/撤销的有效区间（双时间区间）。"""

    __tablename__ = "identity_edges"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    relation_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    member_a = Column(String(64), nullable=False)
    member_b = Column(String(64), nullable=False)
    relation = Column(String(16), nullable=False)  # merge | split | pending | revoke
    status = Column(String(16), nullable=False)  # active | split | revoked | pending
    biz_from = Column(DateTime(timezone=True), nullable=False)
    biz_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)
    sys_to = Column(DateTime(timezone=True), nullable=True)
    target_edge_id = Column(BigInteger, nullable=True)  # split/revoke 指向的 merge 边
    reason = Column(Text)


class RuleVersion(Base):
    """等级规则版本投影：按门店、商品范围与生效区间修订。"""

    __tablename__ = "rule_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    rule_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    rule_id = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)
    supersedes_version = Column(Integer, nullable=True)
    store_scope = Column(JSONB, nullable=False)  # "ALL" 或门店列表
    product_scope = Column(JSONB, nullable=False)  # "ALL" 或品类列表
    window_days = Column(Integer, nullable=False)
    thresholds = Column(JSONB, nullable=False)  # {"SILVER": 300, "GOLD": 1000}
    effective_from = Column(DateTime(timezone=True), nullable=False)
    effective_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)


class RuleWithdrawal(Base):
    """规则撤回投影：支持部分（按门店范围）追溯撤回。"""

    __tablename__ = "rule_withdrawals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    withdraw_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    rule_id = Column(String(64), nullable=False)
    target_version = Column(Integer, nullable=False)
    store_scope = Column(JSONB, nullable=False)
    withdraw_from = Column(DateTime(timezone=True), nullable=False)
    withdraw_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)
    reason = Column(Text)


class MemberDirectory(Base):
    """会员名录投影（仅索引已知会员键，不保存会覆盖历史的“当前主档”）。"""

    __tablename__ = "member_directory"

    member_key = Column(String(64), primary_key=True)
    display_name = Column(String(128))
    first_seen_event_id = Column(BigInteger)


class Semantics(Base):
    """时间语义目录：定义查询口径。"""

    __tablename__ = "semantics"

    code = Column(String(32), primary_key=True)
    name = Column(String(64), nullable=False)
    biz_policy = Column(String(16), nullable=False)  # query
    sys_policy = Column(String(16), nullable=False)  # query | now | both
    description = Column(Text)


class Publication(Base):
    """发布库：冻结证据水位、身份关系图版本与规则集合版本。"""

    __tablename__ = "publications"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    biz_as_of = Column(DateTime(timezone=True), nullable=False)
    sys_as_of = Column(DateTime(timezone=True), nullable=False)
    watermark_event_id = Column(BigInteger, nullable=False)  # 证据水位
    identity_graph_version = Column(BigInteger, nullable=False)  # 关系图冻结版本(最大身份事件id)
    rule_set_version = Column(BigInteger, nullable=False)  # 规则集合冻结版本(最大规则事件id)
    semantics_code = Column(String(32), nullable=False)


class PublicationRow(Base):
    __tablename__ = "publication_rows"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    publication_id = Column(BigInteger, ForeignKey("publications.id"), nullable=False)
    member_key = Column(String(64), nullable=False)
    tier = Column(String(16), nullable=False)
    spend = Column(Numeric(14, 2), nullable=False)
    detail = Column(JSONB, nullable=False)


class Diff(Base):
    __tablename__ = "diffs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pub_a = Column(BigInteger, ForeignKey("publications.id"), nullable=False)
    pub_b = Column(BigInteger, ForeignKey("publications.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class DiffItem(Base):
    """差异归因：把资格变化连到具体事件。"""

    __tablename__ = "diff_items"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    diff_id = Column(BigInteger, ForeignKey("diffs.id"), nullable=False)
    member_key = Column(String(64), nullable=False)
    before = Column(JSONB, nullable=False)
    after = Column(JSONB, nullable=False)
    causes = Column(JSONB, nullable=False)  # [{event_id, cause, detail}]


class Impact(Base):
    """影响索引：变化对营销名单、门店预算、训练样本的下游影响。"""

    __tablename__ = "impacts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    diff_id = Column(BigInteger, ForeignKey("diffs.id"), nullable=False)
    dimension = Column(String(16), nullable=False)  # marketing | budget | training
    entity = Column(String(128), nullable=False)  # 下游实体键
    member_key = Column(String(64), nullable=False)
    change_type = Column(String(32), nullable=False)
    before = Column(JSONB)
    after = Column(JSONB)
    attributed_event_ids = Column(JSONB)


class User(Base):
    __tablename__ = "users"

    username = Column(String(64), primary_key=True)
    password = Column(String(128), nullable=False)
    role = Column(String(32), nullable=False)
    token = Column(String(128), nullable=False)


# ======================================================================
# 会员门店归属历史与权益成本归因（第二域，全部为事件账的可重建投影）
# ======================================================================

class Store(Base):
    """门店目录投影：仅索引已知门店，不保存会覆盖历史的“当前组织”字段。"""

    __tablename__ = "stores"

    store_key = Column(String(64), primary_key=True)
    display_name = Column(String(128))
    first_seen_event_id = Column(BigInteger)


class ServiceRelation(Base):
    """服务关系账投影：会员选择/处方服务/到店消费/线上履约/门店确认。

    双时间（biz_time 发生时间 / ingested_at 证据到达时间）+ 证据等级 + 撤销链。
    撤销不删行：status 置 revoked 并记录撤销事件与撤销到达时间。
    """

    __tablename__ = "service_relations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    member_key = Column(String(64), nullable=False, index=True)
    store_key = Column(String(64), nullable=False, index=True)
    relation_type = Column(String(24), nullable=False)
    # member_choice 会员选择 | prescription 处方服务 | instore_visit 到店消费
    # online_fulfillment 线上履约 | store_confirmation 门店确认
    evidence_grade = Column(Integer, nullable=False)  # 1 低 ... 5 高
    biz_time = Column(DateTime(timezone=True), nullable=False)
    ingested_at = Column(DateTime(timezone=True), nullable=False)
    weight_hint = Column(Numeric(8, 4), nullable=False, default=1)  # 可解释权重（服务类别基权）
    status = Column(String(16), nullable=False, default="active")  # active | revoked
    revoke_event_id = Column(BigInteger, nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    detail = Column(JSONB, nullable=False, default=dict)


class OrgInterval(Base):
    """组织沿革图投影：门店开闭、加盟归属、区域调整、合并拆分的双时间有效区间。

    change: open | close | franchise_join | franchise_exit | region_adjust | merge | split
    合并/拆分以 target_store 指向被合并门店；merge 行在拆分事件到达时闭合（沿用身份图语义）。
    """

    __tablename__ = "org_intervals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    store_key = Column(String(64), nullable=False, index=True)
    change = Column(String(24), nullable=False)
    owner_key = Column(String(64), nullable=True)  # 加盟主体
    region_code = Column(String(64), nullable=True)  # 区域
    target_store = Column(String(64), nullable=True)  # merge/split 关联门店
    biz_from = Column(DateTime(timezone=True), nullable=False)
    biz_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)
    sys_to = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(16), nullable=False, default="active")  # active | closed | merged | split
    reason = Column(Text)


class BudgetPeriod(Base):
    """预算周期 + 预算口径（是否允许分摊、结算精度、证据覆盖率门槛）。"""

    __tablename__ = "budget_periods"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    period_key = Column(String(32), unique=True, nullable=False)  # 如 2026-02
    biz_from = Column(DateTime(timezone=True), nullable=False)
    biz_to = Column(DateTime(timezone=True), nullable=False)
    allow_apportion = Column(Boolean, nullable=False, default=False)
    precision_cents = Column(Integer, nullable=False, default=2)  # 结算小数精度（分）
    coverage_threshold = Column(Numeric(6, 4), nullable=False, default=0.60)  # 证据覆盖率门槛
    status = Column(String(16), nullable=False, default="open")  # open | locked
    locked_at = Column(DateTime(timezone=True), nullable=True)


class BenefitCost(Base):
    """权益成本台账：会员等级/积分/生日券等权益在预算期产生的成本（按类别）。"""

    __tablename__ = "benefit_costs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    period_key = Column(String(32), nullable=False, index=True)
    member_key = Column(String(64), nullable=False, index=True)
    benefit_type = Column(String(24), nullable=False)  # tier | points | birthday_coupon
    amount = Column(Numeric(14, 2), nullable=False)
    rule_version_tag = Column(String(64), nullable=False)  # 规则版本标签（成本适用规则）
    source_event_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


class AttributionRun(Base):
    """归属结算单：一次成本归属计算的冻结头（策略与组织版本冻结于此）。"""

    __tablename__ = "attribution_runs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    period_key = Column(String(32), nullable=False, index=True)
    strategy = Column(String(16), nullable=False)  # single_store | apportion
    sys_as_of = Column(DateTime(timezone=True), nullable=False)  # 系统水位（证据可见口径）
    org_graph_version = Column(BigInteger, nullable=False)  # 组织沿革图冻结版本(最大组织事件id)
    rule_set_version = Column(BigInteger, nullable=False)  # 规则集合冻结版本(最大规则事件id)
    watermark_event_id = Column(BigInteger, nullable=False)  # 证据水位(最大可见事件id)
    total_cost = Column(Numeric(14, 2), nullable=False, default=0)
    allocated_cost = Column(Numeric(14, 2), nullable=False, default=0)
    unallocated_cost = Column(Numeric(14, 2), nullable=False, default=0)
    status = Column(String(16), nullable=False, default="settled")  # settled | superseded
    created_at = Column(DateTime(timezone=True), nullable=False)
    note = Column(Text)


class AttributionLine(Base):
    """归属行：权益成本到门店/区域/规则版本/预算周期的关联（含待裁定与残差）。"""

    __tablename__ = "attribution_lines"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(BigInteger, ForeignKey("attribution_runs.id"), nullable=False, index=True)
    period_key = Column(String(32), nullable=False)
    member_key = Column(String(64), nullable=False)
    benefit_type = Column(String(24), nullable=False)
    cost_id = Column(BigInteger, nullable=False)  # 逻辑外键 -> benefit_costs.id（投影可重建，不设硬 FK）
    store_key = Column(String(64), nullable=True)  # 待裁定时为空
    region_code = Column(String(64), nullable=True)
    rule_version_tag = Column(String(64), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False, default=0)
    share = Column(Numeric(10, 6), nullable=False, default=0)  # 服务份额
    status = Column(String(32), nullable=False)
    # allocated 已归属 | pending_adjudication 待裁定 | unallocated 未分配范围 | residual 小数残差
    evidence_event_ids = Column(JSONB, nullable=False, default=list)
    basis = Column(JSONB, nullable=False, default=dict)  # 计算依据（证据、权重、覆盖率）
    reason = Column(Text)


class SettlementAdjustment(Base):
    """结算调整版本：组织修订或迟到服务证据影响既有（已锁定）预算时签发。

    原结算保留不动（run 置 superseded 仅标记，行不修改），调整以新版本承载差额。
    """

    __tablename__ = "settlement_adjustments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    period_key = Column(String(32), nullable=False, index=True)
    original_run_id = Column(BigInteger, ForeignKey("attribution_runs.id"), nullable=False)
    adjustment_run_id = Column(BigInteger, ForeignKey("attribution_runs.id"), nullable=False)
    reason_kind = Column(String(24), nullable=False)  # org_revision | late_evidence
    trigger_event_ids = Column(JSONB, nullable=False, default=list)
    delta_amount = Column(Numeric(14, 2), nullable=False)
    detail = Column(JSONB, nullable=False, default=dict)  # 按门店/区域的差额明细
    created_at = Column(DateTime(timezone=True), nullable=False)
    note = Column(Text)
