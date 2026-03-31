"""Load input_config YAML and convert to formats expected by the Volcano simulator HTTP API."""

from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import yaml

_FLEXNPU_CORE_KEY = "volcano.sh/flexnpu-core.percentage"
_FLEXNPU_MEM_KEY = "volcano.sh/flexnpu-memory.128mi"


def _ceil_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.ceil(value / step) * step


def _round_resource_map(
    resources: Optional[Dict[str, Any]], granularity_percent: float
) -> None:
    if not resources or granularity_percent <= 0:
        return
    for key in (_FLEXNPU_CORE_KEY, _FLEXNPU_MEM_KEY):
        if key not in resources:
            continue
        raw = resources[key]
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        rounded = _ceil_to_step(v, float(granularity_percent))
        if rounded == int(rounded):
            resources[key] = str(int(rounded))
        else:
            resources[key] = str(rounded)


def _normalize_task_templates(tasks: Optional[List[Dict[str, Any]]], granularity: float) -> None:
    if not tasks:
        return
    for task in tasks:
        if "template" not in task and "spec" in task:
            task["template"] = {"spec": task.pop("spec")}
        tmpl = task.get("template") or {}
        pod_spec = tmpl.get("spec") or {}
        for container in pod_spec.get("containers") or []:
            res = container.get("resources") or {}
            _round_resource_map(res.get("requests"), granularity)
            _round_resource_map(res.get("limits"), granularity)


def cluster_input_to_simulator_yaml(doc: Dict[str, Any]) -> str:
    """input_config/cluster/cluster.yaml -> legacy simulator nodes YAML (top-level ``cluster:``)."""
    nodes = doc.get("nodes")
    if not nodes:
        raise ValueError("cluster config: missing 'nodes' list")

    cluster: List[Dict[str, Any]] = []
    for n in nodes:
        name = n.get("name")
        if not name:
            raise ValueError("cluster config: node entry missing 'name'")
        entry: Dict[str, Any] = {
            "metadata": {
                "name": name,
                "labels": n.get("labels") or {},
                "annotations": n.get("annotations") or {},
            },
            "spec": n.get("spec") if n.get("spec") is not None else {"unschedulable": False},
            "status": n.get("status") or {},
        }
        cluster.append(entry)

    return yaml.safe_dump({"cluster": cluster}, sort_keys=False, allow_unicode=True)


def workload_input_to_simulator_yaml(doc: Dict[str, Any]) -> str:
    """input_config/workload/workload.yaml -> simulator jobs YAML (top-level ``jobs:`` only).

    - Reads ``spec.npuGranularityPercent`` and rounds flexnpu request/limit values up to that step.
    - Maps ``tasks[].spec`` to ``tasks[].template.spec`` for Volcano Job compatibility.
    """
    spec_root = doc.get("spec") or {}
    granularity = float(spec_root.get("npuGranularityPercent") or 0)

    jobs = doc.get("jobs")
    if jobs is None:
        raise ValueError("workload config: missing 'jobs'")

    out_jobs: List[Dict[str, Any]] = []
    for job in jobs:
        job_copy = yaml.safe_load(yaml.safe_dump(job, sort_keys=False, allow_unicode=True))
        jspec = job_copy.get("spec") or {}
        _normalize_task_templates(jspec.get("tasks"), granularity)
        job_copy["spec"] = jspec
        out_jobs.append(job_copy)

    return yaml.safe_dump({"jobs": out_jobs}, sort_keys=False, allow_unicode=True)


def load_cluster_for_simulator(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError("cluster config must be a YAML mapping")
    return cluster_input_to_simulator_yaml(doc)


def load_workload_for_simulator(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError("workload config must be a YAML mapping")
    return workload_input_to_simulator_yaml(doc)


def resolve_out_dir_pattern(out_dir: str, now: Optional[datetime] = None) -> str:
    """Replace ``{date}`` in outDir with a timestamp (default: now)."""
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d-%H-%M-%S")
    return out_dir.replace("{date}", stamp)


def load_plugins_for_simulator(path: str) -> Tuple[str, str]:
    """Load input_config/plugins YAML.

    Returns:
        (scheduler_conf_yaml, resolved_out_dir)

    ``scheduler`` block is sent to ``/step`` as scheduler configuration.
    ``output.outDir`` supports ``{date}`` placeholder.
    """
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError("plugins config must be a YAML mapping")

    scheduler = doc.get("scheduler")
    if not scheduler:
        raise ValueError("plugins config: missing 'scheduler'")

    conf_str = yaml.safe_dump(scheduler, sort_keys=False, allow_unicode=True)

    output = doc.get("output") or {}
    out_dir = output.get("outDir") or "./result/{date}"
    out_dir = resolve_out_dir_pattern(out_dir)
    if not os.path.isabs(out_dir):
        out_dir = os.path.normpath(os.path.join(os.getcwd(), out_dir))
    return conf_str, out_dir
