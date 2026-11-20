"""维度版本与指定时刻快照引擎（SCD Type2 增强）。

业务背景
--------
维度历史版本按生效时间维护，对账需要稳定定位某个业务日期当时生效的版本。
本模块在通用资源模块之外提供一套严格的版本化维度存储，保证以下不变量：

1. 同一自然键的版本区间 ``[effective_from, effective_to)`` 为半开区间，两个有效时段不重叠；
2. 版本只追加、不原地改写：回溯更正先关闭承接区间（兼容未结束的开放区间），
   再生成新的版本号，历史版本永久保留；
3. 指定时刻快照按 ``effective_from <= business_date < effective_to`` 命中唯一版本，
   快照行包含维表、自然键和版本；
4. 每次查询开启时固化一份快照目录（SnapshotCatalog），目录记录每个自然键选中的
   version_id；查询跨页只认目录，后续到达的更正不影响旧查询，旧结果可随时复现；
5. 同一来源的重复更正按幂等指纹去重，只允许落库一次；
6. 快照比对产出字段级差异并回写到维度版本记录，差异清单引用的版本必须与快照清单
   完全一致；reconcile 同步核对已发布版本与比对实际使用的版本。

业务日期与录入日期冲突时，新查询始终以「业务日期命中的当前已发布版本」为准，
并通过 ``backdated_correction`` 标记提示该版本是晚于业务日期录入的回溯更正，
从而区分更正影响的是既有日期，还是本次新增的差异。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.pipeline import pipeline_bus

# 开放区间的失效时间上界，表示区间尚未结束。
OPEN_ENDED = "9999-12-31"
DATE_FMT = "%Y-%m-%d"

# 维度版本生命周期状态。
STATUS_ACTIVE = "生效中"
STATUS_CLOSED = "已失效"

# 差异归因类型。
DIFF_CORRECTION_EXISTING = "更正影响既有日期"
DIFF_NEW = "本次新增差异"
DIFF_MISSING = "记录缺失"


class DimensionRuleError(ValueError):
    """维度版本规则被违反（区间重叠、无可承接区间等）。"""


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value.strip(), DATE_FMT).date()
    except (AttributeError, ValueError) as exc:  # pragma: no cover - 防御分支
        raise DimensionRuleError(f"日期格式必须为 YYYY-MM-DD：{value!r}") from exc


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def canonical_natural_key(natural_key_values: dict[str, Any]) -> str:
    """把自然键字典规范成可展示、可比较的字符串，如 ``customer_id=17``。"""
    return "|".join(f"{k}={natural_key_values[k]}" for k in sorted(natural_key_values))


@dataclass
class DimensionVersion:
    """一条维度历史版本记录（SCD2 一行一个版本）。"""

    version_id: int
    dim_table: str
    natural_key: str
    natural_key_values: dict[str, Any]
    version_no: int
    attributes: dict[str, Any]
    effective_from: str
    effective_to: str
    status: str
    source: str
    is_correction: bool
    idempotency_key: str | None
    created_at: str
    published_at: str
    diff_marks: list[dict[str, Any]] = field(default_factory=list)

    def is_open(self) -> bool:
        return self.effective_to == OPEN_ENDED

    def covers(self, business_date: str) -> bool:
        """半开区间判定：生效日当天命中新版本，失效日当天不再命中。"""
        d = _parse_date(business_date)
        return _parse_date(self.effective_from) <= d < _parse_date(self.effective_to)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "dim_table": self.dim_table,
            "natural_key": self.natural_key,
            "natural_key_values": dict(self.natural_key_values),
            "version_no": self.version_no,
            "attributes": dict(self.attributes),
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "open_ended": self.is_open(),
            "status": self.status,
            "source": self.source,
            "is_correction": self.is_correction,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
            "published_at": self.published_at,
            "diff_marks": [dict(mark) for mark in self.diff_marks],
        }


@dataclass
class SnapshotCatalog:
    """一次查询开启时固化的快照目录。

    selections 以自然键为 key，保存当时命中的 version_id 以及区间边界快照，
    之后即便版本被更正关闭，目录仍能原样复现这次查询的每一页。
    """

    catalog_id: str
    dim_table: str
    business_date: str
    created_at: str
    selections: dict[str, dict[str, Any]] = field(default_factory=dict)
    note: str = ""

    def to_meta(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "dim_table": self.dim_table,
            "business_date": self.business_date,
            "created_at": self.created_at,
            "selection_count": len(self.selections),
            "version_ids": sorted(item["version_id"] for item in self.selections.values()),
            "note": self.note,
        }


class DimensionVersionStore:
    """维度版本、快照目录、差异清单的内存存储与领域规则实现。"""

    def __init__(self) -> None:
        self._versions: list[DimensionVersion] = []
        self._catalogs: dict[str, SnapshotCatalog] = {}
        self._differences: list[dict[str, Any]] = []
        self._idempotency_keys: set[str] = set()
        self._version_seq = 1000
        self._catalog_seq = 0
        self._diff_seq = 0
        self._seed_dim_customer()

    # ------------------------------------------------------------------ 种子数据
    def _seed_dim_customer(self) -> None:
        """构造 customer 维度的既有历史，含一个未结束的开放区间。"""
        seeded = [
            {
                "dim_table": "dim_customer",
                "natural_key_values": {"customer_id": 17},
                "version_no": 1,
                "attributes": {"customer_name": "启明科技", "region": "华东", "customer_level": "普通"},
                "effective_from": "2025-01-01",
                "effective_to": "2025-06-01",
                "created_at": "2025-01-01T09:00:00",
            },
            {
                "dim_table": "dim_customer",
                "natural_key_values": {"customer_id": 17},
                "version_no": 2,
                "attributes": {"customer_name": "启明科技", "region": "华东", "customer_level": "金卡"},
                "effective_from": "2025-06-01",
                "effective_to": OPEN_ENDED,
                "created_at": "2025-06-01T09:00:00",
            },
            {
                "dim_table": "dim_customer",
                "natural_key_values": {"customer_id": 18},
                "version_no": 1,
                "attributes": {"customer_name": "远山物流", "region": "华北", "customer_level": "普通"},
                "effective_from": "2025-03-01",
                "effective_to": OPEN_ENDED,
                "created_at": "2025-03-01T09:00:00",
            },
            {
                "dim_table": "dim_customer",
                "natural_key_values": {"customer_id": 19},
                "version_no": 1,
                "attributes": {"customer_name": "星桥零售", "region": "西南", "customer_level": "银卡"},
                "effective_from": "2025-05-01",
                "effective_to": OPEN_ENDED,
                "created_at": "2025-05-01T09:00:00",
            },
        ]
        for item in seeded:
            version = DimensionVersion(
                version_id=self._next_version_id(),
                dim_table=item["dim_table"],
                natural_key=canonical_natural_key(item["natural_key_values"]),
                natural_key_values=dict(item["natural_key_values"]),
                version_no=item["version_no"],
                attributes=dict(item["attributes"]),
                effective_from=item["effective_from"],
                effective_to=item["effective_to"],
                status=STATUS_ACTIVE if item["effective_to"] == OPEN_ENDED else STATUS_CLOSED,
                source="seed",
                is_correction=False,
                idempotency_key=None,
                created_at=item["created_at"],
                published_at=item["created_at"],
            )
            self._versions.append(version)

    def _next_version_id(self) -> int:
        self._version_seq += 1
        return self._version_seq

    def reset(self) -> None:
        """测试辅助：清空后重新播种。"""
        self._versions.clear()
        self._catalogs.clear()
        self._differences.clear()
        self._idempotency_keys.clear()
        self._version_seq = 1000
        self._catalog_seq = 0
        self._diff_seq = 0
        self._seed_dim_customer()

    # ------------------------------------------------------------------ 基础查询
    def list_versions(
        self, dim_table: str, natural_key_values: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """列出已发布的维度版本（版本一经提交即发布，不做物理删除）。"""
        nk = canonical_natural_key(natural_key_values) if natural_key_values else None
        rows = [
            v.to_dict()
            for v in self._versions
            if v.dim_table == dim_table and (nk is None or v.natural_key == nk)
        ]
        rows.sort(key=lambda row: (row["natural_key"], row["version_no"]))
        return rows

    def _history(self, dim_table: str, natural_key: str) -> list[DimensionVersion]:
        return sorted(
            (v for v in self._versions if v.dim_table == dim_table and v.natural_key == natural_key),
            key=lambda v: (v.effective_from, v.version_no),
        )

    def _find_by_id(self, version_id: int) -> DimensionVersion | None:
        return next((v for v in self._versions if v.version_id == version_id), None)

    def as_of_version(
        self, dim_table: str, natural_key_values: dict[str, Any], business_date: str
    ) -> DimensionVersion | None:
        """按业务日期定位唯一生效版本：effective_from <= d < effective_to。"""
        natural_key = canonical_natural_key(natural_key_values)
        candidates = [v for v in self._history(dim_table, natural_key) if v.covers(business_date)]
        if len(candidates) > 1:
            # 不变量：两个有效时段不能重叠。出现两个命中说明数据被破坏。
            raise DimensionRuleError(
                f"{dim_table}[{natural_key}] 在 {business_date} 命中多个重叠版本："
                f"{[v.version_id for v in candidates]}"
            )
        return candidates[0] if candidates else None

    def _match_filters(
        self, version: DimensionVersion, filters: dict[str, str] | None
    ) -> bool:
        if not filters:
            return True
        for key, expected in filters.items():
            expected = str(expected).strip()
            if not expected:
                continue
            actual = version.natural_key_values.get(key, version.attributes.get(key, ""))
            if expected.casefold() not in str(actual).casefold():
                return False
        return True

    def _snapshot_row(
        self,
        version: DimensionVersion,
        business_date: str,
        *,
        pinned: bool,
        captured_from: str | None = None,
        captured_to: str | None = None,
    ) -> dict[str, Any]:
        recorded_date = version.created_at[:10]
        return {
            "dim_table": version.dim_table,
            "natural_key": version.natural_key,
            "natural_key_values": dict(version.natural_key_values),
            "version_id": version.version_id,
            "version_no": version.version_no,
            "effective_from": captured_from or version.effective_from,
            "effective_to": captured_to or version.effective_to,
            "status": version.status,
            "attributes": dict(version.attributes),
            "business_date": business_date,
            "pinned": pinned,
            "selection_basis": "快照目录固定版本" if pinned else "按业务日期命中当前已发布版本",
            "is_correction": version.is_correction,
            # 录入日期晚于业务日期 → 回溯更正，用于识别业务日期与录入日期冲突。
            "backdated_correction": version.is_correction and recorded_date > business_date,
            "recorded_at": version.created_at,
            "diff_marks": [dict(mark) for mark in version.diff_marks],
        }

    # ------------------------------------------------------------- 指定时刻快照
    def snapshot(
        self,
        dim_table: str,
        business_date: str,
        filters: dict[str, str] | None = None,
        *,
        page: int = 1,
        size: int = 20,
        catalog_id: str | None = None,
    ) -> dict[str, Any]:
        """生成指定业务日期的维度快照，支持目录固定版本与跨页分页。"""
        _parse_date(business_date)
        if catalog_id is not None:
            catalog = self._catalogs.get(catalog_id)
            if catalog is None:
                raise DimensionRuleError(f"快照目录 {catalog_id} 不存在，无法固定版本")
            if catalog.dim_table != dim_table or catalog.business_date != business_date:
                raise DimensionRuleError(
                    f"快照目录 {catalog_id} 绑定的是 {catalog.dim_table}@{catalog.business_date}，"
                    f"与本次请求 {dim_table}@{business_date} 不一致"
                )
            return self._catalog_page(catalog, filters or {}, page, size)

        keys = self._distinct_keys(dim_table)
        rows: list[dict[str, Any]] = []
        for natural_key in keys:
            nkv = self._natural_key_values(dim_table, natural_key)
            version = self.as_of_version(dim_table, nkv, business_date)
            if version is None or not self._match_filters(version, filters):
                continue
            rows.append(self._snapshot_row(version, business_date, pinned=False))
        return self._paginate(rows, page, size, business_date, dim_table, pinned=False, catalog_id=None)

    def open_query(
        self,
        dim_table: str,
        business_date: str,
        filters: dict[str, str] | None = None,
        *,
        page: int = 1,
        size: int = 20,
        note: str = "",
    ) -> dict[str, Any]:
        """开启一次新查询：按当前已发布版本固化快照目录，并返回第一页。"""
        _parse_date(business_date)
        keys = self._distinct_keys(dim_table)
        selections: dict[str, dict[str, Any]] = {}
        for natural_key in keys:
            nkv = self._natural_key_values(dim_table, natural_key)
            version = self.as_of_version(dim_table, nkv, business_date)
            if version is None or not self._match_filters(version, filters):
                continue
            selections[natural_key] = {
                "version_id": version.version_id,
                "version_no": version.version_no,
                "effective_from": version.effective_from,
                "effective_to": version.effective_to,
            }
        self._catalog_seq += 1
        catalog = SnapshotCatalog(
            catalog_id=f"cat-{self._catalog_seq:04d}",
            dim_table=dim_table,
            business_date=business_date,
            created_at=_now_iso(),
            selections=selections,
            note=note,
        )
        self._catalogs[catalog.catalog_id] = catalog
        pipeline_bus.write_audit(
            "维度快照查询",
            f"{dim_table}@{business_date}",
            f"开启快照查询 {catalog.catalog_id}，固化 {len(selections)} 个自然键的版本",
            "dim_snapshot",
            "已固化",
        )
        page_result = self._catalog_page(catalog, filters or {}, page, size)
        page_result["catalog"] = catalog.to_meta()
        return page_result

    def _catalog_page(
        self,
        catalog: SnapshotCatalog,
        filters: dict[str, str],
        page: int,
        size: int,
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for natural_key, selection in catalog.selections.items():
            version = self._find_by_id(selection["version_id"])
            if version is None:
                # 目录引用了未发布/不存在的版本，交给 reconcile 报警，此处跳过。
                continue
            if not self._match_filters(version, filters):
                continue
            rows.append(
                self._snapshot_row(
                    version,
                    catalog.business_date,
                    pinned=True,
                    captured_from=selection["effective_from"],
                    captured_to=selection["effective_to"],
                )
            )
        result = self._paginate(
            rows, page, size, catalog.business_date, catalog.dim_table,
            pinned=True, catalog_id=catalog.catalog_id,
        )
        result["catalog"] = catalog.to_meta()
        return result

    def list_catalogs(self) -> list[dict[str, Any]]:
        return [catalog.to_meta() for catalog in self._catalogs.values()]

    def _distinct_keys(self, dim_table: str) -> list[str]:
        return sorted({v.natural_key for v in self._versions if v.dim_table == dim_table})

    def _natural_key_values(self, dim_table: str, natural_key: str) -> dict[str, Any]:
        version = next(
            v for v in self._versions if v.dim_table == dim_table and v.natural_key == natural_key
        )
        return dict(version.natural_key_values)

    def _paginate(
        self,
        rows: list[dict[str, Any]],
        page: int,
        size: int,
        business_date: str,
        dim_table: str,
        *,
        pinned: bool,
        catalog_id: str | None,
    ) -> dict[str, Any]:
        rows.sort(key=lambda row: row["natural_key"])
        total = len(rows)
        start = (page - 1) * size
        return {
            "items": rows[start:start + size],
            "total": total,
            "page": page,
            "size": size,
            "business_date": business_date,
            "dim_table": dim_table,
            "pinned": pinned,
            "catalog_id": catalog_id,
        }

    # ---------------------------------------------------------- 新维度登记
    def register_dimension(
        self,
        dim_table: str,
        natural_key_values: dict[str, Any],
        effective_from: str,
        attributes: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        """登记一个新自然键的首个维度版本（新客户建档等场景）。

        与更正不同：更正必须承接既有区间，而登记要求自然键尚不存在，
        首版本区间为 ``[effective_from, 9999-12-31)``。
        """
        _parse_date(effective_from)
        if not attributes:
            raise DimensionRuleError("登记维度必须包含属性字段")
        natural_key = canonical_natural_key(natural_key_values)
        if self._history(dim_table, natural_key):
            raise DimensionRuleError(
                f"{dim_table}[{natural_key}] 已存在历史版本，新属性应走更正流程"
            )
        version = DimensionVersion(
            version_id=self._next_version_id(),
            dim_table=dim_table,
            natural_key=natural_key,
            natural_key_values=dict(natural_key_values),
            version_no=1,
            attributes=dict(attributes),
            effective_from=effective_from,
            effective_to=OPEN_ENDED,
            status=STATUS_ACTIVE,
            source=source,
            is_correction=False,
            idempotency_key=None,
            created_at=_now_iso(),
            published_at=_now_iso(),
        )
        self._versions.append(version)
        pipeline_bus.write_audit(
            "维度登记",
            f"{dim_table}[{natural_key}]",
            f"新自然键建档，发布首版本 {version.version_id}，生效日 {effective_from}",
            "dim_snapshot",
            "已发布",
        )
        return {"accepted": True, "message": "新维度首版本已发布", "version": version.to_dict()}

    # ---------------------------------------------------------------- 更正落库
    def submit_correction(
        self,
        dim_table: str,
        natural_key_values: dict[str, Any],
        effective_from: str,
        changes: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        """补交一条维度更正。

        规则：
        - 同来源 + 同自然键 + 同生效日 + 同变更内容的幂等指纹只允许落库一次；
        - 生效日必须落在某个既有有效区间内（兼容开放区间），否则无可承接版本；
        - 承接区间在生效日关闭，新版本继承其后半段（含开放上界），保证区间不重叠。
        """
        _parse_date(effective_from)
        if not changes:
            raise DimensionRuleError("更正必须包含至少一个字段变更")
        natural_key = canonical_natural_key(natural_key_values)
        fingerprint_raw = "|".join([
            source.strip(),
            dim_table,
            natural_key,
            effective_from,
            json.dumps(changes, sort_keys=True, ensure_ascii=False),
        ])
        idempotency_key = hashlib.sha256(fingerprint_raw.encode("utf-8")).hexdigest()[:20]

        # 幂等：同一来源的重复更正直接返回首次落库的版本，不再新增。
        if idempotency_key in self._idempotency_keys:
            existing = next(
                v for v in self._versions if v.idempotency_key == idempotency_key
            )
            pipeline_bus.write_audit(
                "维度更正去重",
                f"{dim_table}[{natural_key}]",
                f"来源 {source} 的更正已落库过（版本 {existing.version_id}），重复提交被拦截",
                "dim_snapshot",
                "重复拦截",
            )
            return {
                "accepted": False,
                "duplicate": True,
                "message": "同一来源的更正已落库，重复提交只保留首次版本",
                "version": existing.to_dict(),
            }

        target = self.as_of_version(dim_table, natural_key_values, effective_from)
        if target is None:
            raise DimensionRuleError(
                f"{dim_table}[{natural_key}] 在 {effective_from} 没有可承接的有效区间，"
                "更正不能落入历史空档"
            )

        new_attributes = {**target.attributes, **changes}
        new_effective_to = target.effective_to
        # 先做落库后的重叠校验，再真正改写，保证「两个有效时段不能重叠」。
        self._assert_no_overlap(
            dim_table, natural_key, effective_from, new_effective_to,
            exclude_version_id=target.version_id,
        )

        # 关闭承接区间（开放区间在此被收敛为闭区间，兼容未结束的有效时段）。
        target.effective_to = effective_from
        target.status = STATUS_CLOSED

        new_version = DimensionVersion(
            version_id=self._next_version_id(),
            dim_table=dim_table,
            natural_key=natural_key,
            natural_key_values=dict(natural_key_values),
            version_no=max(v.version_no for v in self._history(dim_table, natural_key)) + 1,
            attributes=new_attributes,
            effective_from=effective_from,
            effective_to=new_effective_to,
            status=STATUS_ACTIVE if new_effective_to == OPEN_ENDED else STATUS_CLOSED,
            source=source,
            is_correction=True,
            idempotency_key=idempotency_key,
            created_at=_now_iso(),
            published_at=_now_iso(),
        )
        self._versions.append(new_version)
        self._idempotency_keys.add(idempotency_key)

        # 写入后立即复检全量区间不变量。
        violations = self._interval_overlap_violations()
        if violations:  # pragma: no cover - 防御分支
            raise DimensionRuleError(f"更正落库后出现区间重叠：{violations}")

        pipeline_bus.write_audit(
            "维度更正发布",
            f"{dim_table}[{natural_key}]",
            f"来源 {source} 更正生效日 {effective_from}，关闭版本 {target.version_id}，"
            f"发布新版本 {new_version.version_no}({new_version.version_id})",
            "dim_snapshot",
            "已发布",
        )
        return {
            "accepted": True,
            "duplicate": False,
            "message": "更正已发布为新维度版本",
            "version": new_version.to_dict(),
            "closed_version_id": target.version_id,
        }

    def _assert_no_overlap(
        self,
        dim_table: str,
        natural_key: str,
        effective_from: str,
        effective_to: str,
        *,
        exclude_version_id: int | None = None,
    ) -> None:
        new_start = _parse_date(effective_from)
        new_end = _parse_date(effective_to)
        for version in self._history(dim_table, natural_key):
            if version.version_id == exclude_version_id:
                continue
            start = _parse_date(version.effective_from)
            end = _parse_date(version.effective_to)
            # 半开区间重叠判定：start < new_end and new_start < end
            if start < new_end and new_start < end:
                raise DimensionRuleError(
                    f"区间 [{effective_from}, {effective_to}) 与版本 {version.version_id} 的 "
                    f"[{version.effective_from}, {version.effective_to}) 重叠"
                )

    def _interval_overlap_violations(self) -> list[dict[str, Any]]:
        violations: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for version in self._versions:
            key = (version.dim_table, version.natural_key)
            if key in seen:
                continue
            seen.add(key)
            history = self._history(*key)
            for earlier, later in zip(history, history[1:]):
                if _parse_date(earlier.effective_to) > _parse_date(later.effective_from):
                    violations.append({
                        "dim_table": key[0],
                        "natural_key": key[1],
                        "left_version_id": earlier.version_id,
                        "right_version_id": later.version_id,
                    })
        return violations

    # ---------------------------------------------------------------- 快照比对
    def run_compare(
        self,
        business_date: str,
        new_catalog_id: str,
        baseline_catalog_id: str | None = None,
    ) -> dict[str, Any]:
        """对同一业务日期的两份快照做字段级比对，差异回写维度记录。

        - baseline 缺省时取每个自然键当前版本的上一版本（更正前版本）；
        - 更正版本导致、且业务日期早已存在的字段差异归因为「更正影响既有日期」；
        - 基线中不存在的自然键归因为「本次新增差异」；
        - 差异清单引用的 version_id 必须与两份快照清单完全一致，否则标记不一致。
        """
        _parse_date(business_date)
        new_catalog = self._catalogs.get(new_catalog_id)
        if new_catalog is None:
            raise DimensionRuleError(f"新查询快照目录 {new_catalog_id} 不存在")
        baseline_catalog = (
            self._catalogs.get(baseline_catalog_id) if baseline_catalog_id else None
        )
        if baseline_catalog_id and baseline_catalog is None:
            raise DimensionRuleError(f"基线快照目录 {baseline_catalog_id} 不存在")

        consistency_checks: list[dict[str, Any]] = []
        differences: list[dict[str, Any]] = []
        all_keys = sorted(
            set(new_catalog.selections)
            | (set(baseline_catalog.selections) if baseline_catalog else set())
        )
        for natural_key in all_keys:
            new_sel = new_catalog.selections.get(natural_key)
            base_sel = (
                baseline_catalog.selections.get(natural_key)
                if baseline_catalog else None
            )
            new_version = self._find_by_id(new_sel["version_id"]) if new_sel else None
            base_version = (
                self._find_by_id(base_sel["version_id"]) if base_sel else self._predecessor(new_version)
            )

            if new_version is None:
                # 新快照缺失、基线存在 → 记录缺失类差异。
                self._append_diff(
                    differences, consistency_checks, business_date,
                    dim_table=new_catalog.dim_table, natural_key=natural_key,
                    field="*记录*", before_value=None, after_value=None,
                    base_version=base_version, new_version=None,
                    new_sel=None, change_type=DIFF_MISSING,
                    reason="基线快照存在但新查询快照缺失",
                )
                continue

            if base_version is None:
                for field_name, after_value in new_version.attributes.items():
                    self._append_diff(
                        differences, consistency_checks, business_date,
                        dim_table=new_version.dim_table, natural_key=natural_key,
                        field=field_name, before_value=None, after_value=after_value,
                        base_version=None, new_version=new_version,
                        new_sel=new_sel, change_type=DIFF_NEW,
                        reason="自然键为本次新增，无历史基线版本",
                    )
                continue

            fields = sorted(set(base_version.attributes) | set(new_version.attributes))
            for field_name in fields:
                before_value = base_version.attributes.get(field_name)
                after_value = new_version.attributes.get(field_name)
                if before_value == after_value:
                    continue
                change_type, reason = self._classify_change(
                    new_version, base_version, business_date
                )
                self._append_diff(
                    differences, consistency_checks, business_date,
                    dim_table=new_version.dim_table, natural_key=natural_key,
                    field=field_name, before_value=before_value, after_value=after_value,
                    base_version=base_version, new_version=new_version,
                    new_sel=new_sel, change_type=change_type, reason=reason,
                )

        version_consistent = all(item["consistent"] for item in consistency_checks)
        summary = {
            "total_keys": len(all_keys),
            "changed_keys": len({d["natural_key"] for d in differences}),
            "difference_rows": len(differences),
            "correction_affecting_existing_date": sum(
                1 for d in differences if d["change_type"] == DIFF_CORRECTION_EXISTING
            ),
            "new_differences": sum(1 for d in differences if d["change_type"] == DIFF_NEW),
            "missing_differences": sum(1 for d in differences if d["change_type"] == DIFF_MISSING),
        }
        result = {
            "business_date": business_date,
            "dim_table": new_catalog.dim_table,
            "baseline_catalog_id": baseline_catalog_id,
            "new_catalog_id": new_catalog_id,
            "version_consistent": version_consistent,
            "consistency_checks": consistency_checks,
            "summary": summary,
            "differences": differences,
            "compared_at": _now_iso(),
        }
        self._differences.append(result)
        pipeline_bus.write_audit(
            "维度快照比对",
            f"{new_catalog.dim_table}@{business_date}",
            f"比对目录 {baseline_catalog_id or '上一版本'} -> {new_catalog_id}，"
            f"差异 {len(differences)} 项，版本一致={version_consistent}",
            "dim_snapshot",
            "已生成",
        )
        return result

    def _predecessor(self, version: DimensionVersion | None) -> DimensionVersion | None:
        if version is None:
            return None
        history = self._history(version.dim_table, version.natural_key)
        earlier = [v for v in history if v.version_no < version.version_no]
        return earlier[-1] if earlier else None

    def _classify_change(
        self,
        new_version: DimensionVersion,
        base_version: DimensionVersion,
        business_date: str,
    ) -> tuple[str, str]:
        if new_version.is_correction and _parse_date(new_version.effective_from) <= _parse_date(business_date):
            return (
                DIFF_CORRECTION_EXISTING,
                f"版本 {new_version.version_id} 是生效日 {new_version.effective_from} 的回溯更正，"
                f"影响业务日期 {business_date} 的既有数据",
            )
        return DIFF_NEW, "常规版本演进产生的字段差异"

    def _append_diff(
        self,
        differences: list[dict[str, Any]],
        consistency_checks: list[dict[str, Any]],
        business_date: str,
        *,
        dim_table: str,
        natural_key: str,
        field: str,
        before_value: Any,
        after_value: Any,
        base_version: DimensionVersion | None,
        new_version: DimensionVersion | None,
        new_sel: dict[str, Any] | None,
        change_type: str,
        reason: str,
    ) -> None:
        self._diff_seq += 1
        diff_id = f"diff-{self._diff_seq:05d}"
        entry = {
            "diff_id": diff_id,
            "dim_table": dim_table,
            "natural_key": natural_key,
            "field": field,
            "before_value": before_value,
            "after_value": after_value,
            "before_version_id": base_version.version_id if base_version else None,
            "after_version_id": new_version.version_id if new_version else None,
            "before_version_no": base_version.version_no if base_version else None,
            "after_version_no": new_version.version_no if new_version else None,
            # 显式记录快照清单版本，供「快照清单与差异清单版本一致」校验。
            "snapshot_version_id": new_sel["version_id"] if new_sel else None,
            "change_type": change_type,
            "reason": reason,
            "business_date": business_date,
            "source": new_version.source if new_version else (base_version.source if base_version else ""),
        }
        differences.append(entry)

        # 版本一致性核对：差异清单版本必须等于新快照清单里的版本。
        expected = new_sel["version_id"] if new_sel else None
        consistent = entry["after_version_id"] == expected
        consistency_checks.append({
            "diff_id": diff_id,
            "natural_key": natural_key,
            "snapshot_version_id": expected,
            "difference_version_id": entry["after_version_id"],
            "consistent": consistent,
        })

        # 差异结果回写到维度记录（新版本与基线版本都留下标注）。
        mark = {
            "diff_id": diff_id,
            "business_date": business_date,
            "field": field,
            "before_value": before_value,
            "after_value": after_value,
            "baseline_version_id": base_version.version_id if base_version else None,
            "change_type": change_type,
            "written_at": _now_iso(),
        }
        if new_version is not None:
            new_version.diff_marks.append(dict(mark))
        if base_version is not None:
            base_version.diff_marks.append({**dict(mark), "role": "baseline"})

    def list_differences(
        self, *, business_date: str | None = None, catalog_id: str | None = None
    ) -> list[dict[str, Any]]:
        results = list(self._differences)
        if business_date:
            results = [r for r in results if r["business_date"] == business_date]
        if catalog_id:
            results = [
                r for r in results
                if r["new_catalog_id"] == catalog_id or r["baseline_catalog_id"] == catalog_id
            ]
        return results

    # ---------------------------------------------------------------- 版本核对
    def reconcile(self) -> dict[str, Any]:
        """核对已发布的维度版本与快照目录、比对实际使用的版本是否一致。"""
        published_ids = {v.version_id for v in self._versions}
        used_ids: set[int] = set()
        catalog_checks: list[dict[str, Any]] = []
        for catalog in self._catalogs.values():
            unresolved: list[int] = []
            for selection in catalog.selections.values():
                version_id = selection["version_id"]
                used_ids.add(version_id)
                if version_id not in published_ids:
                    unresolved.append(version_id)
            catalog_checks.append({
                "catalog_id": catalog.catalog_id,
                "business_date": catalog.business_date,
                "selection_count": len(catalog.selections),
                "unresolved_version_ids": unresolved,
                "consistent": not unresolved,
            })
        for report in self._differences:
            for diff in report["differences"]:
                if diff["before_version_id"] is not None:
                    used_ids.add(diff["before_version_id"])
                if diff["after_version_id"] is not None:
                    used_ids.add(diff["after_version_id"])

        missing_in_published = sorted(used_ids - published_ids)
        overlap_violations = self._interval_overlap_violations()
        diff_version_mismatches = [
            {"report_business_date": report["business_date"], "diff_id": check["diff_id"]}
            for report in self._differences
            for check in report["consistency_checks"]
            if not check["consistent"]
        ]
        consistent = (
            not missing_in_published
            and not overlap_violations
            and not diff_version_mismatches
            and all(item["consistent"] for item in catalog_checks)
        )
        return {
            "checked_at": _now_iso(),
            "published_version_count": len(published_ids),
            "used_version_count": len(used_ids),
            "published_version_ids": sorted(published_ids),
            "used_version_ids": sorted(used_ids),
            "missing_in_published": missing_in_published,
            "interval_overlap_violations": overlap_violations,
            "catalog_checks": catalog_checks,
            "difference_version_mismatches": diff_version_mismatches,
            "consistent": consistent,
        }


# 单例存储，与 pipeline_bus 一样在进程内共享。
dimension_store = DimensionVersionStore()
