"""根据 stepResult 快照生成四类统计 CSV，写入同一次仿真的输出目录（平铺，无子目录）。

文件：Node_desc.csv、POD_desc.csv、npu_chip.csv、summary.csv。
"""

from __future__ import annotations

import csv
import json
import os
from typing import Any, Dict, List, Mapping, Tuple

from input_config.flexnpu_util_report import (
    CORE_RES,
    MEM_RES,
    _card_caps_sorted,
    _iter_tasks_with_pod,
    _node_annotations,
    _parse_res_quantity,
    _pod_ref,
    _scalar_from_resource,
    compute_flexnpu_snapshot,
)


def _fmt_frac(used: float, total: float) -> str:
    if total < 1e-9:
        return "0/0"
    ru, rt = round(used), round(total)
    if abs(used - ru) < 1e-3 and abs(total - rt) < 1e-3:
        return f"{int(ru)}/{int(rt)}"
    return f"{used:.2f}/{total:.2f}"


def _fmt_node_desc_rate(used: float, total: float) -> str:
    """Node_desc 比率列：0–100 的数值，不带 %；整数显示为整数（如 100）。"""
    if total < 1e-9:
        return "0"
    pct = 100.0 * used / total
    if abs(pct - round(pct)) < 1e-6:
        return str(int(round(pct)))
    return f"{pct:.2f}"


def _fmt_scalar_cell(v: float) -> str:
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.4g}"


def _task_node_name(task: Mapping[str, Any], pod: Mapping[str, Any]) -> str:
    """从 Task 或内嵌 TransactionContext、Pod spec 解析节点名。"""
    n = task.get("NodeName") or task.get("nodeName")
    if n:
        return str(n)
    ctx = task.get("TransactionContext") or task.get("transactionContext")
    if isinstance(ctx, dict):
        n = ctx.get("NodeName") or ctx.get("nodeName")
        if n:
            return str(n)
    spec = pod.get("spec") or {}
    n = spec.get("nodeName") or spec.get("NodeName")
    return str(n) if n else ""


def _is_unset_k8s_time(s: str) -> bool:
    t = (s or "").strip()
    return not t or t.startswith("0001-01-01") or t == "1970-01-01T00:00:00Z"


def _pod_creation_timestamp(
    pod: Mapping[str, Any],
    job: Mapping[str, Any],
    sim_clock: str,
) -> str:
    """Pod 元数据常省略 creationTimestamp；依次尝试 status、Job 时间、仿真时钟等回退。"""
    meta = pod.get("metadata") or {}
    raw = meta.get("creationTimestamp")
    if raw is not None and raw != "":
        if isinstance(raw, dict):
            inner = raw.get("Time") or raw.get("time")
            if inner:
                s = str(inner)
                if not _is_unset_k8s_time(s):
                    return s
        else:
            s = str(raw).strip()
            if not _is_unset_k8s_time(s) and s.lower() != "null":
                return s
    for k in meta:
        if k.lower() == "creationtimestamp" and meta[k] not in (None, ""):
            s = str(meta[k]).strip()
            if not _is_unset_k8s_time(s):
                return s

    st = (pod.get("status") or {}).get("startTime")
    if st:
        if isinstance(st, dict):
            st = st.get("Time") or st.get("time")
        s = str(st).strip() if st else ""
        if not _is_unset_k8s_time(s):
            return s

    for key in ("CreationTimestamp", "creationTimestamp"):
        jts = job.get(key)
        if jts is None or jts == "":
            continue
        if isinstance(jts, dict):
            jts = jts.get("Time") or jts.get("time")
        s = str(jts).strip() if jts else ""
        if not _is_unset_k8s_time(s):
            return s

    return (sim_clock or "").strip()


def _pod_total_flex_requests(pod: Mapping[str, Any]) -> Tuple[float, float]:
    spec = pod.get("spec") or {}
    tc, tm = 0.0, 0.0
    for c in spec.get("containers") or []:
        if not isinstance(c, dict):
            continue
        res = (c.get("resources") or {}).get("requests") or {}
        tc += _parse_res_quantity(res.get(CORE_RES))
        tm += _parse_res_quantity(res.get(MEM_RES))
    return tc, tm


def _chip_json_core(chip_share: Mapping[str, Mapping[str, float]]) -> str:
    if not chip_share:
        return "{}"
    out: Dict[str, Any] = {}
    for ck, vals in chip_share.items():
        c = float(vals.get("core", 0.0))
        if abs(c - round(c)) < 1e-6:
            out[ck] = int(round(c))
        else:
            out[ck] = round(c, 4)
    return json.dumps(out, ensure_ascii=False)


# Node_desc 中 flex 资源展示量级：与调度器内部 ScalarResources 一致的量除以该系数写入 CSV（分配率不变）。
_NODE_DESC_FLEX_SCALE = 1000.0


def _node_card_used_and_caps(
    nname: str,
    ninfo: Mapping[str, Any],
    card_used_core: Mapping[Tuple[str, str], float],
    card_used_mem: Mapping[Tuple[str, str], float],
) -> Tuple[float, float, float, float]:
    """按节点汇总：逐卡估算用量之和与注解容量之和 (core_used, core_cap, mem_used, mem_cap)。"""
    ann = _node_annotations(ninfo)
    ids, cap_c, cap_m = _card_caps_sorted(ann)
    nn = str(nname)
    su_c, tot_c = 0.0, 0.0
    su_m, tot_m = 0.0, 0.0
    for cid in ids:
        ck = str(cid)
        su_c += float(card_used_core.get((nn, ck), 0.0))
        su_m += float(card_used_mem.get((nn, ck), 0.0))
        tot_c += float(cap_c.get(ck, cap_c.get(cid, 0.0)))
        tot_m += float(cap_m.get(ck, cap_m.get(cid, 0.0)))
    return su_c, tot_c, su_m, tot_m


def write_node_desc_csv(
    nodes: Mapping[str, Any],
    card_used_core_raw: Mapping[Tuple[str, str], float],
    card_used_mem_raw: Mapping[Tuple[str, str], float],
    path: str,
) -> None:
    """写节点级 FlexNPU。

    - **分配**（allocated / allocation_rate）：调度器 ``NodeInfo`` 的 Used/Allocatable（含业务粒度在调度侧的记账）。
    - **利用**（utilized / utilization_rate）：按 Pod spec **原始** flex request 分卡汇总（未再做粒度上取整）；
      量级与 Pod/节点注解中的 flex 数值一致，**不再**除以 ``_NODE_DESC_FLEX_SCALE``（该系数仅用于调度器 ScalarResources 与 allocated 列）。
    """
    rows: List[List[str]] = [
        [
            "node_name",
            "flexnpu_core_allocated/total",
            "flexnpu_memory_allocated/total",
            "flexnpu_core_allocation_rate",
            "flexnpu_memory_allocation_rate",
            "flexnpu_core_utilized/total",
            "flexnpu_memory_utilized/total",
            "flexnpu_core_utilization_rate",
            "flexnpu_memory_utilization_rate",
        ]
    ]
    for nname in sorted(nodes.keys()):
        ninfo = nodes[nname]
        if not isinstance(ninfo, dict):
            continue
        used = ninfo.get("Used") or ninfo.get("used")
        alloc = ninfo.get("Allocatable") or ninfo.get("allocatable")
        u_c = _scalar_from_resource(used, CORE_RES) / _NODE_DESC_FLEX_SCALE
        u_m = _scalar_from_resource(used, MEM_RES) / _NODE_DESC_FLEX_SCALE
        a_c = _scalar_from_resource(alloc, CORE_RES) / _NODE_DESC_FLEX_SCALE
        a_m = _scalar_from_resource(alloc, MEM_RES) / _NODE_DESC_FLEX_SCALE

        eu_c, et_c, eu_m, et_m = _node_card_used_and_caps(
            str(nname), ninfo, card_used_core_raw, card_used_mem_raw
        )
        # 与 allocated 列（/1000 后的调度器标量）同量级：注解容量之和通常已与 a_c 一致；勿再对逐卡估算除 1000
        eu_c_d, et_c_d = eu_c, et_c
        eu_m_d, et_m_d = eu_m, et_m
        if et_c < 1e-9:
            et_c_d = a_c
        if et_m < 1e-9:
            et_m_d = a_m

        rows.append(
            [
                str(nname),
                _fmt_frac(u_c, a_c),
                _fmt_frac(u_m, a_m),
                _fmt_node_desc_rate(u_c, a_c),
                _fmt_node_desc_rate(u_m, a_m),
                _fmt_frac(eu_c_d, et_c_d),
                _fmt_frac(eu_m_d, et_m_d),
                _fmt_node_desc_rate(eu_c_d, et_c_d),
                _fmt_node_desc_rate(eu_m_d, et_m_d),
            ]
        )
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)


def write_pod_desc_csv(
    jobs: Mapping[str, Any],
    pod_chip_share: Mapping[Tuple[str, str], Mapping[str, Mapping[str, float]]],
    path: str,
    sim_clock: str = "",
) -> None:
    """写 Running/Binding/Pending Pod 的描述行（含 flex 请求、创建时间、占卡 JSON）。"""
    header = [
        "node_name",
        "namespace",
        "pod_name",
        "flexnpu_core_request",
        "flexnpu_memory_request",
        "phase",
        "created_at",
        "card_used_quantity",
    ]
    rows: List[List[str]] = [header]

    for _jid, job, _tid, task in _iter_tasks_with_pod(jobs):
        if not isinstance(job, dict):
            continue
        pod = task.get("Pod") or task.get("pod") or {}
        if not isinstance(pod, dict):
            continue
        if not isinstance(task, dict):
            continue
        status = pod.get("status") or {}
        phase = (status.get("phase") or "").strip()
        if phase not in ("Running", "Binding", "Pending"):
            continue
        node = _task_node_name(task, pod)
        if phase in ("Running", "Binding") and not node:
            continue

        pref = _pod_ref(pod)
        if "/" in pref:
            ns, pname = pref.split("/", 1)
        else:
            ns, pname = "default", pref

        req_c, req_m = _pod_total_flex_requests(pod)
        created = _pod_creation_timestamp(pod, job, sim_clock)

        pk = (node, pref) if node else None
        chip_json = (
            _chip_json_core(pod_chip_share[pk])
            if pk and pk in pod_chip_share
            else "{}"
        )

        rows.append(
            [
                node or "",
                ns,
                pname,
                _fmt_scalar_cell(req_c),
                _fmt_scalar_cell(req_m),
                phase,
                created,
                chip_json,
            ]
        )

    data_rows = rows[1:]
    data_rows.sort(key=lambda r: (r[0], r[1], r[2]))
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(data_rows)


def write_npu_chip_csv(
    nodes: Mapping[str, Any],
    card_used_core_raw: Mapping[Tuple[str, str], float],
    card_used_mem_raw: Mapping[Tuple[str, str], float],
    card_used_core_gran: Mapping[Tuple[str, str], float],
    card_used_mem_gran: Mapping[Tuple[str, str], float],
    path: str,
) -> None:
    """按节点、按卡写入 flexnpu core/memory 的利用率与分配率（百分比，0–100）。

    core：分配率为粒度上取整后的分卡量/容量，利用率为原始分卡量/容量。
    memory：不参与 ``npuGranularityPercent``，分配与利用相同（两列数值一致）。
    """
    rows: List[List[str]] = [
        [
            "node_name",
            "card_id",
            "flexnpu_core_allocation_rate",
            "flexnpu_core_utilization_rate",
            "flexnpu_memory_allocation_rate",
            "flexnpu_memory_utilization_rate",
        ]
    ]
    for nname in sorted(nodes.keys()):
        ninfo = nodes[nname]
        if not isinstance(ninfo, dict):
            continue
        ann = _node_annotations(ninfo)
        ids, cap_c, cap_m = _card_caps_sorted(ann)
        if not ids:
            continue
        nn = str(nname)
        for cid in ids:
            ck = str(cid)
            ccap = cap_c.get(ck, cap_c.get(cid, 0.0))
            mcap = cap_m.get(ck, cap_m.get(cid, 0.0))
            uc_r = float(card_used_core_raw.get((nn, ck), 0.0))
            uc_g = float(card_used_core_gran.get((nn, ck), 0.0))
            um_r = float(card_used_mem_raw.get((nn, ck), 0.0))
            um_g = float(card_used_mem_gran.get((nn, ck), 0.0))
            pc_util = (100.0 * uc_r / ccap) if ccap > 1e-9 else 0.0
            pc_alloc = (100.0 * uc_g / ccap) if ccap > 1e-9 else 0.0
            pm_util = (100.0 * um_r / mcap) if mcap > 1e-9 else 0.0
            pm_alloc = (100.0 * um_g / mcap) if mcap > 1e-9 else 0.0
            rows.append(
                [
                    nn,
                    ck,
                    f"{pc_alloc:.2f}",
                    f"{pc_util:.2f}",
                    f"{pm_alloc:.2f}",
                    f"{pm_util:.2f}",
                ]
            )
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)


def write_summary_csv(
    nodes: Mapping[str, Any],
    jobs: Mapping[str, Any],
    path: str,
) -> None:
    """汇总节点数与 Running/Binding、Pending Pod 数量。"""
    node_count = len([k for k, v in nodes.items() if isinstance(v, dict)])
    run_c = 0
    pend_c = 0
    for _jid, _job, _tid, task in _iter_tasks_with_pod(jobs):
        pod = task.get("Pod") or task.get("pod") or {}
        status = pod.get("status") or {}
        phase = (status.get("phase") or "").strip()
        if phase == "Running":
            run_c += 1
        elif phase == "Binding":
            run_c += 1
        elif phase == "Pending":
            pend_c += 1
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["node_count", "pod_running_count", "pod_pending_count"])
        w.writerow([str(node_count), str(run_c), str(pend_c)])


def write_output_config_csvs(resultdata: Mapping[str, Any], output_dir: str) -> None:
    """在 ``output_dir`` 下写入上述四个 CSV（目录不存在则创建）；无有效快照时直接返回。"""
    os.makedirs(output_dir, exist_ok=True)
    snap = compute_flexnpu_snapshot(resultdata)
    if snap is None:
        return
    nodes = snap["nodes"]
    jobs = snap["jobs"]
    card_used_core_raw = snap["card_used_core_raw"]
    card_used_mem_raw = snap["card_used_mem_raw"]
    card_used_core_gran = snap["card_used_core_gran"]
    card_used_mem_gran = snap["card_used_mem_gran"]
    pod_chip_share = snap["pod_chip_share"]

    sim_clock = str(
        resultdata.get("Clock")
        or resultdata.get("clock")
        or ""
    )

    write_node_desc_csv(
        nodes,
        card_used_core_raw,
        card_used_mem_raw,
        os.path.join(output_dir, "Node_desc.csv"),
    )
    write_pod_desc_csv(
        jobs,
        pod_chip_share,
        os.path.join(output_dir, "POD_desc.csv"),
        sim_clock=sim_clock,
    )
    write_npu_chip_csv(
        nodes,
        card_used_core_raw,
        card_used_mem_raw,
        card_used_core_gran,
        card_used_mem_gran,
        os.path.join(output_dir, "npu_chip.csv"),
    )
    write_summary_csv(nodes, jobs, os.path.join(output_dir, "summary.csv"))
