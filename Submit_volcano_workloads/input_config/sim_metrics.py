"""Compute frontend chart metrics from simulator stepResult.

- Allocation rate (line chart): **arithmetic mean** of per-node flexnpu_core_allocation_rate (same as Node_desc), in %.
- Schedulable pod count: number of Pods with phase Running in the **first** scheduling snapshot (excludes Binding/Pending).
- Fragmentation: (cluster remaining flexnpu_core sum − max per-node remaining flexnpu_core) / cluster flexnpu_core capacity;
  "remaining" = node Allocatable − Used (same scale as Node_desc, ScalarResources / 1000).
  Interpretation: ~0 if all spare capacity sits on one node; higher when spare is spread across nodes.
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
    """Count only Pods with phase == Running (first stepResult snapshot)."""
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
    """Return None if Nodes cannot be parsed or is empty."""
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

    # Extra: mean memory allocation rate (optional for frontend)
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
