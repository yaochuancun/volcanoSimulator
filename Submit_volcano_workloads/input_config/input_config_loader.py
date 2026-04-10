"""Load YAML under input_config and convert to strings or paths needed by the Volcano simulator HTTP API.

Covers: cluster/workload YAML → simulator-side YAML text; plugins → scheduler config YAML + resolved result output directory.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

_FLEXNPU_CORE_KEY = "volcano.sh/flexnpu-core.percentage"
# Matches flexnpu_util_report: pre-rounding flexnpu_core per container (requests preferred), for utilization stats
FLEXNPU_CORE_RAW_BY_CONTAINER_ANN = (
    "volcano.sh/flexnpu-core.percentage-raw-by-container"
)


def _ceil_to_step(value: float, step: float) -> float:
    """Round value up to a multiple of step (unchanged if step <= 0)."""
    if step <= 0:
        return value
    return math.ceil(value / step) * step


def _round_resource_map(
    resources: Optional[Dict[str, Any]], granularity_percent: float
) -> None:
    """In-place: ceil **flexnpu_core** requests/limits to granularity; flexnpu_memory is not rounded by granularity."""
    if not resources or granularity_percent <= 0:
        return
    key = _FLEXNPU_CORE_KEY
    if key not in resources:
        return
    raw = resources[key]
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return
    rounded = _ceil_to_step(v, float(granularity_percent))
    if rounded == int(rounded):
        resources[key] = str(int(rounded))
    else:
        resources[key] = str(rounded)


def _normalize_task_templates(tasks: Optional[List[Dict[str, Any]]], granularity: float) -> None:
    """Normalize task shape to template.spec and apply granularity rounding to container flexnpu_core requests/limits.

    When granularity > 0, store **pre-rounding** flexnpu_core per container in Pod annotation
    ``FLEXNPU_CORE_RAW_BY_CONTAINER_ANN`` (JSON: container name -> value) so reports can separate utilization vs allocation.
    """
    if not tasks:
        return
    for task in tasks:
        if "template" not in task and "spec" in task:
            task["template"] = {"spec": task.pop("spec")}
        tmpl = task.get("template") or {}
        pod_spec = tmpl.get("spec") or {}
        if granularity and float(granularity) > 0:
            g = float(granularity)
            raw_by_c: Dict[str, float] = {}
            for container in pod_spec.get("containers") or []:
                if not isinstance(container, dict):
                    continue
                cname = str(container.get("name") or "__default__")
                res = container.get("resources") or {}
                for rk in ("requests", "limits"):
                    m = res.get(rk)
                    if not isinstance(m, dict) or _FLEXNPU_CORE_KEY not in m:
                        continue
                    try:
                        v = float(m[_FLEXNPU_CORE_KEY])
                    except (TypeError, ValueError):
                        continue
                    rounded = _ceil_to_step(v, g)
                    if rk == "requests":
                        raw_by_c[cname] = v
                    elif cname not in raw_by_c:
                        raw_by_c[cname] = v
                    if rounded == int(rounded):
                        m[_FLEXNPU_CORE_KEY] = str(int(rounded))
                    else:
                        m[_FLEXNPU_CORE_KEY] = str(rounded)
            if raw_by_c:
                # In Volcano, Pod annotations live on template.metadata; must match simulator NewJobInfoV2 merge onto Pod
                meta = tmpl.setdefault("metadata", {})
                ann = meta.setdefault("annotations", {})
                ann[FLEXNPU_CORE_RAW_BY_CONTAINER_ANN] = json.dumps(
                    raw_by_c, ensure_ascii=False
                )
        else:
            for container in pod_spec.get("containers") or []:
                if not isinstance(container, dict):
                    continue
                res = container.get("resources") or {}
                _round_resource_map(res.get("requests"), 0)
                _round_resource_map(res.get("limits"), 0)


def cluster_input_to_simulator_yaml(doc: Dict[str, Any]) -> str:
    """Convert input_config-style cluster document to simulator top-level ``cluster:`` YAML text."""
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
    """Convert workload document to simulator YAML with top-level ``jobs:`` only.

    - Ceil **flexnpu_core** request/limit to ``spec.npuGranularityPercent`` step (memory not rounded);
    - Map ``tasks[].spec`` to Volcano-compatible ``tasks[].template.spec``.
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
    """Load cluster YAML from file and return simulator nodes YAML string."""
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError("cluster config must be a YAML mapping")
    return cluster_input_to_simulator_yaml(doc)


def cluster_yaml_text_to_simulator_yaml(yaml_text: str) -> str:
    """Parse cluster document from YAML text and return simulator nodes YAML string."""
    doc = yaml.safe_load(yaml_text)
    if not isinstance(doc, dict):
        raise ValueError("cluster config must be a YAML mapping")
    return cluster_input_to_simulator_yaml(doc)


def workload_yaml_text_to_simulator_yaml(yaml_text: str) -> str:
    """Parse workload document from YAML text and return simulator jobs YAML string."""
    doc = yaml.safe_load(yaml_text)
    if not isinstance(doc, dict):
        raise ValueError("workload config must be a YAML mapping")
    return workload_input_to_simulator_yaml(doc)


def workload_doc_to_simulator_yaml(doc: Dict[str, Any]) -> str:
    """Convert in-memory workload document (e.g. after scaling) to simulator jobs YAML string."""
    if not isinstance(doc, dict):
        raise ValueError("workload config must be a mapping")
    return workload_input_to_simulator_yaml(doc)


def workload_npu_granularity_percent_from_doc(doc: Mapping[str, Any]) -> float:
    """Read npuGranularityPercent from a parsed workload document."""
    return workload_npu_granularity_percent(doc)


def workload_npu_granularity_percent(doc: Mapping[str, Any]) -> float:
    """Read top-level ``spec.npuGranularityPercent`` from workload doc (same as ``workload_input_to_simulator_yaml``)."""
    try:
        return float((doc.get("spec") or {}).get("npuGranularityPercent") or 0)
    except (TypeError, ValueError):
        return 0.0


def workload_npu_granularity_percent_from_file(path: str) -> float:
    """Read ``npuGranularityPercent`` from workload YAML file; return 0 if invalid or missing."""
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        return 0.0
    return workload_npu_granularity_percent(doc)


def load_workload_for_simulator(path: str) -> str:
    """Load workload YAML from file and return simulator jobs YAML string."""
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError("workload config must be a YAML mapping")
    return workload_input_to_simulator_yaml(doc)


def resolve_out_dir_pattern(out_dir: str, now: Optional[datetime] = None) -> str:
    """Replace ``{date}`` in ``outDir`` with a ``YYYY-MM-DD-HH-MM-SS`` timestamp (default: now)."""
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d-%H-%M-%S")
    return out_dir.replace("{date}", stamp)


def load_plugins_for_simulator(path: str) -> Tuple[str, str]:
    """Load plugins YAML; return (scheduler config YAML string, absolute result out dir after expanding {date}).

    The ``scheduler`` block is sent as ``/step`` conf; ``output.outDir`` may use a ``{date}`` placeholder.
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


def plugins_document_scheduler_and_outdir(
    doc: Dict[str, Any],
    result_out_dir: str,
) -> Tuple[str, str]:
    """Take scheduler block from a parsed plugins document and set result directory to ``result_out_dir`` (absolute)."""
    if not isinstance(doc, dict):
        raise ValueError("plugins config must be a YAML mapping")
    scheduler = doc.get("scheduler")
    if not scheduler:
        raise ValueError("plugins config: missing 'scheduler'")
    conf_str = yaml.safe_dump(scheduler, sort_keys=False, allow_unicode=True)
    out_dir = os.path.normpath(os.path.abspath(result_out_dir))
    return conf_str, out_dir
