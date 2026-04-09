"""Volcano 仿真 Web API：单用户单并发；接收 cluster/workload/多份 plugins，按算法×缩放系数跑仿真，
落盘 CSV，向前端返回进度与图表数据，并支持 ZIP 导出。

前置：Go 仿真器已启动（默认 http://127.0.0.1:8006）。

运行示例（在 ``Submit_volcano_workloads`` 目录下）::

    pip install -r requirements.txt
    uvicorn sim_web_api:app --host 127.0.0.1 --port 8765

浏览器打开 http://127.0.0.1:8765/
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from input_config.input_config_loader import (
    cluster_yaml_text_to_simulator_yaml,
    plugins_document_scheduler_and_outdir,
    workload_npu_granularity_percent_from_doc,
    workload_doc_to_simulator_yaml,
)
from input_config.sim_metrics import compute_chart_metrics
from input_config.workload_scale import scale_workload_document

from SimRun import reset, step

APP_ROOT = Path(__file__).resolve().parent
STATIC_DIR = APP_ROOT / "static"
STORAGE_ROOT = APP_ROOT / "var" / "sim_web_runs"

DEFAULT_SIM_URL = os.environ.get("VOLCANO_SIM_URL", "http://127.0.0.1:8006")

_ALGO_COLORS = [
    "#f97316",
    "#3b82f6",
    "#8b5cf6",
    "#10b981",
    "#ec4899",
    "#eab308",
    "#6366f1",
]

_lock = threading.Lock()
_active: Dict[str, Any] = {
    "run_id": None,
    "thread": None,
    "state": None,
}


def _safe_algo_slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", name.strip())[:64]
    return s or "algo"


def _parse_scales(s: str) -> List[float]:
    out: List[float] = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            continue
    return out if out else [1.0]


def _normalize_uploaded_text(text: str) -> str:
    """统一换行、去 BOM，并合并连续空行，避免落盘后出现「行间多出空白行」。"""
    if not text:
        return ""
    t = text.replace("\ufeff", "")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    lines = t.split("\n")
    out_lines: List[str] = []
    prev_blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        prev_blank = is_blank
        out_lines.append(line.rstrip())
    body = "\n".join(out_lines).rstrip("\n")
    return body + "\n" if body else ""


class RunState(BaseModel):
    status: str = "idle"
    message: str = ""
    progress_percent: float = 0.0
    total_steps: int = 0
    done_steps: int = 0
    current_step_label: str = ""
    chart: Dict[str, Any] = {}
    error: Optional[str] = None
    run_dir: Optional[str] = None


def _check_simulator(url: str, timeout: float = 2.0) -> Tuple[bool, str]:
    try:
        r = requests.post(f"{url.rstrip('/')}/stepResult", json={"none": ""}, timeout=timeout)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        return True, "ok"
    except requests.RequestException as e:
        return False, str(e)


def _run_simulation_worker(
    run_id: str,
    run_dir: Path,
    cluster_text: str,
    workload_text: str,
    scales: List[float],
    plugin_payloads: List[Tuple[str, str]],
    sim_url: str,
) -> None:
    state = _active["state"]
    assert state is not None

    nodes_yaml = cluster_yaml_text_to_simulator_yaml(cluster_text)
    base_wd = yaml.safe_load(workload_text)
    if not isinstance(base_wd, dict):
        state.status = "failed"
        state.error = "Invalid workload YAML"
        return

    npu_gran = workload_npu_granularity_percent_from_doc(base_wd)
    total = max(1, len(scales) * len(plugin_payloads))
    state.total_steps = total
    state.done_steps = 0
    state.progress_percent = 0.0

    algorithms_meta: List[Dict[str, Any]] = []
    points: List[Dict[str, Any]] = []

    for pi, (algo_name, plugin_text) in enumerate(plugin_payloads):
        base = _safe_algo_slug(Path(algo_name).stem)
        algo_id = f"{base}_{pi}"
        algorithms_meta.append(
            {
                "id": algo_id,
                "name": Path(algo_name).stem or base,
                "color": _ALGO_COLORS[pi % len(_ALGO_COLORS)],
                "source_file": algo_name,
            }
        )

    try:
        state.message = "Starting runs…"
        state.progress_percent = min(2.0, 100.0 / total)

        for pi, (algo_name, plugin_text) in enumerate(plugin_payloads):
            algo_id = algorithms_meta[pi]["id"]
            plug_doc = yaml.safe_load(plugin_text)
            if not isinstance(plug_doc, dict):
                raise ValueError(f"Invalid plugins YAML: {algo_name}")

            for sc in scales:
                state.current_step_label = f"{algo_id} × scale {sc}"
                # 进入本格：先反映「当前格」进度（避免长时间卡在 0%）
                state.progress_percent = round(
                    100.0 * state.done_steps / total, 2
                )
                state.message = f"{state.current_step_label} — reset…"

                scaled_doc = scale_workload_document(base_wd, sc)
                workload_yaml = workload_doc_to_simulator_yaml(scaled_doc)

                sub = f"{algo_id}_scale_{sc}".replace(".", "_")
                out_sub = run_dir / "results" / sub
                out_sub.mkdir(parents=True, exist_ok=True)

                scheduler_yaml, pods_url = plugins_document_scheduler_and_outdir(
                    plug_doc, str(out_sub)
                )
                os.makedirs(pods_url, exist_ok=True)

                reset(sim_url, nodes_yaml, workload_yaml)
                time.sleep(0.3)
                # reset 完成、step 轮询可能较久，给中间进度避免条子长时间不动
                state.progress_percent = round(
                    100.0 * (state.done_steps + 0.35) / total, 2
                )
                state.message = f"{state.current_step_label} — scheduling…"
                snap = step(sim_url, scheduler_yaml, pods_url, npu_gran)

                metrics = compute_chart_metrics(snap) if snap else None
                row: Dict[str, Any] = {
                    "algorithm_id": algo_id,
                    "algorithm_name": algorithms_meta[pi]["name"],
                    "scale": sc,
                    "result_subdir": str(out_sub.relative_to(run_dir)),
                }
                if metrics:
                    row.update(metrics)
                else:
                    row.update(
                        {
                            "allocation_rate_avg": 0.0,
                            "running_pods": 0,
                            "fragmentation_rate": 0.0,
                        }
                    )
                points.append(row)

                state.done_steps += 1
                state.progress_percent = round(100.0 * state.done_steps / total, 2)
                state.message = f"{state.current_step_label} — done"
                state.chart = {
                    "algorithms": algorithms_meta,
                    "scales": scales,
                    "points": list(points),
                }

        state.status = "succeeded"
        state.message = "Done"
        state.current_step_label = ""
        manifest = {
            "run_id": run_id,
            "simulator_url": sim_url,
            "scales": scales,
            "algorithms": algorithms_meta,
            "points": points,
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "chart_data.json").write_text(
            json.dumps(state.chart, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        state.status = "failed"
        state.error = str(e)
        state.message = f"Failed: {e}"


app = FastAPI(title="Volcano Sim Web API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)


@app.get("/api/health")
def api_health() -> Dict[str, Any]:
    sim_url = DEFAULT_SIM_URL
    ok, detail = _check_simulator(sim_url)
    return {
        "ok": ok,
        "simulator_url": sim_url,
        "simulator_reachable": ok,
        "simulator_detail": detail,
        "api": "ok",
    }


@app.get("/api/status")
def api_status() -> Dict[str, Any]:
    """返回 ``{ run_id, state }``，与前端 ``j.state.progress_percent`` 等字段一致。"""
    with _lock:
        st = _active["state"]
        rid = _active["run_id"]
    if st is None:
        return {"run_id": None, "state": RunState().model_dump()}
    return {"run_id": rid, "state": st.model_dump()}


@app.post("/api/runs")
async def api_start_run(
    cluster: UploadFile = File(...),
    workload: UploadFile = File(...),
    workload_scales: str = Form("1.0"),
    plugins: List[UploadFile] = File(...),
) -> JSONResponse:
    scales = _parse_scales(workload_scales)
    if not plugins:
        raise HTTPException(400, "At least one plugins YAML is required")

    cluster_text = _normalize_uploaded_text(
        (await cluster.read()).decode("utf-8-sig", errors="replace")
    )
    workload_text = _normalize_uploaded_text(
        (await workload.read()).decode("utf-8-sig", errors="replace")
    )
    plugin_payloads: List[Tuple[str, str]] = []
    for p in plugins:
        raw = _normalize_uploaded_text(
            (await p.read()).decode("utf-8-sig", errors="replace")
        )
        plugin_payloads.append((p.filename or "plugins.yaml", raw))

    try:
        cluster_yaml_text_to_simulator_yaml(cluster_text)
    except Exception as e:
        raise HTTPException(400, f"cluster YAML: {e}") from e
    try:
        yaml.safe_load(workload_text)
    except Exception as e:
        raise HTTPException(400, f"workload YAML: {e}") from e
    for fname, txt in plugin_payloads:
        doc = yaml.safe_load(txt)
        if not isinstance(doc, dict) or "scheduler" not in doc:
            raise HTTPException(400, f"plugins {fname}: missing scheduler block")

    with _lock:
        if _active["thread"] is not None and _active["thread"].is_alive():
            raise HTTPException(409, "A simulation is already running")
        run_id = uuid.uuid4().hex[:16]
        run_dir = STORAGE_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "input").mkdir(exist_ok=True)
        (run_dir / "results").mkdir(exist_ok=True)
        (run_dir / "input" / "cluster.yaml").write_text(cluster_text, encoding="utf-8")
        (run_dir / "input" / "workload.yaml").write_text(workload_text, encoding="utf-8")
        for fname, txt in plugin_payloads:
            safe = _safe_algo_slug(fname) + ".yaml"
            (run_dir / "input" / f"plugin_{safe}").write_text(txt, encoding="utf-8")

        state = RunState(
            status="running",
            message="Starting",
            run_dir=str(run_dir),
            chart={"algorithms": [], "scales": scales, "points": []},
        )
        _active["run_id"] = run_id
        _active["state"] = state
        th = threading.Thread(
            target=_run_simulation_worker,
            args=(
                run_id,
                run_dir,
                cluster_text,
                workload_text,
                scales,
                plugin_payloads,
                DEFAULT_SIM_URL,
            ),
            daemon=True,
        )
        _active["thread"] = th
        th.start()

    return JSONResponse({"run_id": run_id, "run_dir": str(run_dir)})


@app.get("/api/runs/latest/export")
def api_export_latest() -> StreamingResponse:
    with _lock:
        rid = _active["run_id"]
        st = _active["state"]
    if not rid or not st or not st.run_dir:
        raise HTTPException(404, "No run")
    run_dir = Path(st.run_dir)
    if st.status != "succeeded":
        raise HTTPException(409, "Run not completed yet")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        results = run_dir / "results"
        if results.is_dir():
            for fp in results.rglob("*"):
                if fp.is_file():
                    arc = fp.relative_to(run_dir)
                    zf.write(fp, arcname=str(arc).replace("\\", "/"))
        for name in ("manifest.json", "chart_data.json"):
            p = run_dir / name
            if p.is_file():
                zf.write(p, arcname=name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="volcano_sim_{rid}.zip"'},
    )


@app.get("/")
def serve_index() -> FileResponse:
    """首页与 /api 分离挂载，避免根路径 StaticFiles 与 API 路由冲突。"""
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(404, "static/index.html missing")
    return FileResponse(index)


if STATIC_DIR.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=str(STATIC_DIR)),
        name="assets",
    )
