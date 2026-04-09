"""Workload 缩放：对每个 task 的 replicas 按系数做乘法后向上取整。"""

from __future__ import annotations

import copy
import math
from typing import Any, Dict


def scale_workload_document(doc: Dict[str, Any], factor: float) -> Dict[str, Any]:
    """返回深拷贝后的文档，jobs[].spec.tasks[].replicas -> ceil(replicas * factor)，至少为 1。"""
    out = copy.deepcopy(doc)
    if not isinstance(out, dict):
        return out
    try:
        f = float(factor)
    except (TypeError, ValueError):
        f = 1.0
    jobs = out.get("jobs")
    if not isinstance(jobs, list):
        return out
    for job in jobs:
        if not isinstance(job, dict):
            continue
        spec = job.get("spec") or {}
        tasks = spec.get("tasks")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if "replicas" not in task:
                continue
            try:
                base = int(task["replicas"])
            except (TypeError, ValueError):
                try:
                    base = int(float(task["replicas"]))
                except (TypeError, ValueError):
                    continue
            task["replicas"] = max(1, int(math.ceil(base * f)))
    return out
