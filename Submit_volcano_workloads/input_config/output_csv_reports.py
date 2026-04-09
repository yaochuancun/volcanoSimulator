"""Build four statistics CSVs from a stepResult snapshot into a single simulation output directory (flat, no subdirs).

Files: Node_desc.csv, POD_desc.csv, npu_chip.csv, summary.csv.
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
    """Node_desc rate column: numeric 0–100 without %; whole numbers as integers (e.g. 100)."""
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
    """Resolve node name from Task, nested TransactionContext, or Pod spec."""
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


def _pod_submit_time(
    pod: Mapping[str, Any],
    job: Mapping[str, Any],
    sim_clock: str,
) -> str:
    """Sim-side job submit time: metadata.creationTimestamp; else Job timestamp; else stepResult.clock.

    Does not use ``status.startTime`` (when Running starts, see ``_pod_status_start_time``).
    """
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


def _pod_status_start_time(pod: Mapping[str, Any]) -> str:
    """``Pod.status.startTime`` (often set on Binding→Running in sim); empty if unset or not started."""
    status = pod.get("status") or {}
    st = status.get("startTime") or status.get("starttime")
    if st is None or st == "":
        for k in status:
            if str(k).lower() == "starttime":
                st = status[k]
                break
    if st is None or st == "":
        return ""
    if isinstance(st, dict):
        inner = st.get("Time") or st.get("time")
        if not inner:
            return ""
        s = str(inner).strip()
    else:
        s = str(st).strip()
    if not s or s.lower() == "null" or _is_unset_k8s_time(s):
        return ""
    return s


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


# Node_desc flex display scale: scheduler ScalarResources quantities are divided by this for CSV cells (rates unchanged).
_NODE_DESC_FLEX_SCALE = 1000.0


def _node_card_used_and_caps(
    nname: str,
    ninfo: Mapping[str, Any],
    card_used_core: Mapping[Tuple[str, str], float],
    card_used_mem: Mapping[Tuple[str, str], float],
) -> Tuple[float, float, float, float]:
    """Per-node totals: sum of per-card estimates vs annotation caps (core_used, core_cap, mem_used, mem_cap)."""
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
    """Write node-level FlexNPU rows.

    - **Allocation** (allocated / allocation_rate): scheduler ``NodeInfo`` Used/Allocatable (including granularity bookkeeping).
    - **Utilization** (utilized / utilization_rate): per-card sums from **raw** Pod spec flex requests (no extra rounding);
      same magnitude as flex values in Pod/node annotations; **do not** divide by ``_NODE_DESC_FLEX_SCALE`` (that factor applies only to scheduler ScalarResources and allocated columns).
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
        # Same magnitude as allocated (/1000 scheduler scalars); annotation caps usually match a_c; do not divide per-card estimate by 1000 again
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
    """Write Running/Binding/Pending Pod rows (flex requests, submit time, status.startTime, per-card JSON)."""
    header = [
        "node_name",
        "namespace",
        "pod_name",
        "flexnpu_core_request",
        "flexnpu_memory_request",
        "phase",
        "submit_time",
        "status.startTime",
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
        submit_t = _pod_submit_time(pod, job, sim_clock)
        start_t = _pod_status_start_time(pod)

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
                submit_t,
                start_t,
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
    """Per node and card: flexnpu core/memory utilization and allocation rates (percent, 0–100).

    core: allocation = granular rounded per-card amount / cap; utilization = raw per-card amount / cap.
    memory: not subject to ``npuGranularityPercent``; allocation and utilization match (same two values).
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
    """Summarize node count and Running/Binding vs Pending pod counts."""
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
    """Write the four CSVs under ``output_dir`` (mkdir if needed); no-op if snapshot is invalid."""
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
