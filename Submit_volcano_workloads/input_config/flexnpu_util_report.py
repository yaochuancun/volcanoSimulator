"""根据仿真器 ``stepResult`` JSON，汇总并打印 FlexNPU 算力/显存利用率（按节点、按卡），
以及 Pod 到 NPU 卡的估算分配（结合 flexnpu-num 与容器 request，在节点内轮询分卡）。

供控制台输出与写入 ``flexnpu_utilization.txt`` 使用；``compute_flexnpu_snapshot`` 亦被 CSV 报表复用。
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, DefaultDict, Dict, List, Mapping, Optional, Set, Tuple

# FlexNPU 资源在 Pod spec 与 Node 注解中使用的键名
CORE_RES = "volcano.sh/flexnpu-core.percentage"
MEM_RES = "volcano.sh/flexnpu-memory.128mi"
FLEXNPU_NUM_ANN = "volcano.sh/flexnpu-num"
CORE_LIST_ANN = "volcano.sh/flexnpu-core.percentage-list"
MEM_LIST_ANN = "volcano.sh/flexnpu-memory.128mi-list"


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


def estimate_card_usage(
    jobs: Mapping[str, Any], node_name_to_card_ids: Dict[str, List[str]]
) -> Tuple[
    Dict[Tuple[str, str], float],
    Dict[Tuple[str, str], float],
    Dict[Tuple[str, str], Dict[str, Any]],
    Dict[Tuple[str, str], Dict[str, Dict[str, float]]],
]:
    """对 Running/Binding 的 Pod，按容器 request 与 flexnpu-num 将用量摊到各卡（节点内轮询）。"""
    used_core: Dict[Tuple[str, str], float] = defaultdict(float)
    used_mem: Dict[Tuple[str, str], float] = defaultdict(float)
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
            req_c = _parse_res_quantity(res.get(CORE_RES))
            req_m = _parse_res_quantity(res.get(MEM_RES))
            n_cards = num_map.get(cname, 1)
            if n_cards < 1:
                n_cards = 1
            if req_c <= 0 and req_m <= 0:
                continue
            per_c = req_c / n_cards
            per_m = req_m / n_cards

            assigned: List[str] = []
            start = rr[node] % len(card_ids)
            for i in range(n_cards):
                idx = (start + i) % len(card_ids)
                cid = str(card_ids[idx])
                assigned.append(cid)
                used_core[(node, cid)] += per_c
                used_mem[(node, cid)] += per_m
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
                pod_chip_share[pk][ck]["core"] += per_c
                pod_chip_share[pk][ck]["mem"] += per_m

    return used_core, used_mem, dict(pod_assign), {k: dict(v) for k, v in pod_chip_share.items()}


def compute_flexnpu_snapshot(resultdata: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """从 stepResult 构建节点卡列表、每卡用量估计、Pod 绑卡等信息；无有效 Nodes 时返回 None。"""
    nodes = resultdata.get("Nodes") or resultdata.get("nodes") or {}
    jobs = resultdata.get("Jobs") or resultdata.get("jobs") or {}
    if not isinstance(nodes, dict):
        return None

    node_card_ids: Dict[str, List[str]] = {}
    for nname, ninfo in nodes.items():
        if not isinstance(ninfo, dict):
            continue
        ann = _node_annotations(ninfo)
        ids, _, _ = _card_caps_sorted(ann)
        node_card_ids[str(nname)] = [str(x) for x in ids]

    card_used_core, card_used_mem, pod_assign, pod_chip_share = estimate_card_usage(
        jobs, node_card_ids
    )
    return {
        "nodes": nodes,
        "jobs": jobs,
        "node_card_ids": node_card_ids,
        "card_used_core": card_used_core,
        "card_used_mem": card_used_mem,
        "pod_assign": pod_assign,
        "pod_chip_share": pod_chip_share,
    }


def format_flexnpu_report(resultdata: Mapping[str, Any]) -> str:
    """生成多段文本报告（节点汇总、Pod→卡、逐卡利用率），供写文件或打印。"""
    lines: List[str] = []
    snap = compute_flexnpu_snapshot(resultdata)
    if snap is None:
        return "[flexnpu] No Nodes in stepResult.\n"

    nodes = snap["nodes"]
    card_used_core = snap["card_used_core"]
    card_used_mem = snap["card_used_mem"]
    pod_assign = snap["pod_assign"]

    lines.append("=== FlexNPU utilization (node aggregate from scheduler NodeInfo) ===")
    for nname in sorted(nodes.keys()):
        ninfo = nodes[nname]
        if not isinstance(ninfo, dict):
            continue
        used = ninfo.get("Used") or ninfo.get("used")
        alloc = ninfo.get("Allocatable") or ninfo.get("allocatable")
        u_c = _scalar_from_resource(used, CORE_RES)
        u_m = _scalar_from_resource(used, MEM_RES)
        a_c = _scalar_from_resource(alloc, CORE_RES)
        a_m = _scalar_from_resource(alloc, MEM_RES)
        pct_c = (100.0 * u_c / a_c) if a_c > 1e-9 else 0.0
        pct_m = (100.0 * u_m / a_m) if a_m > 1e-9 else 0.0
        lines.append(
            f"  Node {nname}: flexnpu-core  used={u_c:.3f} alloc={a_c:.3f} util={pct_c:.2f}% | "
            f"flexnpu-memory(128Mi) used={u_m:.3f} alloc={a_m:.3f} util={pct_m:.2f}%"
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

    lines.append("=== Per NPU card (capacity from node annotations; used estimated from pod requests + flexnpu-num) ===")
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
            uc = card_used_core.get((str(nname), ck), 0.0)
            um = card_used_mem.get((str(nname), ck), 0.0)
            pc = (100.0 * uc / ccap) if ccap > 1e-9 else 0.0
            pm = (100.0 * um / mcap) if mcap > 1e-9 else 0.0
            lines.append(
                f"    card {ck}: core cap={ccap:.3f} used~={uc:.3f} util~={pc:.2f}% | "
                f"mem cap={mcap:.3f} used~={um:.3f} util~={pm:.2f}%"
            )

    return "\n".join(lines) + "\n"


def print_flexnpu_utilization(resultdata: Mapping[str, Any]) -> str:
    """格式化报告并打印到标准输出，同时返回字符串供调用方写入文件。"""
    text = format_flexnpu_report(resultdata)
    print(text, end="")
    return text
