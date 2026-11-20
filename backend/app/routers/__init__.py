"""业务路由汇总：维度快照等固定前缀路由在前，通用模块资源路由在后。

通用资源路由含动态段 /api/{module}，固定前缀的领域路由必须先注册，
否则会被动态路径抢先匹配。
"""
from __future__ import annotations

from app.routers import dim_snapshot, resource

ROUTERS = [dim_snapshot, resource]
