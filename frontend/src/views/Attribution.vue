<template>
  <div>
    <h2>会员门店归属 · 权益成本归因 · 结算调整</h2>

    <!-- 归属计算器 -->
    <div class="card">
      <h3>归属计算器（指定时点的服务门店 / 服务份额，试算不落库）</h3>
      <div class="form-row">
        <label>会员</label>
        <select v-model="pv.member_key">
          <option v-for="m in members" :key="m.member_key" :value="m.member_key">{{ m.member_key }} {{ m.display_name || '' }}</option>
        </select>
        <label>预算期</label>
        <select v-model="pv.period_key">
          <option v-for="b in budgets" :key="b.period_key" :value="b.period_key">
            {{ b.period_key }}{{ b.allow_apportion ? '（允许分摊）' : '（单店）' }}
          </option>
        </select>
        <label>系统时点</label>
        <input v-model="pv.sys_time" style="width:190px" placeholder="留空=当前" />
        <label>策略</label>
        <select v-model="pv.strategy">
          <option :value="null">自动选择</option>
          <option value="single_store">单店最高证据</option>
          <option value="apportion">权重分摊</option>
        </select>
        <button @click="preview">试算</button>
      </div>
      <template v-if="result">
        <p>
          <span class="tag">{{ result.strategy }}</span>
          <span :class="['tag', result.status === 'allocated' ? 'ok' : 'bad']">{{ result.status }}</span>
          覆盖率 <b>{{ result.coverage }}</b>
          <span class="muted" v-if="result.missing_families.length">缺失：{{ result.missing_families.join(', ') }}</span>
        </p>
        <p class="muted">{{ result.strategy_reason }} {{ result.reason || '' }}</p>
        <table v-if="result.assignments.length">
          <thead><tr><th>服务门店</th><th>区域</th><th>份额</th><th>证据家族权重</th><th>证据事件</th></tr></thead>
          <tbody>
            <tr v-for="a in result.assignments" :key="a.store_key">
              <td class="mono">{{ a.store_key }}</td>
              <td>{{ a.region_code || '—' }}</td>
              <td class="mono">{{ a.share }}</td>
              <td class="mono">{{ fmtObj(a.by_family) }}</td>
              <td class="mono">{{ a.evidence_event_ids.map(e => '#' + e).join(' ') }}</td>
            </tr>
          </tbody>
        </table>
        <table v-if="result.conflicts.length">
          <thead><tr><th>冲突证据</th><th>类型</th><th>等级</th><th>业务时间</th><th>解析门店</th></tr></thead>
          <tbody>
            <tr v-for="c in result.conflicts" :key="c.event_id">
              <td class="mono">#{{ c.event_id }}</td><td>{{ c.relation_type }}</td>
              <td class="mono">{{ c.evidence_grade }}</td>
              <td class="mono">{{ c.biz_time.slice(0, 16).replace('T', ' ') }}</td>
              <td class="mono">{{ c.resolved_store }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </div>

    <!-- 预算周期与权益成本 -->
    <div class="grid cols-2">
      <div class="card">
        <h3>预算周期（分摊口径 / 精度 / 覆盖率门槛）</h3>
        <table>
          <thead><tr><th>周期</th><th>业务区间</th><th>分摊</th><th>精度</th><th>覆盖率门槛</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="b in budgets" :key="b.period_key">
              <td class="mono">{{ b.period_key }}</td>
              <td class="mono">{{ b.biz_from.slice(0, 10) }} → {{ b.biz_to.slice(0, 10) }}</td>
              <td>{{ b.allow_apportion ? '允许' : '单店' }}</td>
              <td>{{ b.precision_cents }} 位</td>
              <td class="mono">{{ b.coverage_threshold }}</td>
              <td><span :class="['tag', b.status === 'locked' ? 'bad' : 'ok']">{{ b.status }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="card">
        <h3>权益成本台账（等级 / 积分 / 生日券）</h3>
        <table>
          <thead><tr><th>周期</th><th>会员</th><th>权益</th><th>金额</th><th>规则版本</th></tr></thead>
          <tbody>
            <tr v-for="c in costs" :key="c.id">
              <td class="mono">{{ c.period_key }}</td>
              <td class="mono">{{ c.member_key }}</td>
              <td>{{ c.benefit_type }}</td>
              <td>¥{{ c.amount }}</td>
              <td class="mono">{{ c.rule_version_tag }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 成本归因结算 -->
    <div class="card">
      <h3>成本归因器（结算单：策略与组织版本冻结于此）</h3>
      <div class="form-row">
        <label>预算期</label>
        <select v-model="rv.period_key" style="width:120px">
          <option v-for="b in budgets" :key="b.period_key" :value="b.period_key">{{ b.period_key }}</option>
        </select>
        <label>系统时点</label><input v-model="rv.sys_time" style="width:190px" />
        <label>策略</label>
        <select v-model="rv.strategy">
          <option :value="null">自动</option><option value="single_store">单店</option><option value="apportion">分摊</option>
        </select>
        <label><input type="checkbox" v-model="rv.lock" /> 结算并锁定</label>
        <button @click="createRun">生成结算单</button>
      </div>
      <table>
        <thead><tr><th>id</th><th>周期</th><th>策略</th><th>系统水位</th><th>组织版本</th><th>规则版本</th><th>证据水位</th><th>总成本</th><th>已归属</th><th>未分配</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td class="mono">#{{ r.id }}</td><td class="mono">{{ r.period_key }}</td>
            <td><span class="tag">{{ r.strategy }}</span></td>
            <td class="mono">{{ r.sys_as_of.slice(0, 10) }}</td>
            <td class="mono">org#{{ r.org_graph_version }}</td>
            <td class="mono">rule#{{ r.rule_set_version }}</td>
            <td class="mono">ev#{{ r.watermark_event_id }}</td>
            <td>¥{{ r.total_cost }}</td><td>¥{{ r.allocated_cost }}</td><td>¥{{ r.unallocated_cost }}</td>
            <td><span :class="['tag', r.status === 'superseded' ? 'bad' : 'ok']">{{ r.status }}</span></td>
            <td><button class="ghost" @click="openRun(r.id)">明细/回查</button></td>
          </tr>
        </tbody>
      </table>

      <template v-if="runDetail">
        <h4 style="margin-top:14px">结算单 #{{ runDetail.id }} 归属行（点击任一行回查完整链路）</h4>
        <table>
          <thead><tr><th>会员</th><th>权益</th><th>状态</th><th>门店</th><th>区域</th><th>金额</th><th>份额</th><th>规则版本</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="l in runDetail.lines" :key="l.id" style="cursor:pointer" @click="traceLine(l.id)">
              <td class="mono">{{ l.member_key }}</td><td>{{ l.benefit_type }}</td>
              <td><span :class="['tag', lineStatusClass(l.status)]">{{ l.status }}</span></td>
              <td class="mono">{{ l.store_key || '—' }}</td><td>{{ l.region_code || '—' }}</td>
              <td>¥{{ l.amount }}</td><td class="mono">{{ l.share }}</td>
              <td class="mono">{{ l.rule_version_tag }}</td><td class="muted">{{ l.reason || '' }}</td>
            </tr>
          </tbody>
        </table>
      </template>

      <template v-if="trace">
        <h4 style="margin-top:14px">回查链路：门店成本 → 服务关系 → 组织版本 → 归属策略 → 预算版本</h4>
        <p class="mono">
          冻结版本：strategy={{ trace.frozen_versions.strategy }}，org_graph#{{ trace.frozen_versions.org_graph_version }}，
          rule_set#{{ trace.frozen_versions.rule_set_version }}，watermark_ev#{{ trace.frozen_versions.watermark_event_id }}
        </p>
        <div class="grid cols-2">
          <div>
            <b>服务关系证据</b>
            <table>
              <thead><tr><th>事件</th><th>类型</th><th>门店</th><th>等级</th><th>状态</th></tr></thead>
              <tbody>
                <tr v-for="r in trace.service_relations" :key="r.event_id">
                  <td class="mono">#{{ r.event_id }}</td><td>{{ r.relation_type }}</td>
                  <td class="mono">{{ r.store_key }}</td><td class="mono">{{ r.evidence_grade }}</td>
                  <td><span :class="['tag', r.status === 'active' ? 'ok' : 'bad']">{{ r.status }}</span></td>
                </tr>
                <tr v-if="!trace.service_relations.length"><td colspan="5" class="muted">该行无门店证据（未分配/待裁定桶）</td></tr>
              </tbody>
            </table>
          </div>
          <div>
            <b>冻结组织沿革区间（org_graph#{{ trace.frozen_versions.org_graph_version }} 以内）</b>
            <table>
              <thead><tr><th>事件</th><th>门店</th><th>变更</th><th>区域</th><th>业务区间</th><th>状态</th></tr></thead>
              <tbody>
                <tr v-for="o in trace.org_intervals_frozen" :key="o.event_id + o.store_key">
                  <td class="mono">#{{ o.event_id }}</td><td class="mono">{{ o.store_key }}</td>
                  <td>{{ o.change }}</td><td>{{ o.region_code || '—' }}</td>
                  <td class="mono">{{ o.biz_from.slice(0, 10) }} → {{ o.biz_to ? o.biz_to.slice(0, 10) : '开放' }}</td>
                  <td>{{ o.status }}</td>
                </tr>
                <tr v-if="!trace.org_intervals_frozen.length"><td colspan="6" class="muted">线上渠道无组织区间</td></tr>
              </tbody>
            </table>
          </div>
        </div>
        <p class="muted">
          预算周期 {{ trace.budget_period.period_key }}（{{ trace.budget_period.status }}，分摊={{ trace.budget_period.allow_apportion }}）；
          权益成本 #{{ trace.benefit_cost?.id }} ¥{{ trace.benefit_cost?.amount }}，规则版本 {{ trace.benefit_cost?.rule_version_tag }}
        </p>
      </template>
    </div>

    <!-- 结算差异 -->
    <div class="card">
      <h3>结算差异发布器（组织修订 / 迟到服务证据 → 调整建议与调整版本）</h3>
      <div class="form-row">
        <label>系统时点</label><input v-model="sv.sys_time" style="width:200px" />
        <button class="ghost" @click="loadSuggestions">定位受影响预算</button>
        <button @click="issueAdjustment">为 2026-02 签发调整版本</button>
      </div>
      <table v-if="suggestions.length">
        <thead><tr><th>预算期</th><th>原结算</th><th>原水位</th><th>触发事件</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="s in suggestions" :key="s.period_key">
            <td class="mono">{{ s.period_key }}</td><td class="mono">#{{ s.original_run_id }}</td>
            <td class="mono">{{ s.original_sys_as_of.slice(0, 10) }}</td>
            <td class="mono">{{ s.trigger_event_ids.map(e => '#' + e).join(' ') }}</td>
            <td><span v-for="k in s.reason_kinds" :key="k" class="tag" style="margin-right:4px">{{ k }}</span></td>
          </tr>
        </tbody>
      </table>
      <h4 v-if="adjustments.length" style="margin-top:12px">已签发调整版本（原结算保留，差额守恒）</h4>
      <table v-if="adjustments.length">
        <thead><tr><th>id</th><th>周期</th><th>原结算</th><th>调整结算</th><th>原因</th><th>总差额</th><th>门店/桶差额</th></tr></thead>
        <tbody>
          <tr v-for="a in adjustments" :key="a.id">
            <td class="mono">#{{ a.id }}</td><td class="mono">{{ a.period_key }}</td>
            <td class="mono">#{{ a.original_run_id }} → #{{ a.adjustment_run_id }}</td>
            <td><span class="tag">{{ a.reason_kind }}</span></td>
            <td :class="a.delta_amount === 0 ? 'muted' : ''">¥{{ a.delta_amount }}</td>
            <td class="mono">
              <span v-for="d in a.detail" :key="d.store_or_bucket" style="margin-right:8px">
                {{ d.store_or_bucket }}: {{ d.delta > 0 ? '+' : '' }}{{ d.delta }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 服务关系账 -->
    <div class="card">
      <h3>服务关系账（五类证据 · 双时间 · 证据等级 · 撤销留痕）</h3>
      <div class="form-row">
        <label>会员过滤</label>
        <select v-model="relFilter" @change="loadRelations">
          <option value="">全部</option>
          <option v-for="m in members" :key="m.member_key" :value="m.member_key">{{ m.member_key }}</option>
        </select>
      </div>
      <table>
        <thead><tr><th>事件</th><th>会员</th><th>门店</th><th>关系类型</th><th>等级</th><th>业务时间</th><th>到达时间</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <tr v-for="r in relations" :key="r.id">
            <td class="mono">#{{ r.event_id }}</td><td class="mono">{{ r.member_key }}</td>
            <td class="mono">{{ r.store_key }}</td><td>{{ r.relation_type }}</td>
            <td class="mono">{{ r.evidence_grade }}</td>
            <td class="mono">{{ r.biz_time.slice(0, 16).replace('T', ' ') }}</td>
            <td class="mono">{{ r.ingested_at.slice(0, 10) }}</td>
            <td><span :class="['tag', r.status === 'active' ? 'ok' : 'bad']">{{ r.status }}</span></td>
            <td><button v-if="r.status === 'active'" class="ghost" @click="revoke(r)">撤销</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 组织沿革图 -->
    <div class="card">
      <h3>组织沿革图（开闭 / 加盟 / 区域调整 / 合并拆分的双时间有效区间）</h3>
      <div class="form-row">
        <label>门店</label><input v-model="orgForm.store_key" style="width:110px" placeholder="S103" />
        <label>业务时点</label><input v-model="orgForm.biz_time" style="width:190px" placeholder="2026-03-20T14:00:00Z" />
        <label>系统时点</label><input v-model="orgForm.sys_time" style="width:190px" placeholder="留空=当前" />
        <button class="ghost" @click="resolveOrg">按服务日解析组织节点</button>
      </div>
      <p v-if="orgResolve" class="mono">
        解析结果：{{ orgForm.store_key }} → <b>{{ orgResolve.store_key }}</b>
        （{{ orgResolve.status }}，区域 {{ orgResolve.region_code || '—' }}）；
        继承链：{{ orgResolve.trail.map(t => t.store_key + ':' + t.status).join(' → ') }}
      </p>
      <table>
        <thead><tr><th>事件</th><th>门店</th><th>变更</th><th>加盟主体</th><th>区域</th><th>关联门店</th><th>业务区间</th><th>状态</th><th>事由</th></tr></thead>
        <tbody>
          <tr v-for="o in orgIntervals" :key="o.id">
            <td class="mono">#{{ o.event_id }}</td><td class="mono">{{ o.store_key }}</td>
            <td><span class="tag">{{ o.change }}</span></td><td>{{ o.owner_key || '—' }}</td>
            <td>{{ o.region_code || '—' }}</td><td class="mono">{{ o.target_store || '—' }}</td>
            <td class="mono">{{ o.biz_from.slice(0, 10) }} → {{ o.biz_to ? o.biz_to.slice(0, 10) : '开放' }}</td>
            <td>{{ o.status }}</td><td class="muted">{{ o.reason || '' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const members = ref([])
const budgets = ref([])
const costs = ref([])
const runs = ref([])
const runDetail = ref(null)
const trace = ref(null)
const suggestions = ref([])
const adjustments = ref([])
const relations = ref([])
const orgIntervals = ref([])
const result = ref(null)
const orgResolve = ref(null)
const relFilter = ref('')

const pv = ref({ member_key: '13810000001', period_key: '2026-02', sys_time: '2026-03-31T23:59:00Z', strategy: null })
const rv = ref({ period_key: '2026-02', sys_time: '2026-03-31T23:59:00Z', strategy: null, lock: true })
const sv = ref({ sys_time: '2026-04-30T23:59:00Z' })
const orgForm = ref({ store_key: 'S103', biz_time: '2026-03-20T14:00:00Z', sys_time: '2026-04-01T23:59:00Z' })

function fmtObj(o) { return o ? Object.entries(o).map(([k, v]) => `${k}=${v}`).join(', ') : '—' }
function lineStatusClass(s) {
  if (s === 'allocated') return 'ok'
  if (s === 'residual') return 'silver'
  return 'bad'
}

async function preview() {
  result.value = await api.post('/attribution/preview', { ...pv.value, strategy: pv.value.strategy || null })
}
async function createRun() {
  try {
    await api.post('/attribution/runs', { ...rv.value, strategy: rv.value.strategy || null })
    await loadRuns()
  } catch (e) { alert(e.message) }
}
async function loadRuns() { runs.value = await api.get('/attribution/runs') }
async function openRun(id) {
  runDetail.value = await api.get(`/attribution/runs/${id}`)
  trace.value = null
}
async function traceLine(id) { trace.value = await api.get(`/attribution/lines/${id}/trace`) }
async function loadSuggestions() {
  suggestions.value = await api.post('/settlement/suggestions', sv.value)
}
async function issueAdjustment() {
  try {
    await api.post('/settlement/adjustments', { period_key: '2026-02', ...sv.value })
    adjustments.value = await api.get('/settlement/adjustments')
    await loadRuns()
  } catch (e) { alert(e.message) }
}
async function loadRelations() {
  relations.value = await api.get(relFilter.value ? `/service-relations?member=${relFilter.value}` : '/service-relations')
}
async function revoke(r) {
  await api.post('/service-relations/revoke', {
    target_event_id: r.event_id, biz_time: r.biz_time,
    ingested_at: new Date().toISOString(), reason: '前端登记：服务证据撤销',
  })
  await loadRelations()
}
async function resolveOrg() {
  orgResolve.value = await api.post('/org/resolve', orgForm.value)
}

onMounted(async () => {
  members.value = await api.get('/members')
  budgets.value = await api.get('/budget-periods')
  costs.value = await api.get('/benefit-costs')
  orgIntervals.value = await api.get('/org/intervals')
  await Promise.all([preview(), loadRuns(), loadRelations(), loadSuggestions(), (adjustments.value = await api.get('/settlement/adjustments'))])
})
</script>
