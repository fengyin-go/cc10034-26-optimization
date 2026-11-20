# 实时数仓CDC变更捕获中心

面向实时数仓建设场景，提供Binlog解析、变更事件捕获、DDL追踪、SCD处理和下游分发同步的CDC治理平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| Binlog解析 | `binlog_parser` | 解析配置 | 配置名称、源数据库、解析模式 |
| 变更事件 | `change_event` | 变更记录 | 事件ID、来源表、操作类型 |
| DDL追踪 | `ddl_tracker` | DDL记录 | 记录ID、变更数据库、变更表名 |
| Schema同步 | `schema_sync` | 同步任务 | 任务名称、源表、目标表 |
| 采集通道 | `cdc_channel` | 通道实例 | 通道名称、Binlog源、输出目标 |
| 检查点管理 | `checkpoint_manager` | 检查点 | 检查点ID、所属通道、Binlog文件 |
| 位点存储 | `position_store` | 位点记录 | 记录ID、数据库实例、Binlog名称 |
| 格式适配 | `format_adapter` | 适配规则 | 规则名称、输入格式、输出格式 |
| 缓慢变化维 | `scd_handler` | SCD配置 | 配置名称、维度表、SCD类型 |
| 去重窗口 | `dedup_window` | 窗口配置 | 配置名称、去重键、窗口大小 |
| 路由分发 | `route_dispatcher` | 路由规则 | 规则名称、匹配条件、目标通道 |
| 回填位点 | `backfill_position` | 回填任务 | 任务名称、源位点、目标位点 |
| 延迟监控 | `lag_monitor` | 延迟指标 | 指标时间、所属通道、延迟秒数 |
| 快照比对 | `snapshot_compare` | 比对报告 | 报告名称、源表名、目标表名 |
| 事件过滤 | `event_filter` | 过滤规则 | 规则名称、过滤类型、匹配表达式 |
| 分区同步 | `partition_sync` | 同步配置 | 配置名称、源分区策略、目标分区策略 |
| 审计轨迹 | `audit_trail` | 审计日志 | 审计时间、操作类型、目标对象 |
| 故障切换 | `failover_group` | 切换组 | 组名、主通道、备用通道 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 维度快照与历史版本（SCD Type2 增强）

`backend/app/dim_snapshot.py` 在通用模块之外提供严格的版本化维度存储，
路由前缀 `/api/dim-snapshot`（在通用 `/api/{module}` 之前注册，不会被动态路径抢占），
前端入口为「维度快照」页面（`/dim_snapshot`）。

核心不变量：

- 版本区间 `[effective_from, effective_to)` 半开、两个有效时段不重叠、只追加不改写；
  回溯更正先关闭承接区间（含未结束的开放区间），再发布新版本，历史版本永久保留。
- 指定时刻快照按 `effective_from <= 业务日期 < effective_to` 命中唯一版本，
  快照行含维表、自然键和版本。
- 每次查询开启固化一份**快照目录（catalog）**，记录每个自然键选中的 `version_id`；
  跨页只认目录，后续更正不影响旧查询，旧结果可随时复现。
- 同一来源更正按幂等指纹去重，重复提交只落库一次；落入历史空档的更正直接拒绝。
- 快照比对产出字段级差异并回写维度记录，差异清单引用版本必须与快照清单一致；
  `reconcile` 同步核对已发布版本与目录、比对实际使用的版本。
- 业务日期与录入日期冲突时，新查询按业务日期命中当前已发布版本，并以
  `backdated_correction` 标记回溯更正；差异据此区分为「更正影响既有日期」与
  「本次新增差异」。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/dim-snapshot/versions/{dim_table}` | 已发布历史版本，可按 `customer_id` 过滤 |
| GET | `/api/dim-snapshot/as-of` | 单键指定时刻版本定位（不固化目录） |
| GET | `/api/dim-snapshot/snapshot` | 指定时刻快照分页，带 `catalog_id` 时固定版本 |
| POST | `/api/dim-snapshot/queries` | 开启新查询并固化快照目录 |
| GET | `/api/dim-snapshot/catalogs` | 快照目录清单 |
| POST | `/api/dim-snapshot/dimensions` | 新自然键建档（首版本） |
| POST | `/api/dim-snapshot/corrections` | 补交更正（幂等、关闭承接区间、发新版本） |
| POST | `/api/dim-snapshot/compare` | 新/旧快照字段级比对，差异回写 |
| GET | `/api/dim-snapshot/differences` | 差异清单 |
| GET | `/api/dim-snapshot/reconcile` | 已发布版本与比对版本核对 |

服务层测试：`backend/tests/test_dim_snapshot.py`（`python -I tests/test_dim_snapshot.py`）。

领域：数据工程
子方向：数仓与数据湖
