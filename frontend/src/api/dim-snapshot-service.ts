/**
 * 维度快照领域接口封装：指定时刻快照、版本目录、更正落库、快照比对、版本核对。
 *
 * 与通用 backend-service 的模块资源不同，这里走固定前缀 /api/dim-snapshot，
 * 由后端专属路由承载 SCD2 版本规则，不与 /api/{module} 动态路由混用。
 */
import { request } from './client'
import type {
  CatalogMeta,
  CompareReport,
  CorrectionResult,
  DimensionVersion,
  ReconcileReport,
  SnapshotPage,
  SnapshotRow,
} from '@/data/dim-snapshot-types'

const PREFIX = '/api/dim-snapshot'

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = (await response.json().catch(() => null))?.detail ?? response.statusText
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return (await response.json()) as T
}

/** 查询维表已发布的历史版本，可按 customer_id 收敛到单个自然键。 */
export async function listVersions(
  dimTable: string,
  customerId?: number,
): Promise<{ items: DimensionVersion[]; total: number }> {
  const query = new URLSearchParams()
  if (customerId !== undefined) query.set('customer_id', String(customerId))
  const response = await request(`${PREFIX}/versions/${dimTable}?${query}`)
  return readJson(response)
}

/** 单键 as-of：直接定位某业务日期当时生效的版本（不固化目录）。 */
export async function asOfSnapshot(
  dimTable: string,
  businessDate: string,
  customerId: number,
): Promise<{ snapshot: SnapshotRow }> {
  const query = new URLSearchParams({
    dim_table: dimTable,
    business_date: businessDate,
    customer_id: String(customerId),
  })
  const response = await request(`${PREFIX}/as-of?${query}`)
  return readJson(response)
}

/** 指定时刻快照分页；带 catalogId 时严格按目录固定版本复现每一页。 */
export async function pagedSnapshot(params: {
  dimTable: string
  businessDate: string
  customerId?: number
  catalogId?: string
  page?: number
  size?: number
}): Promise<SnapshotPage> {
  const query = new URLSearchParams({
    dim_table: params.dimTable,
    business_date: params.businessDate,
    page: String(params.page ?? 1),
    size: String(params.size ?? 20),
  })
  if (params.customerId !== undefined) query.set('customer_id', String(params.customerId))
  if (params.catalogId) query.set('catalog_id', params.catalogId)
  const response = await request(`${PREFIX}/snapshot?${query}`)
  return readJson<SnapshotPage>(response)
}

/** 开启一次新查询：固化快照目录并返回第一页，catalog_id 用于后续跨页固定版本。 */
export async function openQuery(params: {
  dimTable: string
  businessDate: string
  customerId?: number
  note?: string
  page?: number
  size?: number
}): Promise<SnapshotPage> {
  const query = new URLSearchParams({
    dim_table: params.dimTable,
    business_date: params.businessDate,
    page: String(params.page ?? 1),
    size: String(params.size ?? 20),
  })
  if (params.customerId !== undefined) query.set('customer_id', String(params.customerId))
  if (params.note) query.set('note', params.note)
  const response = await request(`${PREFIX}/queries?${query}`, { method: 'POST' })
  return readJson<SnapshotPage>(response)
}

/** 快照清单：全部已固化目录及其版本。 */
export async function listCatalogs(): Promise<{ items: CatalogMeta[]; total: number }> {
  const response = await request(`${PREFIX}/catalogs`)
  return readJson(response)
}

/** 补交维度更正：同来源幂等去重，回溯生效自动关闭承接区间并发布新版本。 */
export async function submitCorrection(payload: {
  dimTable: string
  naturalKey: Record<string, string | number>
  effectiveFrom: string
  changes: Record<string, string | number>
  source: string
}): Promise<CorrectionResult> {
  const response = await request(`${PREFIX}/corrections`, {
    method: 'POST',
    body: JSON.stringify({
      dim_table: payload.dimTable,
      natural_key: payload.naturalKey,
      effective_from: payload.effectiveFrom,
      changes: payload.changes,
      source: payload.source,
    }),
  })
  return readJson<CorrectionResult>(response)
}

/** 新自然键建档，发布首个维度版本。 */
export async function registerDimension(payload: {
  dimTable: string
  naturalKey: Record<string, string | number>
  effectiveFrom: string
  attributes: Record<string, string | number>
  source: string
}): Promise<{ accepted: boolean; message: string; version: DimensionVersion }> {
  const response = await request(`${PREFIX}/dimensions`, {
    method: 'POST',
    body: JSON.stringify({
      dim_table: payload.dimTable,
      natural_key: payload.naturalKey,
      effective_from: payload.effectiveFrom,
      attributes: payload.attributes,
      source: payload.source,
    }),
  })
  return readJson(response)
}

/** 对同一业务日期的新/旧快照做字段级比对，差异回写维度记录。 */
export async function runCompare(payload: {
  businessDate: string
  newCatalogId: string
  baselineCatalogId?: string
}): Promise<CompareReport> {
  const response = await request(`${PREFIX}/compare`, {
    method: 'POST',
    body: JSON.stringify({
      business_date: payload.businessDate,
      new_catalog_id: payload.newCatalogId,
      baseline_catalog_id: payload.baselineCatalogId ?? null,
    }),
  })
  return readJson<CompareReport>(response)
}

/** 差异清单查询，可按业务日期或快照目录过滤。 */
export async function listDifferences(params: {
  businessDate?: string
  catalogId?: string
}): Promise<{ items: CompareReport[]; total: number }> {
  const query = new URLSearchParams()
  if (params.businessDate) query.set('business_date', params.businessDate)
  if (params.catalogId) query.set('catalog_id', params.catalogId)
  const response = await request(`${PREFIX}/differences?${query}`)
  return readJson(response)
}

/** 同步核对已发布版本与快照目录、比对使用的版本。 */
export async function reconcile(): Promise<ReconcileReport> {
  const response = await request(`${PREFIX}/reconcile`)
  return readJson<ReconcileReport>(response)
}
