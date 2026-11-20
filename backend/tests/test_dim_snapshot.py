"""维度快照引擎端到端场景测试（不依赖 pytest，直接 python 运行）。

覆盖题述完整链路：
  旧查询固化 -> 回溯地区更正 -> 幂等/区间规则 -> 新查询选版 ->
  快照比对差异回写 -> 版本核对 -> 旧查询可复现/跨页固定版本。
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/workspace/backend")

from app.dim_snapshot import (  # noqa: E402
    DIFF_CORRECTION_EXISTING,
    OPEN_ENDED,
    DimensionRuleError,
    dimension_store,
)

DIM = "dim_customer"
BIZ_DATE = "2025-08-01"
NK17 = {"customer_id": 17}
failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -> {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# 每个进程从种子态开始，保证可重复。
dimension_store.reset()

# ------------------------------------------------------------ 1. 旧查询
section("1. 查询 customer_id=17 在 2025-08-01 的维度数据")
before = dimension_store.as_of_version(DIM, NK17, BIZ_DATE)
check("更正前 8/1 命中版本 v2(1002)", before is not None and before.version_id == 1002)
check("更正前地区为「华东」", before is not None and before.attributes["region"] == "华东")
check("承接版本是未结束的开放区间", before is not None and before.effective_to == OPEN_ENDED)

# 半开区间边界：生效日当天命中新版本，失效日当天不命中。
edge_v2 = dimension_store.as_of_version(DIM, NK17, "2025-06-01")
edge_v1 = dimension_store.as_of_version(DIM, NK17, "2025-05-31")
check("边界 2025-06-01 命中 v2", edge_v2 is not None and edge_v2.version_id == 1002)
check("边界 2025-05-31 命中 v1", edge_v1 is not None and edge_v1.version_no == 1)

# 旧查询开启，固化快照目录。
old_query = dimension_store.open_query(DIM, BIZ_DATE, note="旧查询：更正前对账")
old_catalog_id = old_query["catalog"]["catalog_id"]
old_sel_17 = [
    item for item in old_query["items"] if item["natural_key"] == "customer_id=17"
][0]
check("旧查询固化目录 cat-0001", old_catalog_id == "cat-0001", old_catalog_id)
check("旧查询目录固定 version_id=1002", old_sel_17["version_id"] == 1002)
check("旧查询行 pinned=True", old_sel_17["pinned"] is True)
check("旧查询地区为「华东」", old_sel_17["attributes"]["region"] == "华东")

# ------------------------------------------------------------ 2. 补交更正
section("2. 补交同日生效（2025-08-01）的地区更正：华东 -> 华南")
correction = dimension_store.submit_correction(
    dim_table=DIM,
    natural_key_values=NK17,
    effective_from=BIZ_DATE,
    changes={"region": "华南"},
    source="region-rework-202508",
)
new_version_id = correction["version"]["version_id"]
check("更正被接受为新版本", correction["accepted"] is True)
check("新版本号 v3", correction["version"]["version_no"] == 3)
check("新版本地区为「华南」", correction["version"]["attributes"]["region"] == "华南")
check("新版本承接开放上界", correction["version"]["effective_to"] == OPEN_ENDED)

closed = dimension_store._find_by_id(correction["closed_version_id"])
check("旧开放区间在生效日关闭", closed is not None and closed.effective_to == BIZ_DATE)
check("被关闭版本状态=已失效", closed is not None and closed.status == "已失效")

# 区间不变量：三段互不重叠且连续。
history17 = dimension_store.list_versions(DIM, NK17)
intervals = [(v["effective_from"], v["effective_to"]) for v in history17]
check("版本区间连续不重叠", intervals == [
    ("2025-01-01", "2025-06-01"),
    ("2025-06-01", "2025-08-01"),
    ("2025-08-01", OPEN_ENDED),
], intervals)
check("引擎复检无重叠违规", dimension_store._interval_overlap_violations() == [])

# ------------------------------------------------------------ 3. 幂等
section("3. 重复提交同一来源的更正")
duplicate = dimension_store.submit_correction(
    dim_table=DIM,
    natural_key_values=NK17,
    effective_from=BIZ_DATE,
    changes={"region": "华南"},
    source="region-rework-202508",
)
check("重复提交被拒绝落库", duplicate["accepted"] is False and duplicate["duplicate"] is True)
check("重复提交返回首次版本", duplicate["version"]["version_id"] == new_version_id)
check("版本总数未增加", len(dimension_store.list_versions(DIM, NK17)) == 3)

# 空档更正必须被拒绝（没有可承接区间）。
try:
    dimension_store.submit_correction(
        DIM, NK17, "2024-12-31", {"region": "华中"}, "gap-source"
    )
    check("历史空档更正被拦截", False)
except DimensionRuleError:
    check("历史空档更正被拦截", True)

# ------------------------------------------------------------ 4. 新查询选版
section("4. 更正后对同一业务日期开启新查询")
current = dimension_store.as_of_version(DIM, NK17, BIZ_DATE)
check("即时 as-of 现在命中新版本 v3", current is not None and current.version_id == new_version_id)
check("新查询实际采用地区「华南」", current is not None and current.attributes["region"] == "华南")

new_query = dimension_store.open_query(DIM, BIZ_DATE, note="新查询：更正后对账")
new_catalog_id = new_query["catalog"]["catalog_id"]
new_sel_17 = [item for item in new_query["items"] if item["natural_key"] == "customer_id=17"][0]
check("新查询固化目录 cat-0002", new_catalog_id == "cat-0002", new_catalog_id)
check("新查询固定新版本 version_id=%d" % new_version_id, new_sel_17["version_id"] == new_version_id)
check("新查询标记回溯更正 backdated_correction=True", new_sel_17["backdated_correction"] is True)
check("新查询地区为「华南」", new_sel_17["attributes"]["region"] == "华南")

# 目录绑定一致性：拿新目录去查别的业务日期必须报错。
try:
    dimension_store.snapshot(DIM, "2025-09-01", catalog_id=new_catalog_id)
    check("目录跨业务日期误用被拦截", False)
except DimensionRuleError:
    check("目录跨业务日期误用被拦截", True)

# ------------------------------------------------------------ 5. 旧查询可复现 + 跨页固定
section("5. 旧查询仍可复现，且跨页固定同一版本")
old_page1 = dimension_store.snapshot(DIM, BIZ_DATE, page=1, size=2, catalog_id=old_catalog_id)
old_page2 = dimension_store.snapshot(DIM, BIZ_DATE, page=2, size=2, catalog_id=old_catalog_id)
pinned_ids = sorted(
    item["version_id"] for item in old_page1["items"] + old_page2["items"]
)
catalog_ids = sorted(
    sel["version_id"] for sel in dimension_store._catalogs[old_catalog_id].selections.values()
)
check("跨页版本与旧目录完全一致", pinned_ids == catalog_ids, (pinned_ids, catalog_ids))
old_17_pages = [
    item for item in old_page1["items"] + old_page2["items"]
    if item["natural_key"] == "customer_id=17"
][0]
check("旧查询复现地区仍为「华东」", old_17_pages["attributes"]["region"] == "华东")
check("旧查询复现仍 pinned 到 1002", old_17_pages["version_id"] == 1002)

# ------------------------------------------------------------ 6. 快照比对
section("6. 新查询与旧查询做快照比对")
report = dimension_store.run_compare(
    business_date=BIZ_DATE,
    new_catalog_id=new_catalog_id,
    baseline_catalog_id=old_catalog_id,
)
region_diffs = [d for d in report["differences"] if d["field"] == "region"]
check("比对产出 region 字段差异", len(region_diffs) == 1)
if region_diffs:
    rd = region_diffs[0]
    check("差异值 华东 -> 华南", rd["before_value"] == "华东" and rd["after_value"] == "华南")
    check("差异归因=更正影响既有日期", rd["change_type"] == DIFF_CORRECTION_EXISTING)
    check("差异清单版本=新快照清单版本", rd["snapshot_version_id"] == new_version_id)
    check("差异 before/after 版本号正确",
          rd["before_version_id"] == 1002 and rd["after_version_id"] == new_version_id)
check("快照清单与差异清单版本一致", report["version_consistent"] is True)
check("汇总：1 个更正影响既有日期",
      report["summary"]["correction_affecting_existing_date"] == 1)
check("汇总：差异行数=1", report["summary"]["difference_rows"] == 1)

# 差异结果回写到维度记录。
new_version_row = dimension_store._find_by_id(new_version_id)
old_version_row = dimension_store._find_by_id(1002)
check("新版本记录已回写差异标注",
      any(m["field"] == "region" for m in new_version_row.diff_marks))
check("基线版本记录留下 baseline 标注",
      any(m.get("role") == "baseline" and m["field"] == "region"
          for m in old_version_row.diff_marks))

# ------------------------------------------------------------ 7. 版本核对
section("7. 同步核对已发布版本与比对使用版本")
reconcile = dimension_store.reconcile()
check("核对结果 consistent=True", reconcile["consistent"] is True, reconcile)
check("无区间重叠违规", reconcile["interval_overlap_violations"] == [])
check("无未发布版本引用", reconcile["missing_in_published"] == [])
check("两个快照目录全部可解析",
      all(c["consistent"] for c in reconcile["catalog_checks"]))
check("差异版本无错配", reconcile["difference_version_mismatches"] == [])
used = set(reconcile["used_version_ids"])
check("比对使用版本包含 1002 与新版本", {1002, new_version_id}.issubset(used), used)

# ------------------------------------------------------------ 8. 新增差异归因
section("8. 新增自然键归因为「本次新增差异」（扩展验证）")
# 先建不含新客户的基线目录，再登记新客户，模拟本次新增的自然键。
base_query2 = dimension_store.open_query(DIM, BIZ_DATE, note="新增前基线查询")
base_catalog = dimension_store._catalogs[base_query2["catalog_id"]]
dimension_store.register_dimension(
    DIM, {"customer_id": 20}, BIZ_DATE,
    {"customer_name": "新客户", "region": "东北", "customer_level": "普通"},
    "onboarding-202508",
)
new_query2 = dimension_store.open_query(DIM, BIZ_DATE, note="含新增客户的查询")
# 基线目录天然不含 customer 20（建档在目录固化之后），无需手工裁剪。
report2 = dimension_store.run_compare(
    BIZ_DATE, new_query2["catalog_id"], base_query2["catalog_id"]
)
new_diffs = [d for d in report2["differences"] if d["natural_key"] == "customer_id=20"]
check("新增键差异归因=本次新增差异",
      bool(new_diffs) and all(d["change_type"] == "本次新增差异" for d in new_diffs),
      [d["change_type"] for d in new_diffs])
check("新增键的三个属性各有一条差异", len(new_diffs) == 3, len(new_diffs))

print("\n" + ("全部通过 ✅" if not failures else f"失败 {len(failures)} 项：{failures}"))
sys.exit(1 if failures else 0)
