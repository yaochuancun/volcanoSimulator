"""从仿真器 stepResult 计算前端图表用指标。

- 分配率（折线图）：各节点 flexnpu_core_allocation_rate（与 Node_desc 一致）的 **算术平均**（%）。
- 可分配 Pod 数：**第一次**调度快照中 phase 为 Running 的 Pod 个数（不含 Binding/Pending）。
- 碎片率：(集群剩余 flexnpu_core 总量 − 单节点剩余 flexnpu_core 的最大值) / 集群 flexnpu_core 总容量；
  其中「剩余」= 节点 Allocatable − Used（与 Node_desc 同量级，ScalarResources / 1000）。
  含义：若全部剩余集中在一个节点上则接近 0；剩余分散在多个节点上则升高。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from input_config.flexnpu_util_report import CORE_RES, MEM_RES, _scalar_from_resource

_NODE_AGG_SCALE = 1000.0


def _node_used_alloc_core(
    ninfo: Mapping[str, Any],
) -> tuple[float, float]:
    used = ninfo.get("Used") or ninfo.get("used")
    alloc = ninfo.get("Allocatable") or ninfo.get("allocatable")
    u_c = _scalar_from_resource(used, CORE_RES) / _NODE_AGG_SCALE
    a_c = _scalar_from_resource(alloc, CORE_RES) / _NODE_AGG_SCALE
    return u_c, a_c


def count_running_pods_first_snapshot(jobs: Mapping[str, Any]) -> int:
    """仅统计 phase == Running 的 Pod（第一次 stepResult 快照）。"""
    n = 0
    for _jname, job in (jobs or {}).items():
        if not isinstance(job, dict):
            continue
        tasks = job.get("Tasks") or job.get("tasks") or {}
        if not isinstance(tasks, dict):
            continue
        for _tname, task in tasks.items():
            if not isinstance(task, dict):
                continue
            pod = task.get("Pod") or task.get("pod") or {}
            if not isinstance(pod, dict):
                continue
            status = pod.get("status") or {}
            phase = (status.get("phase") or "").strip()
            if phase == "Running":
                n += 1
    return n


def compute_chart_metrics(resultdata: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """无法解析 Nodes 时返回 None。"""
    nodes = resultdata.get("Nodes") or resultdata.get("nodes") or {}
    jobs = resultdata.get("Jobs") or resultdata.get("jobs") or {}
    if not isinstance(nodes, dict) or not nodes:
        return None

    rates: List[float] = []
    total_cap = 0.0
    total_remain = 0.0
    per_node_remain: List[float] = []

    for _nname, ninfo in nodes.items():
        if not isinstance(ninfo, dict):
            continue
        u_c, a_c = _node_used_alloc_core(ninfo)
        if a_c > 1e-9:
            rates.append(100.0 * u_c / a_c)
        total_cap += a_c
        rem = max(0.0, a_c - u_c)
        per_node_remain.append(rem)
        total_remain += rem

    allocation_rate_avg = sum(rates) / len(rates) if rates else 0.0
    max_node_remain = max(per_node_remain) if per_node_remain else 0.0

    if total_cap < 1e-9:
        fragmentation_rate = 0.0
    else:
        frag_numerator = total_remain - max_node_remain
        fragmentation_rate = max(0.0, min(100.0, 100.0 * frag_numerator / total_cap))

    running_pods = count_running_pods_first_snapshot(jobs)

    # 附：内存侧平均分配率（前端可选用）
    mem_rates: List[float] = []
    for _nname, ninfo in nodes.items():
        if not isinstance(ninfo, dict):
            continue
        used = ninfo.get("Used") or ninfo.get("used")
        alloc = ninfo.get("Allocatable") or ninfo.get("allocatable")
        u_m = _scalar_from_resource(used, MEM_RES) / _NODE_AGG_SCALE
        a_m = _scalar_from_resource(alloc, MEM_RES) / _NODE_AGG_SCALE
        if a_m > 1e-9:
            mem_rates.append(100.0 * u_m / a_m)
    allocation_memory_rate_avg = sum(mem_rates) / len(mem_rates) if mem_rates else 0.0

    return {
        "allocation_rate_avg": round(allocation_rate_avg, 4),
        "allocation_memory_rate_avg": round(allocation_memory_rate_avg, 4),
        "running_pods": running_pods,
        "fragmentation_rate": round(fragmentation_rate, 4),
        "node_count": len(rates),
        "cluster_flexnpu_core_capacity": round(total_cap, 6),
        "cluster_flexnpu_core_remaining": round(total_remain, 6),
        "max_single_node_flexnpu_core_remaining": round(max_node_remain, 6),
    }
