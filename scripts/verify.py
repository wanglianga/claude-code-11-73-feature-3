#!/usr/bin/env python3
"""端到端验证：任意时点资格、两种口径差异、下游影响。

用法: python3 scripts/verify.py [base_url]
前置: 后端已启动（默认 http://127.0.0.1:8000）。脚本会重置业务数据并灌入种子场景。
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
A, B, C, D, F, G = ("13800000001", "13900000002", "13900000003",
                    "13900000004", "13900000005", "13900000006")
# 门店归属域会员
M1, M2, M3, M4, M5 = ("13810000001", "13810000002", "13810000003",
                      "13810000004", "13810000005")
TOTAL_EVENTS = 49
passed, failed = 0, 0


def call(method, path, body=None, token=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} | ({"Authorization": f"Bearer {token}"} if token else {}),
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def check(name, cond, detail=""):
    global passed, failed
    passed, failed = passed + bool(cond), failed + (not cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  -> {detail}"))


def elig(token, member, biz, semantics="AS_OF", sys_time=None):
    q = f"/api/members/{member}/eligibility?biz_time={biz}&semantics={semantics}"
    if sys_time:
        q += f"&sys_time={sys_time}"
    return call("GET", q, token=token)


def main():
    token = call("POST", "/api/login", {"username": "admin", "password": "admin123"})["token"]
    call("POST", "/api/admin/reset", token=token)
    seed = call("POST", "/api/seed", token=token)
    check(f"种子灌入 {TOTAL_EVENTS} 个事件", seed["events_created"] == TOTAL_EVENTS, seed)

    # ---------- 1. 任意时点资格 ----------
    # Case1: 2026-02-28 当时可见：误合并仍有效 -> 簇{A,B}，v1 阈值 1000，1800 -> GOLD
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_OF", "2026-02-28T23:59:00Z")
    check("Case1 A@02-28 当时可见 = GOLD", r["tier"] == "GOLD", r)
    check("Case1 簇含 B（合并生效中）", set(r["cluster"]) == {A, B}, r["cluster"])
    check("Case1 消费 1800（含 B 的 600）", r["total_spend"] == 1800.0, r["total_spend"])

    # Case2: 同一业务时点、事后补全：拆分追溯生效 -> 簇{A}；v2 追溯(1500)适用 S001/ONLINE；
    #        v2 对 S002 被部分撤回 -> S002 用 v1。1280 / 350 -> SILVER
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("Case2 A@02-28 事后补全 = SILVER", r["tier"] == "SILVER", r)
    check("Case2 簇仅 A（误合并已追溯拆分）", r["cluster"] == [A], r["cluster"])
    vers = {s["rule_version"]: s["spend"] for s in r["scopes"]}
    check("Case2 v2 范围 1280 / v1(S002 撤回后) 350", vers.get(2) == 1280.0 and vers.get(1) == 350.0, vers)

    # Case0: 2026-01-31 当时可见 = SILVER(900)；事后补全因离线补传 T4(01-28,350) 变 GOLD(1250)
    r = elig(token, A, "2026-01-31T23:59:00Z", "AS_OF", "2026-01-31T23:59:00Z")
    check("Case0 A@01-31 当时可见 = SILVER", r["tier"] == "SILVER" and r["total_spend"] == 900.0, r)
    r = elig(token, A, "2026-01-31T23:59:00Z", "AS_KNOWN_NOW")
    check("Case0b A@01-31 事后补全 = GOLD（离线补传改判）", r["tier"] == "GOLD" and r["total_spend"] == 1250.0, r)

    # Case3/4: 2026-03-31 两个口径均 GOLD，但消费构成不同（2230 vs 1630）
    r = elig(token, A, "2026-03-31T23:59:00Z", "AS_OF", "2026-03-31T23:59:00Z")
    check("Case3 A@03-31 当时可见 = GOLD 2230（合并中+更正后+补传）", r["tier"] == "GOLD" and r["total_spend"] == 2230.0, r)
    r = elig(token, A, "2026-03-31T23:59:00Z", "AS_KNOWN_NOW")
    check("Case4 A@03-31 事后补全 = GOLD 1630（拆分后）", r["tier"] == "GOLD" and r["total_spend"] == 1630.0, r)

    # 更正链：T3 在 03-05 前为 300，之后为 380
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_OF", "2026-03-04T23:59:00Z")
    check("更正前口径含 T3=300（总 1800）", r["total_spend"] == 1800.0, r["total_spend"])
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_OF", "2026-03-06T00:00:00Z")
    check("更正后口径含 T3=380（总 1880）", r["total_spend"] == 1880.0, r["total_spend"])
    check("更正留痕 replaced_events", len(r["replaced_events"]) == 1, r["replaced_events"])

    # 其他会员：B 拆分后回落；D 被撤回保护；F 被规则修订击中；G 被撤回保护
    r = elig(token, B, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("B@02-28 事后补全 = SILVER 600（拆出后独立）", r["tier"] == "SILVER" and r["total_spend"] == 600.0, r)
    r = elig(token, D, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("D@02-28 事后补全 = GOLD 1100（S002 撤回 v2 保护）", r["tier"] == "GOLD" and r["total_spend"] == 1100.0, r)
    r = elig(token, F, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("F@02-28 事后补全 = SILVER（v2 阈值 1500 击中）", r["tier"] == "SILVER", r)
    r = elig(token, G, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("G@02-28 事后补全 = GOLD 1400（撤回保护，否则 SILVER）", r["tier"] == "GOLD", r)

    # DELTA 语义
    r = elig(token, A, "2026-02-28T23:59:00Z", "DELTA", "2026-02-28T23:59:00Z")
    check("DELTA: A@02-28 两种口径 GOLD->SILVER", r["as_of"]["tier"] == "GOLD" and r["as_known_now"]["tier"] == "SILVER" and r["tier_changed"], r)

    # ---------- 2. 发布库 + 差异归因 + 影响索引 ----------
    p1 = call("POST", "/api/publications", {"name": "2026-02月末·当时可见", "biz_as_of": "2026-02-28T23:59:00Z", "sys_as_of": "2026-02-28T23:59:00Z", "semantics_code": "AS_OF"}, token)
    pmid = call("POST", "/api/publications", {"name": "2026-02月末·3月末水位", "biz_as_of": "2026-02-28T23:59:00Z", "sys_as_of": "2026-03-31T23:59:00Z", "semantics_code": "AS_OF"}, token)
    p2 = call("POST", "/api/publications", {"name": "2026-02月末·事后补全", "biz_as_of": "2026-02-28T23:59:00Z", "sys_as_of": "2026-11-22T00:00:00Z", "semantics_code": "AS_KNOWN_NOW"}, token)
    check("发布 P1 冻结证据水位=39（2月末可见最大事件id）", p1["watermark_event_id"] == 39, p1)
    check("发布 P2 冻结规则集合版本>0 且身份图版本>0", p2["rule_set_version"] > 0 and p2["identity_graph_version"] > 0, p2)

    d_full = call("POST", "/api/diffs", {"pub_a": p1["id"], "pub_b": p2["id"]}, token)
    d_mid = call("POST", "/api/diffs", {"pub_a": pmid["id"], "pub_b": p2["id"]}, token)
    diff = call("GET", f"/api/diffs/{d_full['id']}", token=token)
    items = {i["member_key"]: i for i in diff["items"]}
    check("全量差异含 A 的降级记录", A in items and items[A]["before"]["tier"] == "GOLD" and items[A]["after"]["tier"] == "SILVER", items.get(A))
    causes_a = {c["cause"] for c in items[A]["causes"]} if A in items else set()
    check("A 的归因覆盖：迟到交易/更正/身份拆分/规则修订",
          {"LATE_TRANSACTION", "CORRECTION", "IDENTITY_SPLIT", "RULE_REVISION"} <= causes_a, causes_a)
    check("B 的归因含身份拆分", any(c["cause"] == "IDENTITY_SPLIT" for c in items.get(B, {}).get("causes", [])), items.get(B))
    check("F 的归因含规则修订", any(c["cause"] == "RULE_REVISION" for c in items.get(F, {}).get("causes", [])), items.get(F))

    diff_mid = call("GET", f"/api/diffs/{d_mid['id']}", token=token)
    items_mid = {i["member_key"]: i for i in diff_mid["items"]}
    check("中段差异：D 因撤回由 SILVER 升回 GOLD",
          D in items_mid and items_mid[D]["before"]["tier"] == "SILVER" and items_mid[D]["after"]["tier"] == "GOLD", items_mid.get(D))
    check("D 的归因含规则撤回", any(c["cause"] == "RULE_WITHDRAWAL" for c in items_mid.get(D, {}).get("causes", [])), items_mid.get(D))
    check("中段差异：A 因拆分降级", A in items_mid and items_mid[A]["after"]["tier"] == "SILVER", items_mid.get(A))

    # 影响索引
    impacts = diff["impacts"]
    mk = [i for i in impacts if i["dimension"] == "marketing" and i["member_key"] == A]
    bg = [i for i in impacts if i["dimension"] == "budget" and i["member_key"] == A]
    tr = [i for i in impacts if i["dimension"] == "training" and i["member_key"] == A]
    check("影响：A 退出 GOLD 营销名单、进入 SILVER 名单",
          any(i["entity"] == "marketing:list:GOLD" for i in mk) and any(i["entity"] == "marketing:list:SILVER" for i in mk), mk)
    check("影响：A 的门店预算条目（S001/S002/ONLINE）", len(bg) >= 2, bg)
    check("影响：A 的 2026-02 训练样本标签重述 GOLD->SILVER",
          any(i["change_type"] == "LABEL_RESTATED" and i["entity"] == "training:samples:2026-02" for i in tr), tr)
    check("影响条目携带归因事件 id", all(i["attributed_event_ids"] for i in mk + tr), mk + tr)
    ent = call("GET", "/api/impacts?entity=marketing:list:GOLD", token=token)
    check("影响索引可按下游实体反查", len(ent) >= 1, ent)

    # ---------- 3. 账本完整性 / 追加-only / 幂等 / 投影重建 ----------
    v = call("GET", "/api/admin/ledger/verify", token=token)
    check("链式哈希完整", v["ok"] and v["checked"] == TOTAL_EVENTS, v)

    dup = call("POST", "/api/events", {"source": "pos", "event_type": "transaction", "biz_time": "2026-01-05T10:20:00Z",
                                       "payload": {"member_key": A, "store": "S001", "category": "OTC", "amount": 500},
                                       "idempotency_key": "seed-t1"}, token)
    check("幂等键重放不重复入账", dup["created"] is False, dup)

    # 追加-only：直接 UPDATE/DELETE events 应被触发器拒绝（本机有 psql 时验证）
    psql = shutil.which("psql") or "/opt/postgresql-18/bin/psql"
    if os.path.exists(psql):
        sql = "UPDATE events SET actor='hacker' WHERE id=1;"
        r1 = subprocess.run([psql, "postgresql://ccdev:ccdev@127.0.0.1:5432/member_bitemp", "-c", sql],
                            capture_output=True, text=True,
                            env={"PGPASSWORD": "ccdev", "PATH": "/usr/bin:/bin"})
        check("追加-only：UPDATE 被拒绝", "追加式" in (r1.stderr + r1.stdout) or r1.returncode != 0, r1.stderr[:200])
    else:
        print("SKIP  追加-only 触发器直查（无 psql，容器内验证见 README）")

    rb = call("POST", "/api/admin/rebuild", token=token)
    check(f"投影重建（{TOTAL_EVENTS} 事件重放）", rb["events_replayed"] == TOTAL_EVENTS and rb["identity_edges"] >= 4 and rb["rule_versions"] == 3, rb)
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("重建后资格结论一致（SILVER）", r["tier"] == "SILVER" and r["total_spend"] == 1630.0, r)

    # 并发修订解决：v2/v3 同链修订 v1，03-15 应适用 v3
    r = elig(token, F, "2026-03-15T23:59:00Z", "AS_KNOWN_NOW")
    check("并发修订确定性解决：03-15 适用 v3(1300)，F 1200 -> SILVER",
          r["tier"] == "SILVER" and all(s["rule_version"] == 3 for s in r["scopes"]), r["scopes"])

    # ==================================================================
    # 4. 门店归属历史与权益成本归因
    # ==================================================================
    def preview(member, period, sys_t, strategy=None):
        body = {"member_key": member, "period_key": period, "sys_time": sys_t}
        if strategy:
            body["strategy"] = strategy
        return call("POST", "/api/attribution/preview", body, token)

    def lines(run_id, **params):
        q = f"/api/attribution/runs/{run_id}/lines"
        if params:
            q += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        return call("GET", q, token=token)

    # ---- 4.0 投影重建包含新域表 ----
    check("重建后服务关系 15 条、组织区间 8 条、预算 2 个、成本 6 条",
          rb["service_relations"] == 15 and rb["org_intervals"] == 8
          and rb["budget_periods"] == 2 and rb["benefit_costs"] == 6, rb)

    # ---- 4.1 跨店服务：M1 五家族齐全，权重分摊 0.55/0.30/0.15 ----
    r = preview(M1, "2026-02", "2026-03-31T23:59:00Z")
    shares = {a["store_key"]: a["share"] for a in r["assignments"]}
    check("M1 自动选择分摊策略且覆盖率 1.00",
          r["strategy"] == "apportion" and r["status"] == "allocated" and r["coverage"] == 1.0, r)
    check("M1 份额 S101=0.55 / S102=0.30 / ONLINE=0.15",
          shares == {"S101": 0.55, "S102": 0.30, "ONLINE": 0.15}, shares)

    # ---- 4.2 证据冲突：M2 会员选择与履约同等级异店 -> 待裁定，不任选 ----
    r = preview(M2, "2026-02", "2026-03-31T23:59:00Z")
    check("M2 标记待裁定", r["status"] == "pending_adjudication", r["status"])
    check("M2 冲突证据指向 S101/S102 且不输出归属",
          {c["resolved_store"] for c in r["conflicts"]} == {"S101", "S102"} and not r["assignments"],
          r.get("conflicts"))

    # ---- 4.3 期中并购：M3 服务发生日跨 3-15，按日拆分，禁止月末组织树回填 ----
    r = preview(M3, "2026-03", "2026-04-01T23:59:00Z")
    resolve = {e["biz_time"][:10]: e["resolved_store"] for e in r["evidence"]}
    check("并购前 03-05 留 S103、并购后 03-20 继承 S104",
          resolve.get("2026-03-05") == "S103" and resolve.get("2026-03-20") == "S104", resolve)
    r2 = preview(M3, "2026-03", "2026-04-01T23:59:00Z", strategy="apportion")
    check("不允许分摊的预算强制回退单店策略",
          r2["strategy"] == "single_store" and "回退" in r2["strategy_reason"], r2["strategy_reason"])

    # ---- 4.4 缺证据未分配范围：M4 覆盖率 0.45，缺口 0.55 不摊给现有门店 ----
    r = preview(M4, "2026-02", "2026-03-31T23:59:00Z")
    check("M4 可分摊但覆盖率仅 0.45，缺失三个证据家族",
          r["coverage"] == 0.45 and set(r["missing_families"]) ==
          {"member_choice", "prescription", "store_confirmation"}, r)

    # ---- 4.5 结算 2026-02 并锁定：守恒、残差、未分配、待裁定同时落账 ----
    run1 = call("POST", "/api/attribution/runs",
                {"period_key": "2026-02", "sys_time": "2026-03-31T23:59:00Z",
                 "lock": True, "note": "2月末锁定结算"}, token)
    ls1 = lines(run1["id"])
    check("结算守恒：行金额合计=总成本 498.88",
          abs(sum(l["amount"] for l in ls1) - run1["total_cost"]) < 0.005
          and run1["total_cost"] == 498.88, run1)
    check("结算单冻结组织版本/规则版本/证据水位",
          run1["org_graph_version"] > 0 and run1["rule_set_version"] > 0
          and run1["watermark_event_id"] > 0, run1)
    # M1 生日券 100 按 0.55/0.30/0.15 量化
    m1_bd = {l["store_key"]: l["amount"] for l in ls1
             if l["member_key"] == M1 and l["benefit_type"] == "birthday_coupon" and l["status"] == "allocated"}
    check("M1 生日券分摊 55/30/15", m1_bd == {"S101": 55.0, "S102": 30.0, "ONLINE": 15.0}, m1_bd)
    # M1 等级权益 88.88 产生 0.01 小数残差，归入份额最大门店 S101，单独成行
    resid = [l for l in ls1 if l["status"] == "residual"]
    check("分摊残差 0.01 单独成行并归入 S101",
          len(resid) == 1 and resid[0]["amount"] == 0.01 and resid[0]["store_key"] == "S101", resid)
    m1_tier_alloc = sum(l["amount"] for l in ls1
                        if l["member_key"] == M1 and l["benefit_type"] == "tier")
    check("残差后 M1 等级权益仍守恒 88.88", abs(m1_tier_alloc - 88.88) < 0.005, m1_tier_alloc)
    # M4 未分配范围 = 200 * 0.55 = 110，缺口不平均摊给现有门店
    m4_un = [l for l in ls1 if l["member_key"] == M4 and l["status"] == "unallocated"]
    check("M4 未分配范围 110（200×0.55），不摊给现有门店",
          len(m4_un) == 1 and m4_un[0]["amount"] == 110.0 and m4_un[0]["store_key"] is None, m4_un)
    # M2 待裁定：整额挂起，不任选门店
    m2_pending = [l for l in ls1 if l["member_key"] == M2]
    check("M2 成本 60 整额待裁定、无门店",
          len(m2_pending) == 1 and m2_pending[0]["status"] == "pending_adjudication"
          and m2_pending[0]["amount"] == 60.0 and m2_pending[0]["store_key"] is None, m2_pending)
    # 已锁定预算拒绝直接重算
    try:
        call("POST", "/api/attribution/runs",
             {"period_key": "2026-02", "sys_time": "2026-04-30T23:59:00Z"}, token)
        check("锁定预算拒绝直接重算", False, "未拒绝")
    except urllib.error.HTTPError as e:
        check("锁定预算拒绝直接重算（须走调整版本）", e.code == 400 and "锁定" in e.read().decode(), e.code)

    # ---- 4.6 3 月预算结算：M3 单店归 S104（最高等级门店选择）----
    run3 = call("POST", "/api/attribution/runs",
                {"period_key": "2026-03", "sys_time": "2026-04-01T23:59:00Z"}, token)
    ls3 = lines(run3["id"])
    m3_lines = [l for l in ls3 if l["member_key"] == M3]
    check("M3 权益 120 整额归入并购存续店 S104",
          len(m3_lines) == 1 and m3_lines[0]["store_key"] == "S104"
          and m3_lines[0]["amount"] == 120.0 and m3_lines[0]["status"] == "allocated", m3_lines)

    # ---- 4.7 迟到服务 + 组织修订冲击已锁定预算：保留原结算，签发调整版本 ----
    sug = call("POST", "/api/settlement/suggestions", {"sys_time": "2026-04-30T23:59:00Z"}, token)
    sug202602 = next((s for s in sug if s["period_key"] == "2026-02"), None)
    check("差异发布器定位 2026-02 且原因含迟到证据与组织修订",
          sug202602 is not None and set(sug202602["reason_kinds"]) ==
          {"late_evidence", "org_revision"} and len(sug202602["trigger_event_ids"]) >= 3, sug202602)
    adj = call("POST", "/api/settlement/adjustments",
               {"period_key": "2026-02", "sys_time": "2026-04-30T23:59:00Z",
                "note": "迟到处方服务与追溯区域修订"}, token)
    check("调整版本指向原结算且携带触发事件",
          adj["original_run_id"] == run1["id"] and adj["adjustment_run_id"] != run1["id"]
          and adj["trigger_event_ids"], adj)
    orig = call("GET", f"/api/attribution/runs/{run1['id']}", token=token)
    new = call("GET", f"/api/attribution/runs/{adj['adjustment_run_id']}", token=token)
    check("原结算保留不动（superseded 标记，13 行不变）",
          orig["status"] == "superseded" and len(orig["lines"]) == len(ls1),
          (orig["status"], len(orig["lines"])))
    check("调整差额总和为 0（成本守恒，只在门店/未分配桶间移动）",
          abs(adj["delta_amount"]) < 0.005, adj["delta_amount"])
    d_s102 = next(x for x in adj["detail"] if x["store_or_bucket"] == "S102")
    d_un = next(x for x in adj["detail"] if x["store_or_bucket"] == "UNALLOC:unallocated")
    check("迟到处方把 15 元从未分配桶移入 S102",
          d_s102["delta"] == 15.0 and d_un["delta"] == -15.0, adj["detail"])
    m5_new = [l for l in new["lines"] if l["member_key"] == M5]
    check("调整版 M5 新增 S102 归属 15 元",
          any(l["store_key"] == "S102" and l["amount"] == 15.0 for l in m5_new), m5_new)

    # ---- 4.8 回查链路：任一门店成本 -> 服务关系/组织版本/归属策略/预算版本 ----
    s101_line = lines(new["id"], store="S101", status="allocated")[0]
    trace = call("GET", f"/api/attribution/lines/{s101_line['id']}/trace", token=token)
    check("回查含冻结的归属策略与三类版本号",
          trace["frozen_versions"]["strategy"] in ("apportion", "mixed")
          and trace["frozen_versions"]["org_graph_version"] > 0
          and trace["frozen_versions"]["rule_set_version"] > 0
          and trace["frozen_versions"]["watermark_event_id"] > 0, trace["frozen_versions"])
    check("回查含服务关系证据（类型/门店/等级/状态）",
          len(trace["service_relations"]) >= 1
          and all(r["evidence_grade"] and r["status"] for r in trace["service_relations"]),
          trace["service_relations"])
    check("回查含冻结组织沿革区间", len(trace["org_intervals_frozen"]) >= 1,
          trace["org_intervals_frozen"])
    check("回查含预算周期与权益成本规则版本",
          trace["budget_period"]["period_key"] == "2026-02"
          and trace["benefit_cost"]["rule_version_tag"] == "benefit-rule-v1", trace)

    # ---- 4.9 服务证据撤销留痕：撤销后覆盖率与份额重算，历史行不删除 ----
    rels = call("GET", f"/api/service-relations?member={M1}", token=token)
    choice_ev = next(r for r in rels if r["relation_type"] == "member_choice" and r["status"] == "active")
    call("POST", "/api/service-relations/revoke",
         {"target_event_id": choice_ev["event_id"], "biz_time": "2026-04-15T10:00:00Z",
          "ingested_at": "2026-04-15T10:05:00Z",
          "reason": "会员改选门店，撤销原选择证据"}, token)
    rels2 = call("GET", f"/api/service-relations?member={M1}", token=token)
    revoked = [r for r in rels2 if r["event_id"] == choice_ev["event_id"]][0]
    check("撤销只置 revoked 并记录撤销事件，不删除历史行",
          revoked["status"] == "revoked" and revoked["revoke_event_id"] is not None, revoked)
    r = preview(M1, "2026-02", "2026-04-30T23:59:00Z")
    shares = {a["store_key"]: a["share"] for a in r["assignments"]}
    check("撤销后覆盖率降至 0.80，S101 份额由 0.55 降至 0.35",
          r["coverage"] == 0.8 and shares.get("S101") == 0.35
          and shares.get("S102") == 0.30 and shares.get("ONLINE") == 0.15, shares)

    # ---- 4.10 结算后重建投影：冻结结算保留，逻辑引用与回查链路仍有效 ----
    trace_before = call("GET", f"/api/attribution/lines/{s101_line['id']}/trace", token=token)
    rb2 = call("POST", "/api/admin/rebuild", token=token)
    check("重建仅重放可重放投影（49 种子+1 撤销=50 事件 / 15 服务关系 / 6 成本）",
          rb2["events_replayed"] == TOTAL_EVENTS + 1 and rb2["service_relations"] == 15
          and rb2["benefit_costs"] == 6, rb2)
    runs_after = call("GET", "/api/attribution/runs?period=2026-02", token=token)
    adj_after = call("GET", "/api/settlement/adjustments", token=token)
    check("冻结结算单与调整版本在重建后保留",
          len(runs_after) == 2 and len(adj_after) == 1
          and any(x["id"] == adj["adjustment_run_id"] for x in runs_after),
          (len(runs_after), len(adj_after)))
    trace_after = call("GET", f"/api/attribution/lines/{s101_line['id']}/trace", token=token)
    check("重建后回查链路有效且成本引用一致",
          trace_after["benefit_cost"]["id"] == trace_before["benefit_cost"]["id"]
          and trace_after["benefit_cost"]["amount"] == trace_before["benefit_cost"]["amount"]
          and len(trace_after["service_relations"]) == len(trace_before["service_relations"])
          and trace_after["frozen_versions"] == trace_before["frozen_versions"],
          trace_after.get("benefit_cost"))
    r = preview(M1, "2026-02", "2026-04-30T23:59:00Z")
    check("重建后归属结论一致（撤销仍生效，覆盖率 0.80）", r["coverage"] == 0.8, r["coverage"])

    print(f"\n== 验证完成: {passed} 通过, {failed} 失败 ==")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
