"""维度快照领域路由：指定时刻快照、版本目录、更正落库、快照比对与版本核对。

所有接口位于 /api/dim-snapshot/ 固定前缀下，注册顺序先于通用资源路由，
不会被 /api/{module} 动态路径抢占。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.dim_snapshot import DimensionRuleError, dimension_store

router = APIRouter(prefix="/api/dim-snapshot", tags=["维度快照"])


class CorrectionPayload(BaseModel):
    dim_table: str = "dim_customer"
    natural_key: dict[str, Any] = Field(default_factory=dict)
    effective_from: str
    changes: dict[str, Any] = Field(default_factory=dict)
    source: str = "manual-correction"


class RegisterPayload(BaseModel):
    dim_table: str = "dim_customer"
    natural_key: dict[str, Any] = Field(default_factory=dict)
    effective_from: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    source: str = "dimension-onboarding"


class ComparePayload(BaseModel):
    business_date: str
    new_catalog_id: str
    baseline_catalog_id: str | None = None


def _handle_rule_error(exc: DimensionRuleError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/versions/{dim_table}")
def list_versions(
    dim_table: str,
    customer_id: int | None = Query(default=None),
) -> dict[str, Any]:
    """列出维表已发布的全部历史版本；可按自然键 customer_id 过滤。"""
    nkv = {"customer_id": customer_id} if customer_id is not None else None
    items = dimension_store.list_versions(dim_table, nkv)
    return {"dim_table": dim_table, "total": len(items), "items": items}


@router.get("/as-of")
def as_of_snapshot(
    dim_table: str = Query(..., description="维表名，如 dim_customer"),
    business_date: str = Query(..., description="业务日期 YYYY-MM-DD"),
    customer_id: int = Query(..., description="自然键 customer_id"),
) -> dict[str, Any]:
    """单自然键场景：直接查询指定时刻命中的维度版本（不固化目录）。"""
    nkv = {"customer_id": customer_id}
    try:
        version = dimension_store.as_of_version(dim_table, nkv, business_date)
    except DimensionRuleError as exc:
        raise _handle_rule_error(exc) from exc
    if version is None:
        raise HTTPException(
            status_code=404,
            detail=f"{dim_table}{nkv} 在 {business_date} 没有生效版本",
        )
    return {
        "business_date": business_date,
        "dim_table": dim_table,
        "snapshot": dimension_store._snapshot_row(
            version, business_date, pinned=False
        ),
    }


@router.get("/snapshot")
def paged_snapshot(
    dim_table: str,
    business_date: str,
    customer_id: int | None = Query(default=None),
    catalog_id: str | None = Query(default=None, description="快照目录ID，跨页固定版本"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """指定时刻维度快照（分页）。

    - 不传 catalog_id：按当前已发布版本即时生成；
    - 传 catalog_id：严格按目录固化的 version_id 复现同一查询的每一页。
    """
    filters = {"customer_id": customer_id} if customer_id is not None else None
    try:
        return dimension_store.snapshot(
            dim_table, business_date, filters,
            page=page, size=size, catalog_id=catalog_id,
        )
    except DimensionRuleError as exc:
        raise _handle_rule_error(exc) from exc


@router.post("/queries")
def open_query(
    dim_table: str,
    business_date: str,
    customer_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    note: str = Query(default=""),
) -> dict[str, Any]:
    """开启一次新查询：固化快照目录（版本清单），返回第一页与 catalog_id。"""
    filters = {"customer_id": customer_id} if customer_id is not None else None
    try:
        return dimension_store.open_query(
            dim_table, business_date, filters,
            page=page, size=size, note=note,
        )
    except DimensionRuleError as exc:
        raise _handle_rule_error(exc) from exc


@router.get("/catalogs")
def list_catalogs() -> dict[str, Any]:
    """快照清单：所有已固化目录及其版本。"""
    items = dimension_store.list_catalogs()
    return {"total": len(items), "items": items}


@router.post("/dimensions")
def register_dimension(payload: RegisterPayload) -> dict[str, Any]:
    """新自然键建档：发布首个维度版本（更正与建档的规则不同，入口分开）。"""
    if not payload.natural_key:
        raise HTTPException(status_code=422, detail="natural_key 不能为空，例如 {\"customer_id\": 20}")
    try:
        return dimension_store.register_dimension(
            dim_table=payload.dim_table,
            natural_key_values=payload.natural_key,
            effective_from=payload.effective_from,
            attributes=payload.attributes,
            source=payload.source,
        )
    except DimensionRuleError as exc:
        raise _handle_rule_error(exc) from exc


@router.post("/corrections")
def submit_correction(payload: CorrectionPayload) -> dict[str, Any]:
    """补交维度更正：幂等去重、关闭承接区间、发布新版本。"""
    if not payload.natural_key:
        raise HTTPException(status_code=422, detail="natural_key 不能为空，例如 {\"customer_id\": 17}")
    try:
        return dimension_store.submit_correction(
            dim_table=payload.dim_table,
            natural_key_values=payload.natural_key,
            effective_from=payload.effective_from,
            changes=payload.changes,
            source=payload.source,
        )
    except DimensionRuleError as exc:
        raise _handle_rule_error(exc) from exc


@router.post("/compare")
def run_compare(payload: ComparePayload) -> dict[str, Any]:
    """对同一业务日期的新/旧两份快照执行字段级比对，差异回写维度记录。"""
    try:
        return dimension_store.run_compare(
            business_date=payload.business_date,
            new_catalog_id=payload.new_catalog_id,
            baseline_catalog_id=payload.baseline_catalog_id,
        )
    except DimensionRuleError as exc:
        raise _handle_rule_error(exc) from exc


@router.get("/differences")
def list_differences(
    business_date: str | None = Query(default=None),
    catalog_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """差异清单：可按业务日期或快照目录过滤。"""
    items = dimension_store.list_differences(
        business_date=business_date, catalog_id=catalog_id
    )
    return {"total": len(items), "items": items}


@router.get("/reconcile")
def reconcile() -> dict[str, Any]:
    """同步核对已发布的维度版本与快照目录、比对使用的版本。"""
    return dimension_store.reconcile()
