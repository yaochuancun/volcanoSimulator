"""根据仿真器 ``stepResult`` JSON，汇总并打印 FlexNPU 算力/显存利用率（按节点、按卡），
以及 Pod 到 NPU 卡的估算分配（结合 flexnpu-num 与容器 request，在节点内轮询分卡）。

供控制台输出与写入 ``flexnpu_utilization.txt`` 使用；``compute_flexnpu_snapshot`` 亦被 CSV 报表复用。
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Any, DefaultDict, Dict, List, Mapping, Optional, Set, Tuple

# FlexNPU 资源在 Pod spec 与 Node 注解中使用的键名
CORE_RES = "volcano.sh/flexnpu-core.percentage"
MEM_RES = "volcano.sh/flexnpu-memory.128mi"
FLEXNPU_NUM_ANN = "volcano.sh/flexnpu-num"
CORE_LIST_ANN = "volcano.sh/flexnpu-core.percentage-list"
MEM_LIST_ANN = "volcano.sh/flexnpu-memory.128mi-list"
# 与 input_config_loader 写入一致：取整前各容器 flexnpu_core（JSON），用于利用率与分配率区分
FLEXNPU_CORE_RAW_BY_CONTAINER_ANN = (
    "volcano.sh/flexnpu-core.percentage-raw-by-container"
)

# 与 Volcano ScalarResources（MilliValue）及 Node_desc.csv 展示一致：节点汇总里 used/alloc 除以该系数。
_FLEX_NODE_AGG_DISPLAY_SCALE = 1000.0


def _parse_json_map(s: Optional[str]) -> Dict[str, float]:
    if not s or not str(s).strip():
        return {}
    try:
        obj = json.loads(str(s).strip())
    except json.JSONDecodeError:
        return {}
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, float] = {}
    for k, v in obj.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _scalar_from_resource(res: Any, name: str) -> float:
    if not res or not isinstance(res, dict):
        return 0.0
    m = res.get("ScalarResources") or res.get("scalarResources") or {}
    if not m:
        return 0.0
    if name in m:
        return float(m[name])
    for k, v in m.items():
        if str(k) == name:
            return float(v)
    return 0.0


def _parse_res_quantity(q: Any) -> float:
    if q is None:
        return 0.0
    if isinstance(q, (int, float)):
        return float(q)
    s = str(q).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _flex_core_raw_by_container_map(pod: Mapping[str, Any]) -> Dict[str, float]:
    meta = pod.get("metadata") or {}
    ann = meta.get("annotations") or {}
    raw_val = ann.get(FLEXNPU_CORE_RAW_BY_CONTAINER_ANN)
    if raw_val is None:
        return {}
    if isinstance(raw_val, dict):
        out: Dict[str, float] = {}
        for k, v in raw_val.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    return _parse_json_map(str(raw_val))


def _req_c_utilization_quantity(
    pod: Mapping[str, Any], container_name: str, spec_core_qty: Any
) -> float:
    """利用率侧 flexnpu_core：优先 Pod 注解中的取整前值，否则与 spec request 一致。"""
    m = _flex_core_raw_by_container_map(pod)
    ck = str(container_name) if container_name else "__default__"
    if ck in m:
        return m[ck]
    return _parse_res_quantity(spec_core_qty)


def _node_annotations(node_info: Mapping[str, Any]) -> Dict[str, str]:
    n = node_info.get("Node") or node_info.get("node")
    if not n or not isinstance(n, dict):
        return {}
    meta = n.get("metadata") or {}
    ann = meta.get("annotations")
    return dict(ann) if isinstance(ann, dict) else {}


def _card_caps_sorted(annotations: Mapping[str, str]) -> Tuple[List[str], Dict[str, float], Dict[str, float]]:
    core_map = _parse_json_map(annotations.get(CORE_LIST_ANN))
    mem_map = _parse_json_map(annotations.get(MEM_LIST_ANN))
    ids = sorted(core_map.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x))
    if not ids:
        ids = sorted(mem_map.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x))
    return ids, core_map, mem_map


def _flexnpu_num_map(job_info: Mapping[str, Any]) -> Dict[str, int]:
    pg = job_info.get("PodGroup") or job_info.get("podGroup")
    if not pg or not isinstance(pg, dict):
        return {}
    meta = pg.get("metadata") or {}
    ann = meta.get("annotations")
    if not isinstance(ann, dict):
        return {}
    raw = ann.get(FLEXNPU_NUM_ANN)
    if not raw:
        return {}
    m = _parse_json_map(raw)
    out: Dict[str, int] = {}
    for k, v in m.items():
        if v <= 0:
            continue
        out[k] = max(1, int(round(v)))
    return out


def _pod_ref(pod: Mapping[str, Any]) -> str:
    m = pod.get("metadata") or {}
    ns = m.get("namespace") or "default"
    name = m.get("name") or ""
    return f"{ns}/{name}"


def _iter_tasks_with_pod(jobs: Mapping[str, Any]):
    for job_id, job in (jobs or {}).items():
        if not isinstance(job, dict):
            continue
        tasks = job.get("Tasks") or job.get("tasks") or {}
        if not isinstance(tasks, dict):
            continue
        for tid, task in tasks.items():
            if isinstance(task, dict):
                yield job_id, job, tid, task


def _new_pod_assign_entry() -> Dict[str, Any]:
    return {"union": set(), "by_container": []}


def _ceil_to_granularity_step(value: float, step: float) -> float:
    """与 ``input_config_loader._ceil_to_step`` 一致：将 flex 请求按粒度向上取整。"""
    if step <= 0:
        return value
    return math.ceil(value / step) * step


def estimate_card_usage(
    jobs: Mapping[str, Any],
    node_name_to_card_ids: Dict[str, List[str]],
    granularity_percent: float = 0.0,
) -> Tuple[
    Dict[Tuple[str, str], float],
    Dict[Tuple[str, str], float],
    Dict[Tuple[str, str], float],
    Dict[Tuple[str, str], float],
    Dict[Tuple[str, str], Dict[str, Any]],
    Dict[Tuple[str, str], Dict[str, Dict[str, float]]],
]:
    """对 Running/Binding 的 Pod，按容器 request 与 flexnpu-num 将用量摊到各卡（节点内轮询）。

    返回两套逐卡累计量（单位与 Pod spec 中 flex 请求一致）：

    - **raw（利用率）**：优先使用 Pod 注解 ``FLEXNPU_CORE_RAW_BY_CONTAINER_ANN`` 中的取整前 request；
      无注解时与 spec 中 request 一致（与加载器未写注解或旧仿真器兼容）。
    - **granular（分配）**：对 **flexnpu_core** 按 spec 中 request（已为加载器取整后值）再经 ``granularity_percent`` 做与 ``input_config_loader`` 一致的上取整后分卡；
      **flexnpu_memory** 不参与粒度。core 在粒度 >0 时分配量可高于利用率侧真实需求。

    ``pod_chip_share`` 为分配侧（core 取整、mem 与 raw 一致）各 Pod 在各卡上的量，供 CSV「占卡」等展示。
    """
    g = float(granularity_percent) if granularity_percent else 0.0
    used_core_raw: Dict[Tuple[str, str], float] = defaultdict(float)
    used_mem_raw: Dict[Tuple[str, str], float] = defaultdict(float)
    used_core_gran: Dict[Tuple[str, str], float] = defaultdict(float)
    used_mem_gran: Dict[Tuple[str, str], float] = defaultdict(float)
    rr: Dict[str, int] = defaultdict(int)
    pod_assign: DefaultDict[Tuple[str, str], Dict[str, Any]] = defaultdict(_new_pod_assign_entry)
    pod_chip_share: DefaultDict[Tuple[str, str], Dict[str, Dict[str, float]]] = defaultdict(dict)

    for _jid, job, _tid, task in _iter_tasks_with_pod(jobs):
        node = task.get("NodeName") or task.get("nodeName") or ""
        if not node:
            continue
        node = str(node)
        pod = task.get("Pod") or task.get("pod") or {}
        status = pod.get("status") or {}
        phase = (status.get("phase") or "").strip()
        if phase not in ("Running", "Binding"):
            continue

        card_ids = node_name_to_card_ids.get(node) or []
        if not card_ids:
            continue

        spec = pod.get("spec") or {}
        containers = spec.get("containers") or []
        num_map = _flexnpu_num_map(job)

        for c in containers:
            if not isinstance(c, dict):
                continue
            cname = c.get("name") or ""
            res = (c.get("resources") or {}).get("requests") or {}
            req_c_spec = _parse_res_quantity(res.get(CORE_RES))
            req_c_util = _req_c_utilization_quantity(pod, cname, res.get(CORE_RES))
            req_m_raw = _parse_res_quantity(res.get(MEM_RES))
            req_c_gran = (
                _ceil_to_granularity_step(req_c_spec, g) if g > 0 else req_c_spec
            )
            # 分配粒度仅针对 flexnpu_core，memory 不做上取整
            req_m_gran = req_m_raw
            n_cards = num_map.get(cname, 1)
            if n_cards < 1:
                n_cards = 1
            if req_c_util <= 0 and req_m_raw <= 0:
                continue
            per_c_raw = req_c_util / n_cards
            per_m_raw = req_m_raw / n_cards
            per_c_gran = req_c_gran / n_cards
            per_m_gran = req_m_gran / n_cards

            assigned: List[str] = []
            start = rr[node] % len(card_ids)
            for i in range(n_cards):
                idx = (start + i) % len(card_ids)
                cid = str(card_ids[idx])
                assigned.append(cid)
                used_core_raw[(node, cid)] += per_c_raw
                used_mem_raw[(node, cid)] += per_m_raw
                used_core_gran[(node, cid)] += per_c_gran
                used_mem_gran[(node, cid)] += per_m_gran
            rr[node] = start + n_cards

            pref = _pod_ref(pod)
            ent = pod_assign[(node, pref)]
            ent["union"].update(assigned)
            ent["by_container"].append((cname, list(assigned)))

            pk = (node, pref)
            for cid in assigned:
                ck = f"{node}-{cid}"
                if ck not in pod_chip_share[pk]:
                    pod_chip_share[pk][ck] = {"core": 0.0, "mem": 0.0}
                pod_chip_share[pk][ck]["core"] += per_c_gran
                pod_chip_share[pk][ck]["mem"] += per_m_gran

    chip_out = {k: dict(v) for k, v in pod_chip_share.items()}
    return (
        dict(used_core_raw),
        dict(used_mem_raw),
        dict(used_core_gran),
        dict(used_mem_gran),
        dict(pod_assign),
        chip_out,
    )


def compute_flexnpu_snapshot(resultdata: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """从 stepResult 构建节点卡列表、每卡用量估计、Pod 绑卡等信息；无有效 Nodes 时返回 None。"""
    nodes = resultdata.get("Nodes") or resultdata.get("nodes") or {}
    jobs = resultdata.get("Jobs") or resultdata.get("jobs") or {}
    if not isinstance(nodes, dict):
        return None

    try:
        g = float(
            resultdata.get("npuGranularityPercent")
            or resultdata.get("npu_granularity_percent")
            or 0
        )
    except (TypeError, ValueError):
        g = 0.0

    node_card_ids: Dict[str, List[str]] = {}
    for nname, ninfo in nodes.items():
        if not isinstance(ninfo, dict):
            continue
        ann = _node_annotations(ninfo)
        ids, _, _ = _card_caps_sorted(ann)
        node_card_ids[str(nname)] = [str(x) for x in ids]

    (
        card_used_core_raw,
        card_used_mem_raw,
        card_used_core_gran,
        card_used_mem_gran,
        pod_assign,
        pod_chip_share,
    ) = estimate_card_usage(jobs, node_card_ids, granularity_percent=g)
    return {
        "nodes": nodes,
        "jobs": jobs,
        "node_card_ids": node_card_ids,
        "card_used_core_raw": card_used_core_raw,
        "card_used_mem_raw": card_used_mem_raw,
        "card_used_core_gran": card_used_core_gran,
        "card_used_mem_gran": card_used_mem_gran,
        "pod_assign": pod_assign,
        "pod_chip_share": pod_chip_share,
        "npuGranularityPercent": g,
    }


def format_flexnpu_report(resultdata: Mapping[str, Any]) -> str:
    """生成多段文本报告（节点汇总、Pod→卡、逐卡利用率），供写文件或打印。"""
    lines: List[str] = []
    snap = compute_flexnpu_snapshot(resultdata)
    if snap is None:
        return "[flexnpu] No Nodes in stepResult.\n"

    nodes = snap["nodes"]
    card_used_core_raw = snap["card_used_core_raw"]
    card_used_mem_raw = snap["card_used_mem_raw"]
    card_used_core_gran = snap["card_used_core_gran"]
    card_used_mem_gran = snap["card_used_mem_gran"]
    pod_assign = snap["pod_assign"]

    lines.append(
        "=== FlexNPU node aggregate (scheduler NodeInfo: allocated / capacity = allocation rate) ==="
    )
    for nname in sorted(nodes.keys()):
        ninfo = nodes[nname]
        if not isinstance(ninfo, dict):
            continue
        used = ninfo.get("Used") or ninfo.get("used")
        alloc = ninfo.get("Allocatable") or ninfo.get("allocatable")
        u_c = _scalar_from_resource(used, CORE_RES) / _FLEX_NODE_AGG_DISPLAY_SCALE
        u_m = _scalar_from_resource(used, MEM_RES) / _FLEX_NODE_AGG_DISPLAY_SCALE
        a_c = _scalar_from_resource(alloc, CORE_RES) / _FLEX_NODE_AGG_DISPLAY_SCALE
        a_m = _scalar_from_resource(alloc, MEM_RES) / _FLEX_NODE_AGG_DISPLAY_SCALE
        pct_c = (100.0 * u_c / a_c) if a_c > 1e-9 else 0.0
        pct_m = (100.0 * u_m / a_m) if a_m > 1e-9 else 0.0
        lines.append(
            f"  Node {nname}: flexnpu-core  used={u_c:.3f} cap={a_c:.3f} alloc%={pct_c:.2f}% | "
            f"flexnpu-memory(128Mi) used={u_m:.3f} cap={a_m:.3f} alloc%={pct_m:.2f}%"
        )

    lines.append(
        "=== Pod -> NPU cards (estimated: flexnpu-num + container requests, round-robin on node) ==="
    )
    if not pod_assign:
        lines.append("  (no Running/Binding pods with flexnpu requests on nodes that define card lists)")
    else:

        def _card_sort_key(c: str) -> Tuple[int, str]:
            return (int(c), c) if str(c).isdigit() else (10**9, str(c))

        for (node, pref) in sorted(pod_assign.keys(), key=lambda x: (x[0], x[1])):
            ent = pod_assign[(node, pref)]
            union: Set[str] = ent.get("union") or set()
            if not union:
                continue
            union_sorted = sorted(union, key=_card_sort_key)
            lines.append(f"  Node {node}  Pod {pref}: cards {','.join(union_sorted)}")
            for cname, cids in ent.get("by_container") or []:
                lines.append(f"    container {cname}: cards {','.join(cids)}")

    lines.append(
        "=== Per NPU card (cap from annotations; util%=raw request / cap, alloc%=granular-rounded / cap) ==="
    )
    for nname in sorted(nodes.keys()):
        ninfo = nodes[nname]
        if not isinstance(ninfo, dict):
            continue
        ann = _node_annotations(ninfo)
        ids, cap_c, cap_m = _card_caps_sorted(ann)
        if not ids:
            lines.append(f"  Node {nname}: (no {CORE_LIST_ANN} / {MEM_LIST_ANN}, skip cards)")
            continue
        lines.append(f"  Node {nname}:")
        for cid in ids:
            ck = str(cid)
            ccap = cap_c.get(ck, cap_c.get(cid, 0.0))
            mcap = cap_m.get(ck, cap_m.get(cid, 0.0))
            uc_r = float(card_used_core_raw.get((str(nname), ck), 0.0))
            uc_g = float(card_used_core_gran.get((str(nname), ck), 0.0))
            um_r = float(card_used_mem_raw.get((str(nname), ck), 0.0))
            um_g = float(card_used_mem_gran.get((str(nname), ck), 0.0))
            pc_util = (100.0 * uc_r / ccap) if ccap > 1e-9 else 0.0
            pc_alloc = (100.0 * uc_g / ccap) if ccap > 1e-9 else 0.0
            pm_util = (100.0 * um_r / mcap) if mcap > 1e-9 else 0.0
            pm_alloc = (100.0 * um_g / mcap) if mcap > 1e-9 else 0.0
            lines.append(
                f"    card {ck}: core cap={ccap:.3f} raw~={uc_r:.3f} util~={pc_util:.2f}% "
                f"alloc~={pc_alloc:.2f}% | mem cap={mcap:.3f} raw~={um_r:.3f} util~={pm_util:.2f}% "
                f"alloc~={pm_alloc:.2f}%"
            )

    return "\n".join(lines) + "\n"


def print_flexnpu_utilization(resultdata: Mapping[str, Any]) -> str:
    """格式化报告并打印到标准输出，同时返回字符串供调用方写入文件。"""
    text = format_flexnpu_report(resultdata)
    print(text, end="")
    return text
