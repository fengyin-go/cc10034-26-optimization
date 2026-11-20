<template>
  <section class="page" data-module="dim_snapshot">
    <header class="page-head">
      <div>
        <h2>维度快照与历史版本</h2>
        <p class="page-desc">
          SCD Type2 版本按生效时间维护，查询开启即固化快照目录；回溯更正幂等落库、区间不重叠，
          新查询按业务日期选版并与旧快照做字段级差异比对。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="reconcile" @click="loadReconcile">版本核对</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">已发布版本</span>
        <strong class="stat-value">{{ reconcileReport?.published_version_count ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">固化快照目录</span>
        <strong class="stat-value">{{ catalogs.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">最近比对差异项</span>
        <strong class="stat-value">{{ compareReport?.summary.difference_rows ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">版本核对结论</span>
        <strong class="stat-value" :class="reconcileReport?.consistent ? 'trend-up' : 'error-text'">
          {{ reconcileReport ? (reconcileReport.consistent ? '一致' : '不一致') : '待核对' }}
        </strong>
      </article>
    </div>

    <form class="analysis-form" @submit.prevent="openNewQuery">
      <label>
        维表
        <input v-model="dimTable" data-testid="dim-table" />
      </label>
      <label>
        业务日期
        <input v-model="businessDate" data-testid="business-date" type="date" />
      </label>
      <label>
        自然键 customer_id（可选）
        <input v-model="customerId" data-testid="customer-id" type="number" />
      </label>
      <label>
        查询备注
        <input v-model="queryNote" data-testid="query-note" placeholder="如：更正后对账" />
      </label>
      <button class="btn primary" data-testid="open-query" type="submit">开启新查询（固化目录）</button>
      <button class="btn" type="button" data-testid="as-of" @click="loadAsOf">仅 as-of 定位</button>
    </form>

    <p v-if="message" class="page-foot"><span :class="messageError ? 'error-text' : ''">{{ message }}</span></p>

    <!-- 当前快照 -->
    <h3>指定时刻快照（含维表、自然键、版本）</h3>
    <table class="data-table" data-testid="snapshot-table">
      <thead>
        <tr>
          <th>自然键</th>
          <th>版本</th>
          <th>生效区间 [from, to)</th>
          <th>地区/属性</th>
          <th>选版依据</th>
          <th>回溯更正</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in snapshotPage?.items ?? []" :key="`${row.natural_key}-${row.version_id}`">
          <td>{{ row.natural_key }}</td>
          <td>v{{ row.version_no }}（#{{ row.version_id }}）</td>
          <td>{{ row.effective_from }} → {{ row.effective_to }}</td>
          <td>
            <span v-for="(value, key) in row.attributes" :key="key" class="attr-chip">
              {{ key }}={{ value }}
            </span>
          </td>
          <td>{{ row.selection_basis }}</td>
          <td>
            <span :class="row.backdated_correction ? 'error-text' : ''">
              {{ row.backdated_correction ? '是（录入晚于业务日期）' : '否' }}
            </span>
          </td>
        </tr>
        <tr v-if="!(snapshotPage?.items.length)">
          <td colspan="6" class="empty-state">暂无快照数据，请先开启查询</td>
        </tr>
      </tbody>
    </table>
    <footer class="page-foot">
      <span>
        目录：{{ snapshotPage?.catalog?.catalog_id ?? '未固化' }}，
        共 {{ snapshotPage?.total ?? 0 }} 个自然键，第 {{ snapshotPage?.page ?? 1 }} 页
      </span>
      <span v-if="activeCatalogId" class="row-actions">
        <button class="link" type="button" :disabled="(snapshotPage?.page ?? 1) <= 1" @click="turnPage(-1)">上一页</button>
        <button class="link" type="button" @click="turnPage(1)">下一页（沿用同一目录）</button>
      </span>
    </footer>

    <!-- 历史版本 -->
    <h3>历史版本与有效区间</h3>
    <table class="data-table" data-testid="version-table">
      <thead>
        <tr>
          <th>版本</th>
          <th>生效区间</th>
          <th>状态</th>
          <th>来源</th>
          <th>更正</th>
          <th>差异回写</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="version in versions" :key="version.version_id">
          <td>v{{ version.version_no }}（#{{ version.version_id }}）</td>
          <td>{{ version.effective_from }} → {{ version.effective_to }}</td>
          <td>{{ version.status }}</td>
          <td>{{ version.source }}</td>
          <td>{{ version.is_correction ? '回溯更正' : '常规版本' }}</td>
          <td>{{ version.diff_marks.length }} 条</td>
        </tr>
        <tr v-if="!versions.length">
          <td colspan="6" class="empty-state">暂无版本，先选择 customer_id 后查询</td>
        </tr>
      </tbody>
    </table>

    <!-- 更正补交 -->
    <h3>补交地区更正（同来源幂等，区间不重叠）</h3>
    <form class="analysis-form" @submit.prevent="submitRegionCorrection">
      <label>
        生效日期
        <input v-model="correctionDate" data-testid="correction-date" type="date" />
      </label>
      <label>
        更正后地区
        <input v-model="correctionRegion" data-testid="correction-region" />
      </label>
      <label>
        更正来源
        <input v-model="correctionSource" data-testid="correction-source" />
      </label>
      <button class="btn primary" data-testid="submit-correction" type="submit">提交更正</button>
    </form>

    <!-- 快照比对 -->
    <h3>快照比对（差异回写维度记录，校验版本一致）</h3>
    <form class="analysis-form" @submit.prevent="compareCatalogs">
      <label>
        新查询目录
        <select v-model="compareNewCatalog" data-testid="compare-new">
          <option v-for="catalog in catalogs" :key="catalog.catalog_id" :value="catalog.catalog_id">
            {{ catalog.catalog_id }}（{{ catalog.business_date }}，{{ catalog.selection_count }} 键）
          </option>
        </select>
      </label>
      <label>
        基线目录（留空取上一版本）
        <select v-model="compareBaselineCatalog" data-testid="compare-baseline">
          <option value="">自动：各键上一版本</option>
          <option v-for="catalog in catalogs" :key="catalog.catalog_id" :value="catalog.catalog_id">
            {{ catalog.catalog_id }}（{{ catalog.business_date }}）
          </option>
        </select>
      </label>
      <button class="btn primary" data-testid="run-compare" type="submit">执行比对</button>
    </form>
    <table v-if="compareReport" class="data-table" data-testid="diff-table">
      <thead>
        <tr>
          <th>自然键</th>
          <th>字段</th>
          <th>更正前</th>
          <th>更正后</th>
          <th>版本（基线 → 新）</th>
          <th>差异归因</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="diff in compareReport.differences" :key="diff.diff_id">
          <td>{{ diff.natural_key }}</td>
          <td>{{ diff.field }}</td>
          <td>{{ diff.before_value ?? '—' }}</td>
          <td>{{ diff.after_value ?? '—' }}</td>
          <td>#{{ diff.before_version_id ?? '—' }} → #{{ diff.after_version_id ?? '—' }}</td>
          <td>{{ diff.change_type }}</td>
        </tr>
        <tr v-if="!compareReport.differences.length">
          <td colspan="6" class="empty-state">两份快照无字段差异</td>
        </tr>
      </tbody>
    </table>
    <p v-if="compareReport" class="page-foot">
      <span>快照清单与差异清单版本一致：{{ compareReport.version_consistent ? '是' : '否' }}</span>
      <span>更正影响既有日期 {{ compareReport.summary.correction_affecting_existing_date }} 项，
        本次新增 {{ compareReport.summary.new_differences }} 项</span>
    </p>

    <!-- 版本核对 -->
    <h3 v-if="reconcileReport">已发布版本与比对版本核对</h3>
    <table v-if="reconcileReport" class="data-table" data-testid="reconcile-table">
      <thead>
        <tr><th>目录</th><th>业务日期</th><th>固化版本数</th><th>未解析版本</th><th>结论</th></tr>
      </thead>
      <tbody>
        <tr v-for="check in reconcileReport.catalog_checks" :key="check.catalog_id">
          <td>{{ check.catalog_id }}</td>
          <td>{{ check.business_date }}</td>
          <td>{{ check.selection_count }}</td>
          <td>{{ check.unresolved_version_ids.length ? check.unresolved_version_ids.join(',') : '无' }}</td>
          <td :class="check.consistent ? 'trend-up' : 'error-text'">{{ check.consistent ? '一致' : '异常' }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  asOfSnapshot,
  listCatalogs,
  listVersions,
  openQuery,
  pagedSnapshot,
  reconcile,
  runCompare,
  submitCorrection,
} from '@/api/dim-snapshot-service'
import type {
  CatalogMeta,
  CompareReport,
  DimensionVersion,
  ReconcileReport,
  SnapshotPage,
} from '@/data/dim-snapshot-types'

const dimTable = ref('dim_customer')
const businessDate = ref('2025-08-01')
const customerId = ref(17)
const queryNote = ref('')
const correctionDate = ref('2025-08-01')
const correctionRegion = ref('华南')
const correctionSource = ref('region-rework-202508')
const compareNewCatalog = ref('')
const compareBaselineCatalog = ref('')

const snapshotPage = ref<SnapshotPage | null>(null)
const versions = ref<DimensionVersion[]>([])
const catalogs = ref<CatalogMeta[]>([])
const compareReport = ref<CompareReport | null>(null)
const reconcileReport = ref<ReconcileReport | null>(null)
const message = ref('')
const messageError = ref(false)
const activeCatalogId = ref('')

function notify(text: string, isError = false) {
  message.value = text
  messageError.value = isError
}

async function refreshCatalogs() {
  const result = await listCatalogs()
  catalogs.value = result.items
}

async function refreshVersions() {
  const result = await listVersions(dimTable.value, customerId.value || undefined)
  versions.value = result.items
}

async function openNewQuery() {
  try {
    const page = await openQuery({
      dimTable: dimTable.value,
      businessDate: businessDate.value,
      customerId: customerId.value || undefined,
      note: queryNote.value,
    })
    snapshotPage.value = page
    activeCatalogId.value = page.catalog?.catalog_id ?? ''
    compareNewCatalog.value = activeCatalogId.value
    notify(`已固化快照目录 ${activeCatalogId.value}，跨页将固定同一版本`)
    await Promise.all([refreshCatalogs(), refreshVersions()])
  } catch (error) {
    notify(error instanceof Error ? error.message : '开启查询失败', true)
  }
}

async function loadAsOf() {
  try {
    const result = await asOfSnapshot(dimTable.value, businessDate.value, Number(customerId.value))
    snapshotPage.value = {
      items: [result.snapshot],
      total: 1,
      page: 1,
      size: 20,
      business_date: businessDate.value,
      dim_table: dimTable.value,
      pinned: false,
      catalog_id: null,
    }
    activeCatalogId.value = ''
    notify(`as-of 命中版本 #${result.snapshot.version_id}，地区=${result.snapshot.attributes.region}`)
    await refreshVersions()
  } catch (error) {
    notify(error instanceof Error ? error.message : 'as-of 查询失败', true)
  }
}

async function turnPage(delta: number) {
  if (!activeCatalogId.value) return
  const nextPage = (snapshotPage.value?.page ?? 1) + delta
  if (nextPage < 1) return
  try {
    snapshotPage.value = await pagedSnapshot({
      dimTable: dimTable.value,
      businessDate: businessDate.value,
      catalogId: activeCatalogId.value,
      page: nextPage,
      size: snapshotPage.value?.size ?? 20,
    })
  } catch (error) {
    notify(error instanceof Error ? error.message : '翻页失败', true)
  }
}

async function submitRegionCorrection() {
  try {
    const result = await submitCorrection({
      dimTable: dimTable.value,
      naturalKey: { customer_id: Number(customerId.value) },
      effectiveFrom: correctionDate.value,
      changes: { region: correctionRegion.value },
      source: correctionSource.value,
    })
    notify(
      result.duplicate
        ? `重复提交已拦截，沿用首次版本 #${result.version.version_id}`
        : `更正已发布版本 #${result.version.version_id}，旧区间在 ${correctionDate.value} 关闭`,
      result.duplicate,
    )
    await Promise.all([refreshVersions(), refreshCatalogs()])
  } catch (error) {
    notify(error instanceof Error ? error.message : '更正提交失败', true)
  }
}

async function compareCatalogs() {
  if (!compareNewCatalog.value) {
    notify('请先选择新查询目录', true)
    return
  }
  try {
    compareReport.value = await runCompare({
      businessDate: businessDate.value,
      newCatalogId: compareNewCatalog.value,
      baselineCatalogId: compareBaselineCatalog.value || undefined,
    })
    await refreshVersions()
    notify(
      `比对完成：${compareReport.value.summary.difference_rows} 项差异，` +
        `版本一致=${compareReport.value.version_consistent}`,
    )
  } catch (error) {
    notify(error instanceof Error ? error.message : '快照比对失败', true)
  }
}

async function loadReconcile() {
  try {
    reconcileReport.value = await reconcile()
    notify(
      reconcileReport.value.consistent
        ? '核对通过：已发布版本与比对使用版本完全一致'
        : '核对发现版本不一致，请检查异常目录',
      !reconcileReport.value.consistent,
    )
  } catch (error) {
    notify(error instanceof Error ? error.message : '版本核对失败', true)
  }
}

onMounted(async () => {
  await Promise.all([refreshCatalogs(), refreshVersions()])
})
</script>

<style scoped>
.attr-chip {
  display: inline-block;
  margin-right: 6px;
  padding: 2px 8px;
  background: #f1f5f9;
  border-radius: 10px;
  font-size: 12px;
}
.trend-up {
  color: #16a34a;
}
h3 {
  margin: 18px 0 8px;
  font-size: 15px;
}
</style>
