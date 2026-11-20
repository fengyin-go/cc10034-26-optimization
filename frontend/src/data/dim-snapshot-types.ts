/**
 * 维度快照领域类型：与后端 app/dim_snapshot.py 的返回结构保持一致。
 *
 * 核心模型：
 * - DimensionVersion：SCD2 一条历史版本，区间 [effectiveFrom, effectiveTo) 半开不重叠；
 * - SnapshotCatalog：查询开启时固化的快照目录，跨页按 versionId 固定版本；
 * - SnapshotDifference：字段级差异，携带快照清单版本用于一致性核对。
 */

export type ChangeType = '更正影响既有日期' | '本次新增差异' | '记录缺失'

export interface DimensionVersion {
  version_id: number
  dim_table: string
  natural_key: string
  natural_key_values: Record<string, string | number>
  version_no: number
  attributes: Record<string, string | number>
  effective_from: string
  effective_to: string
  open_ended: boolean
  status: string
  source: string
  is_correction: boolean
  idempotency_key: string | null
  created_at: string
  published_at: string
  diff_marks: Array<Record<string, unknown>>
}

export interface SnapshotRow {
  dim_table: string
  natural_key: string
  natural_key_values: Record<string, string | number>
  version_id: number
  version_no: number
  effective_from: string
  effective_to: string
  status: string
  attributes: Record<string, string | number>
  business_date: string
  pinned: boolean
  selection_basis: string
  is_correction: boolean
  /** 录入日期晚于业务日期的回溯更正：业务日期与录入日期冲突时靠它识别。 */
  backdated_correction: boolean
  recorded_at: string
  diff_marks: Array<Record<string, unknown>>
}

export interface CatalogMeta {
  catalog_id: string
  dim_table: string
  business_date: string
  created_at: string
  selection_count: number
  version_ids: number[]
  note: string
}

export interface SnapshotPage {
  items: SnapshotRow[]
  total: number
  page: number
  size: number
  business_date: string
  dim_table: string
  pinned: boolean
  catalog_id: string | null
  catalog?: CatalogMeta
}

export interface CorrectionResult {
  accepted: boolean
  duplicate: boolean
  message: string
  version: DimensionVersion
  closed_version_id?: number
}

export interface SnapshotDifference {
  diff_id: string
  dim_table: string
  natural_key: string
  field: string
  before_value: string | number | null
  after_value: string | number | null
  before_version_id: number | null
  after_version_id: number | null
  before_version_no: number | null
  after_version_no: number | null
  snapshot_version_id: number | null
  change_type: ChangeType
  reason: string
  business_date: string
  source: string
}

export interface CompareReport {
  business_date: string
  dim_table: string
  baseline_catalog_id: string | null
  new_catalog_id: string
  version_consistent: boolean
  consistency_checks: Array<{
    diff_id: string
    natural_key: string
    snapshot_version_id: number | null
    difference_version_id: number | null
    consistent: boolean
  }>
  summary: {
    total_keys: number
    changed_keys: number
    difference_rows: number
    correction_affecting_existing_date: number
    new_differences: number
    missing_differences: number
  }
  differences: SnapshotDifference[]
  compared_at: string
}

export interface ReconcileReport {
  checked_at: string
  published_version_count: number
  used_version_count: number
  published_version_ids: number[]
  used_version_ids: number[]
  missing_in_published: number[]
  interval_overlap_violations: Array<Record<string, unknown>>
  catalog_checks: Array<{
    catalog_id: string
    business_date: string
    selection_count: number
    unresolved_version_ids: number[]
    consistent: boolean
  }>
  difference_version_mismatches: Array<Record<string, unknown>>
  consistent: boolean
}
