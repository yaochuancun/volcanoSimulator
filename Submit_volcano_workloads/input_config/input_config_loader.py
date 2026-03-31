"""读取 input_config 下的 YAML，并转换为 Volcano 仿真器 HTTP API 所需的字符串或路径。

包含：集群/负载 YAML → 仿真器侧 YAML 文本；plugins → 调度配置 YAML + 解析后的结果输出目录。
"""

from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import yaml

_FLEXNPU_CORE_KEY = "volcano.sh/flexnpu-core.percentage"
_FLEXNPU_MEM_KEY = "volcano.sh/flexnpu-memory.128mi"


def _ceil_to_step(value: float, step: float) -> float:
    """将 value 向上取整到 step 的整数倍（step<=0 时原样返回）。"""
    if step <= 0:
        return value
    return math.ceil(value / step) * step


def _round_resource_map(
    resources: Optional[Dict[str, Any]], granularity_percent: float
) -> None:
    """就地按粒度向上取整 flexnpu core/memory 请求/限制字段。"""
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
    """规范 task 结构为 template.spec，并对容器资源做 flex 粒度舍入。"""
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
    """将 input_config 风格的 cluster 文档转为仿真器约定的顶层 ``cluster:`` YAML 字符串。"""
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
    """将 workload 文档转为仅含顶层 ``jobs:`` 的仿真器 YAML。

    - 按 ``spec.npuGranularityPercent`` 将 flexnpu request/limit 向上取整到粒度步长；
    - 将 ``tasks[].spec`` 映射为 Volcano Job 兼容的 ``tasks[].template.spec``。
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
    """从文件加载 cluster YAML 并返回仿真器用 nodes YAML 字符串。"""
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError("cluster config must be a YAML mapping")
    return cluster_input_to_simulator_yaml(doc)


def load_workload_for_simulator(path: str) -> str:
    """从文件加载 workload YAML 并返回仿真器用 jobs YAML 字符串。"""
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError("workload config must be a YAML mapping")
    return workload_input_to_simulator_yaml(doc)


def resolve_out_dir_pattern(out_dir: str, now: Optional[datetime] = None) -> str:
    """将 ``outDir`` 中的 ``{date}`` 替换为 ``年-月-日-时-分-秒`` 时间戳（默认当前时间）。"""
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d-%H-%M-%S")
    return out_dir.replace("{date}", stamp)


def load_plugins_for_simulator(path: str) -> Tuple[str, str]:
    """加载 plugins YAML，返回 (调度器配置 YAML 字符串, 展开 {date} 后的结果输出目录绝对路径)。

    ``scheduler`` 块会作为 ``/step`` 的 conf；``output.outDir`` 支持 ``{date}`` 占位符。
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
