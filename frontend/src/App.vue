<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">实时数仓CDC变更捕获中心</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">面向实时数仓建设场景，提供Binlog解析、变更事件捕获、DDL追踪、SCD处理和下游分发同步的CDC治理平台</span>
        <span class="head-user">当前值班：数据工程组</span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
const navItems = [{ label: "运营概览", path: "/" }, { label: "维度快照", path: "/dim_snapshot" }, { label: "Binlog解析", path: "/binlog_parser" }, { label: "变更事件", path: "/change_event" }, { label: "DDL追踪", path: "/ddl_tracker" }, { label: "Schema同步", path: "/schema_sync" }, { label: "采集通道", path: "/cdc_channel" }, { label: "检查点管理", path: "/checkpoint_manager" }, { label: "位点存储", path: "/position_store" }, { label: "格式适配", path: "/format_adapter" }, { label: "缓慢变化维", path: "/scd_handler" }, { label: "去重窗口", path: "/dedup_window" }, { label: "路由分发", path: "/route_dispatcher" }, { label: "回填位点", path: "/backfill_position" }, { label: "延迟监控", path: "/lag_monitor" }, { label: "快照比对", path: "/snapshot_compare" }, { label: "事件过滤", path: "/event_filter" }, { label: "分区同步", path: "/partition_sync" }, { label: "审计轨迹", path: "/audit_trail" }, { label: "故障切换", path: "/failover_group" }]
</script>
