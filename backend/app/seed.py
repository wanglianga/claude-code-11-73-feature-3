"""演示种子场景（全部通过接入账写入，幂等键固定，可重复执行）。

同一会员 A(13800000001) 贯穿四类历史变更：
1. 加盟店离线补传：T4 业务时间 2026-01-28，平台 2026-03-15 才收到；
2. 误合并后拆分：2026-02-15 人工裁定把 B 并入 A，2026-04-05 追溯拆分 + 撤销；
3. 部分追溯规则撤回：规则 v2（2026-02-01 起追溯生效，GOLD 阈值 1000→1500）
   于 2026-04-10 仅对门店 S002 撤回；
4. 并发修订：v2 与 v3 在 2026-03-01 相隔 5 分钟先后修订 v1，
   由 (effective_from, version) 全序确定性解决，修订链完整保留。
另有更正链：T3 金额 300 → 380（2026-03-05 补录）。

ATTR_SEED_EVENTS 为门店归属域五场景（事件 id 接在原 18 个之后）：
M1 跨店分摊+小数残差、M2 同等级证据冲突待裁定、M3 期中并购按日拆分、
M4 缺证据未分配范围、M5 迟到处方服务冲击已锁定预算；
含组织沿革（S101–S105 开闭/区域/并购/拆分 + 追溯区域修订）、
两个预算周期（2026-02 允许分摊 / 2026-03 单店）与 6 条权益成本。
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .services.ledger import ingest_event


def T(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


SEED_EVENTS = [
    # ---- 交易：会员 A ----
    dict(idem="seed-t1", source="pos", type="transaction", biz="2026-01-05T10:20:00", ing="2026-01-05T10:20:05",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "S001", "category": "OTC", "amount": 500, "channel": "pos"}),
    dict(idem="seed-t2", source="online", type="transaction", biz="2026-01-20T21:00:00", ing="2026-01-20T21:00:03",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "ONLINE", "category": "保健", "amount": 400, "channel": "app"}),
    dict(idem="seed-t3", source="pos", type="transaction", biz="2026-02-10T12:00:00", ing="2026-02-10T12:00:04",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "S001", "category": "RX", "amount": 300, "channel": "pos"}),
    # 更正：T3 金额 300 -> 380（修订链 supersedes）
    dict(idem="seed-t3-fix", source="pos", type="transaction", biz="2026-02-10T12:00:00", ing="2026-03-05T09:00:00",
         supersedes="seed-t3",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "S001", "category": "RX", "amount": 380, "channel": "pos", "comment": "收银错单更正"}),
    # 加盟店离线补传：业务时间 2026-01-28，2026-03-15 才到达平台
    dict(idem="seed-t4", source="franchise_offline", type="transaction", biz="2026-01-28T15:30:00", ing="2026-03-15T08:00:00",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "S002", "category": "OTC", "amount": 350, "channel": "offline_batch", "batch": "S002-2026-03"}),
    # ---- 其他会员 ----
    dict(idem="seed-t5", source="pos", type="transaction", biz="2026-02-01T11:00:00", ing="2026-02-01T11:00:02",
         payload={"member_key": "13900000002", "member_name": "李芳", "store": "S001", "category": "OTC", "amount": 600, "channel": "pos"}),
    dict(idem="seed-t9", source="pos", type="transaction", biz="2026-02-15T16:00:00", ing="2026-02-15T16:00:02",
         payload={"member_key": "13900000003", "member_name": "王强", "store": "S001", "category": "OTC", "amount": 100, "channel": "pos"}),
    dict(idem="seed-t7", source="pos", type="transaction", biz="2026-02-05T10:00:00", ing="2026-02-05T10:00:02",
         payload={"member_key": "13900000004", "member_name": "赵敏", "store": "S002", "category": "OTC", "amount": 1100, "channel": "pos"}),
    dict(idem="seed-t8", source="pos", type="transaction", biz="2026-02-03T10:00:00", ing="2026-02-03T10:00:02",
         payload={"member_key": "13900000005", "member_name": "孙磊", "store": "S001", "category": "RX", "amount": 1200, "channel": "pos"}),
    dict(idem="seed-t10", source="pos", type="transaction", biz="2026-02-08T10:00:00", ing="2026-02-08T10:00:02",
         payload={"member_key": "13900000006", "member_name": "钱进", "store": "S002", "category": "OTC", "amount": 1400, "channel": "pos"}),
    # ---- 规则：v1 ----
    dict(idem="seed-r1", source="rule_admin", type="rule_upsert", biz="2026-01-01T00:00:00", ing="2026-01-01T00:00:00",
         payload={"rule_id": "tier-standard", "version": 1, "store_scope": "ALL", "product_scope": "ALL",
                  "window_days": 90, "thresholds": {"SILVER": 300, "GOLD": 1000}, "effective_from": "2026-01-01T00:00:00+00:00"}),
    # ---- 身份：误合并（人工裁定）----
    dict(idem="seed-m1", source="identity_desk", type="identity_merge", biz="2026-02-15T09:00:00", ing="2026-02-15T09:00:00",
         payload={"primary_key": "13800000001", "secondary_key": "13900000002", "reason": "人工裁定：疑似同一人（事后证实为误合并）", "adjudicator": "ops-01"}),
    # ---- 并发规则修订：v2、v3 相隔 5 分钟先后修订 v1 ----
    dict(idem="seed-r2", source="rule_admin", type="rule_upsert", biz="2026-03-01T09:00:00", ing="2026-03-01T09:00:00",
         payload={"rule_id": "tier-standard", "version": 2, "supersedes_version": 1, "store_scope": "ALL", "product_scope": "ALL",
                  "window_days": 90, "thresholds": {"SILVER": 300, "GOLD": 1500}, "effective_from": "2026-02-01T00:00:00+00:00",
                  "comment": "追溯修订：2 月起 GOLD 阈值上调"}),
    dict(idem="seed-r3", source="rule_admin", type="rule_upsert", biz="2026-03-01T09:05:00", ing="2026-03-01T09:05:00",
         payload={"rule_id": "tier-standard", "version": 3, "supersedes_version": 1, "store_scope": "ALL", "product_scope": "ALL",
                  "window_days": 90, "thresholds": {"SILVER": 300, "GOLD": 1300}, "effective_from": "2026-03-01T00:00:00+00:00",
                  "comment": "并发修订：3 月起 GOLD 阈值 1300"}),
    # ---- 身份：待核（不影响资格）----
    dict(idem="seed-p1", source="identity_desk", type="identity_pending", biz="2026-03-20T10:00:00", ing="2026-03-20T10:00:00",
         payload={"key_a": "13800000001", "key_b": "13900000003", "reason": "手机号相似，待核"}),
    # ---- 身份：误合并追溯拆分 + 撤销 ----
    dict(idem="seed-s1", source="identity_desk", type="identity_split", biz="2026-04-05T09:00:00", ing="2026-04-05T09:00:00",
         payload={"primary_key": "13800000001", "secondary_key": "13900000002", "merge_event_key": "seed-m1",
                  "retroactive": True, "reason": "误合并拆分：双方确认为不同自然人"}),
    dict(idem="seed-v1", source="identity_desk", type="identity_revoke", biz="2026-04-05T10:00:00", ing="2026-04-05T10:00:00",
         payload={"target_event_key": "seed-m1", "reason": "撤销申请：原人工裁定证据不足"}),
    # ---- 规则：部分追溯撤回（仅门店 S002）----
    dict(idem="seed-r4", source="rule_admin", type="rule_withdraw", biz="2026-04-10T10:00:00", ing="2026-04-10T10:00:00",
         payload={"rule_id": "tier-standard", "target_version": 2, "store_scope": ["S002"],
                  "withdraw_from": "2026-02-01T00:00:00+00:00",
                  "reason": "加盟店 S002 促销口径争议，v2 对该门店追溯撤回"}),
]

# ======================================================================
# 门店归属与权益成本归因演示场景（事件 id 接在原 18 个之后，保持旧结论稳定）
# 覆盖：跨店服务分摊+残差 / 证据冲突待裁定 / 期中并购按日拆分 /
#       缺证据未分配范围 / 迟到服务与组织修订冲击已锁定预算
# ======================================================================
M1, M2, M3, M4, M5 = "13810000001", "13810000002", "13810000003", "13810000004", "13810000005"

ATTR_SEED_EVENTS = [
    # ---- 组织沿革图（门店 3 月建档，历史区间按业务时间回填）----
    dict(idem="seed-org-open-101", source="org_admin", type="store_open", biz="2026-01-01T00:00:00", ing="2026-03-01T00:01:00",
         payload={"store_key": "S101", "store_name": "城东旗舰店", "region_code": "R-EAST", "owner_key": "DIRECT",
                  "reason": "组织系统 3 月上线，补录开店沿革"}),
    dict(idem="seed-org-open-102", source="org_admin", type="store_open", biz="2026-01-01T00:00:00", ing="2026-03-01T00:02:00",
         payload={"store_key": "S102", "store_name": "通勤路店", "region_code": "R-WEST", "owner_key": "DIRECT"}),
    dict(idem="seed-org-open-103", source="org_admin", type="store_open", biz="2026-01-01T00:00:00", ing="2026-03-01T00:03:00",
         payload={"store_key": "S103", "store_name": "城南加盟店", "region_code": "R-SOUTH", "owner_key": "FR-07"}),
    dict(idem="seed-org-open-104", source="org_admin", type="store_open", biz="2026-03-01T00:00:00", ing="2026-03-01T00:04:00",
         payload={"store_key": "S104", "store_name": "新城中心店", "region_code": "R-SOUTH", "owner_key": "DIRECT"}),
    # 区域调整：S102 3-10 起划入北区
    dict(idem="seed-org-region-102", source="org_admin", type="region_adjust", biz="2026-03-10T00:00:00", ing="2026-03-10T09:00:00",
         payload={"store_key": "S102", "region_code": "R-NORTH", "reason": "服务区调整：通勤路店划入北区"}),
    # 期中并购：S103 于 3-15 并入 S104（3-12 公告，业务日 3-15 生效）
    dict(idem="seed-org-merge-103", source="org_admin", type="store_merge", biz="2026-03-15T00:00:00", ing="2026-03-12T10:00:00",
         payload={"store_key": "S104", "target_store": "S103", "region_code": "R-SOUTH",
                  "reason": "加盟店并购：城南加盟店 S103 并入新城中心店 S104，3-15 生效"}),
    # 拆分：S105 4-01 从 S101 拆分
    dict(idem="seed-org-split-105", source="org_admin", type="store_split", biz="2026-04-01T00:00:00", ing="2026-04-01T08:00:00",
         payload={"store_key": "S105", "target_store": "S101", "region_code": "R-EAST",
                  "reason": "城东旗舰店拆分出社区店 S105"}),
    # 滞后组织修订：S104 区域追溯调整（4-25 才到达，影响已锁定的 3 月预算）
    dict(idem="seed-org-region-104-late", source="org_admin", type="region_adjust", biz="2026-03-01T00:00:00", ing="2026-04-25T09:00:00",
         payload={"store_key": "S104", "region_code": "R-CENTRAL",
                  "reason": "追溯区域修订：新城中心店 3 月起归属中区"}),

    # ---- 预算周期 ----
    dict(idem="seed-budget-2026-02", source="budget_admin", type="budget_period_define", biz="2026-02-01T00:00:00", ing="2026-02-01T00:00:00",
         payload={"period_key": "2026-02", "biz_from": "2026-02-01T00:00:00+00:00", "biz_to": "2026-03-01T00:00:00+00:00",
                  "allow_apportion": True, "precision_cents": 2, "coverage_threshold": 0.45}),
    dict(idem="seed-budget-2026-03", source="budget_admin", type="budget_period_define", biz="2026-03-01T00:00:00", ing="2026-03-01T00:00:00",
         payload={"period_key": "2026-03", "biz_from": "2026-03-01T00:00:00+00:00", "biz_to": "2026-04-01T00:00:00+00:00",
                  "allow_apportion": False, "precision_cents": 2, "coverage_threshold": 0.45}),

    # ---- M1 陈曦：跨店服务，五类证据齐全 -> 权重分摊（88.88 制造小数残差）----
    dict(idem="seed-m1-choice", source="service_desk", type="service_choice", biz="2026-02-02T10:00:00", ing="2026-02-02T10:00:00",
         payload={"member_key": M1, "member_name": "陈曦", "store_key": "S101", "evidence_grade": 4}),
    dict(idem="seed-m1-rx", source="service_desk", type="service_prescription", biz="2026-02-05T15:00:00", ing="2026-02-05T15:00:00",
         payload={"member_key": M1, "store_key": "S101", "evidence_grade": 5, "case_no": "RX-20260205-11"}),
    dict(idem="seed-m1-visit", source="service_desk", type="service_visit", biz="2026-02-12T18:30:00", ing="2026-02-12T18:30:00",
         payload={"member_key": M1, "store_key": "S102", "evidence_grade": 3}),
    dict(idem="seed-m1-online", source="service_desk", type="service_online", biz="2026-02-18T20:00:00", ing="2026-02-18T20:00:00",
         payload={"member_key": M1, "store_key": "ONLINE", "evidence_grade": 3, "order_no": "OL-7781"}),
    dict(idem="seed-m1-confirm", source="service_desk", type="service_confirmation", biz="2026-02-20T09:00:00", ing="2026-02-20T09:00:00",
         payload={"member_key": M1, "store_key": "S101", "evidence_grade": 2}),

    # ---- M2 何静：会员选择与线上履约同等级、门店冲突 -> 待裁定 ----
    dict(idem="seed-m2-choice", source="service_desk", type="service_choice", biz="2026-02-03T11:00:00", ing="2026-02-03T11:00:00",
         payload={"member_key": M2, "member_name": "何静", "store_key": "S101", "evidence_grade": 4}),
    dict(idem="seed-m2-online", source="service_desk", type="service_online", biz="2026-02-16T21:00:00", ing="2026-02-16T21:00:00",
         payload={"member_key": M2, "store_key": "S102", "evidence_grade": 4, "order_no": "OL-7802"}),

    # ---- M4 高远：仅两类证据，覆盖率 0.45 -> 分摊但保留未分配范围 ----
    dict(idem="seed-m4-visit", source="service_desk", type="service_visit", biz="2026-02-08T17:00:00", ing="2026-02-08T17:00:00",
         payload={"member_key": M4, "member_name": "高远", "store_key": "S102", "evidence_grade": 3}),
    dict(idem="seed-m4-online", source="service_desk", type="service_online", biz="2026-02-19T19:30:00", ing="2026-02-19T19:30:00",
         payload={"member_key": M4, "store_key": "ONLINE", "evidence_grade": 3}),

    # ---- M5 马芳：首批证据两类，4 月迟到处方服务冲击已锁定的 2 月预算 ----
    dict(idem="seed-m5-choice", source="service_desk", type="service_choice", biz="2026-02-06T10:30:00", ing="2026-02-06T10:30:00",
         payload={"member_key": M5, "member_name": "马芳", "store_key": "S101", "evidence_grade": 4}),
    dict(idem="seed-m5-visit", source="service_desk", type="service_visit", biz="2026-02-15T12:00:00", ing="2026-02-15T12:00:00",
         payload={"member_key": M5, "store_key": "S101", "evidence_grade": 3}),
    dict(idem="seed-m5-rx-late", source="franchise_offline", type="service_prescription", biz="2026-02-21T16:00:00", ing="2026-04-12T08:30:00",
         payload={"member_key": M5, "store_key": "S102", "evidence_grade": 5,
                  "case_no": "RX-20260221-07", "batch": "offline-2026-04",
                  "comment": "加盟店离线补传：2 月处方服务 4 月才到达平台"}),

    # ---- M3 林楠：3 月期中并购，服务发生日跨 3-15，按日解析归属 ----
    dict(idem="seed-m3-visit-pre", source="service_desk", type="service_visit", biz="2026-03-05T14:00:00", ing="2026-03-05T14:00:00",
         payload={"member_key": M3, "member_name": "林楠", "store_key": "S103", "evidence_grade": 3}),
    dict(idem="seed-m3-visit-post", source="service_desk", type="service_visit", biz="2026-03-20T14:00:00", ing="2026-03-20T14:00:00",
         payload={"member_key": M3, "store_key": "S103", "evidence_grade": 4,
                  "comment": "并购后到店，原 S103 门店受理，组织上继承 S104"}),
    dict(idem="seed-m3-choice", source="service_desk", type="service_choice", biz="2026-03-22T09:00:00", ing="2026-03-22T09:00:00",
         payload={"member_key": M3, "store_key": "S104", "evidence_grade": 5}),

    # ---- 权益成本台账 ----
    dict(idem="seed-cost-m1-birthday", source="benefit_admin", type="benefit_cost", biz="2026-02-28T23:00:00", ing="2026-03-01T08:00:00",
         payload={"period_key": "2026-02", "member_key": M1, "benefit_type": "birthday_coupon",
                  "amount": 100.00, "rule_version_tag": "benefit-rule-v1"}),
    dict(idem="seed-cost-m1-tier", source="benefit_admin", type="benefit_cost", biz="2026-02-28T23:00:00", ing="2026-03-01T08:05:00",
         payload={"period_key": "2026-02", "member_key": M1, "benefit_type": "tier",
                  "amount": 88.88, "rule_version_tag": "benefit-rule-v1"}),
    dict(idem="seed-cost-m2-points", source="benefit_admin", type="benefit_cost", biz="2026-02-28T23:00:00", ing="2026-03-01T08:10:00",
         payload={"period_key": "2026-02", "member_key": M2, "benefit_type": "points",
                  "amount": 60.00, "rule_version_tag": "benefit-rule-v1"}),
    dict(idem="seed-cost-m4-points", source="benefit_admin", type="benefit_cost", biz="2026-02-28T23:00:00", ing="2026-03-01T08:15:00",
         payload={"period_key": "2026-02", "member_key": M4, "benefit_type": "points",
                  "amount": 200.00, "rule_version_tag": "benefit-rule-v1"}),
    dict(idem="seed-cost-m5-tier", source="benefit_admin", type="benefit_cost", biz="2026-02-28T23:00:00", ing="2026-03-01T08:20:00",
         payload={"period_key": "2026-02", "member_key": M5, "benefit_type": "tier",
                  "amount": 50.00, "rule_version_tag": "benefit-rule-v1"}),
    dict(idem="seed-cost-m3-tier", source="benefit_admin", type="benefit_cost", biz="2026-03-31T23:00:00", ing="2026-04-01T08:00:00",
         payload={"period_key": "2026-03", "member_key": M3, "benefit_type": "tier",
                  "amount": 120.00, "rule_version_tag": "benefit-rule-v2"}),
]


def seed(db: Session) -> dict:
    from sqlalchemy import select

    from . import models

    created, skipped = 0, 0
    key_to_id: dict[str, int] = {}
    for spec in SEED_EVENTS + ATTR_SEED_EVENTS:
        supersedes_id = None
        if spec.get("supersedes"):
            supersedes_id = key_to_id.get(spec["supersedes"])
        payload = dict(spec["payload"])
        # 把业务键引用解析为事件 id（拆分/撤销指向合并事件）
        for ref_in, ref_out in (("merge_event_key", "merge_event_id"), ("target_event_key", "target_event_id")):
            if ref_in in payload:
                payload[ref_out] = key_to_id.get(payload.pop(ref_in))
        ev, was_created = ingest_event(
            db,
            source=spec["source"],
            event_type=spec["type"],
            biz_time=T(spec["biz"]),
            ingested_at=T(spec["ing"]),
            payload=payload,
            actor="seed",
            supersedes_id=supersedes_id,
            idempotency_key=spec["idem"],
        )
        key_to_id[spec["idem"]] = ev.id
        created += 1 if was_created else 0
        skipped += 0 if was_created else 1

    # 时间语义目录
    semantics = [
        ("AS_OF", "当时可见", "query", "query",
         "以查询时点同时作为业务截止与系统截止：只纳入当时已到达平台的证据，重现当时可见的资格结论。"),
        ("AS_KNOWN_NOW", "事后补全", "query", "now",
         "业务时点不变、系统时点为当前：纳入其后到达的补传、更正、身份与规则变更，给出事后补全口径。"),
        ("DELTA", "二者差异", "query", "both",
         "对同一业务时点分别按两种口径计算并归因差异（迟到证据、更正、身份变更、规则修订/撤回）。"),
    ]
    for code, name, biz_p, sys_p, desc in semantics:
        if db.get(models.Semantics, code) is None:
            db.add(models.Semantics(code=code, name=name, biz_policy=biz_p, sys_policy=sys_p, description=desc))
    # 演示账号
    for username, password, role in (("admin", "admin123", "管理员"), ("analyst", "analyst123", "分析师")):
        if db.get(models.User, username) is None:
            db.add(models.User(username=username, password=password, role=role, token=f"token-{username}-2026"))
    db.commit()
    return {"events_created": created, "events_skipped": skipped, "total_events": db.query(models.Event).count()}
