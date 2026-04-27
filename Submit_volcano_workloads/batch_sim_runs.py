#!/usr/bin/env python3
"""Run the Go Volcano simulator repeatedly with fixed cluster / workload / scheduler (plugins) configs.

Each iteration writes the same metric CSVs as ``SimRun.step`` (``Node_desc.csv``, ``POD_desc.csv``,
``npu_chip.csv``, ``summary.csv``, ``tasksSUM.csv``, etc.) into a dedicated subdirectory
(``run_0001/``, ``run_0002/``, …).

By default all runs go under ``Submit_volcano_workloads/batch_results/<YYYY-MM-DD-HH-MM-SS>/``.
Use ``--output-dir`` only to override that root path.

Prerequisites:
  - Go simulator listening on ``--sim-url`` (default ``http://127.0.0.1:8006``).
  - From this directory (``Submit_volcano_workloads``): ``pip install -r requirements.txt``

Example::

    cd Submit_volcano_workloads
    python batch_sim_runs.py \\
        --cluster input_config/cluster/cluster.yaml \\
        --workload input_config/workload/workload.yaml \\
        --plugins input_config/plugins/plugins.yaml \\
        --runs 100
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from input_config.input_config_loader import (  # noqa: E402
    load_cluster_for_simulator,
    load_workload_for_simulator,
    workload_npu_granularity_percent_from_file,
    plugins_document_scheduler_and_outdir,
)
from SimRun import reset, step  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Repeated simulator runs: same node + workload + plugins YAML; CSVs per run under batch_results/<datetime>/ by default.",
    )
    p.add_argument(
        "--cluster",
        required=True,
        help="Path to cluster YAML (node config for /reset).",
    )
    p.add_argument(
        "--workload",
        required=True,
        help="Path to workload YAML (JobList for /reset).",
    )
    p.add_argument(
        "--plugins",
        required=True,
        help="Path to plugins YAML (scheduler block for /step; output.outDir in file is ignored for per-run dirs).",
    )
    p.add_argument(
        "--runs",
        type=int,
        default=100,
        help="Number of full reset+step cycles (default: 100).",
    )
    p.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help=(
            "Root directory for this batch (each run uses run_NNNN/ inside). "
            "Default: batch_results/<YYYY-MM-DD-HH-MM-SS>/ under this script's directory."
        ),
    )
    p.add_argument(
        "--sim-url",
        default=os.environ.get("VOLCANO_SIM_URL", "http://127.0.0.1:8006"),
        help="Simulator base URL (default: env VOLCANO_SIM_URL or http://127.0.0.1:8006).",
    )
    p.add_argument(
        "--sleep-after-reset",
        type=float,
        default=1.0,
        help="Seconds to sleep after /reset before /step (default: 1.0).",
    )
    p.add_argument(
        "--sleep-between-runs",
        type=float,
        default=0.5,
        help="Seconds to sleep after a finished step before the next reset (default: 0.5).",
    )
    p.add_argument(
        "--fail-fast",
        action="store_true",
        help="Exit on first run where step() does not return a snapshot dict.",
    )
    return p.parse_args()


def _load_plugins_doc(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError(f"plugins YAML must be a mapping: {path}")
    return doc


def main() -> int:
    args = _parse_args()
    runs = int(args.runs)
    if runs < 1:
        print("--runs must be >= 1", file=sys.stderr)
        return 2

    cluster_path = Path(args.cluster).resolve()
    workload_path = Path(args.workload).resolve()
    plugins_path = Path(args.plugins).resolve()

    if args.output_dir:
        out_root = Path(args.output_dir).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        out_root = (_SCRIPT_DIR / "batch_results" / stamp).resolve()

    for p, label in (
        (cluster_path, "cluster"),
        (workload_path, "workload"),
        (plugins_path, "plugins"),
    ):
        if not p.is_file():
            print(f"Missing {label} file: {p}", file=sys.stderr)
            return 2

    out_root.mkdir(parents=True, exist_ok=True)
    plugins_doc = _load_plugins_doc(plugins_path)

    nodes_yaml = load_cluster_for_simulator(str(cluster_path))
    workload_yaml = load_workload_for_simulator(str(workload_path))
    npu_granularity = workload_npu_granularity_percent_from_file(str(workload_path))

    manifest: Dict[str, Any] = {
        "sim_url": args.sim_url.rstrip("/"),
        "cluster": str(cluster_path),
        "workload": str(workload_path),
        "plugins": str(plugins_path),
        "runs_requested": runs,
        "runs_ok": 0,
        "runs_failed": 0,
        "runs": [],
        "output_root": str(out_root),
        "output_dir_from_cli": bool(args.output_dir),
    }

    print(f"Output root: {out_root}")
    print(f"Runs: {runs} | Simulator: {args.sim_url}")

    for i in range(runs):
        run_id = i + 1
        run_sub = out_root / f"run_{run_id:04d}"
        run_sub.mkdir(parents=True, exist_ok=True)

        scheduler_conf_yaml, pods_dir = plugins_document_scheduler_and_outdir(
            plugins_doc,
            str(run_sub.resolve()),
        )
        os.makedirs(pods_dir, exist_ok=True)

        entry: Dict[str, Any] = {
            "index": run_id,
            "subdir": str(run_sub.relative_to(out_root)),
            "absolute_dir": str(run_sub),
            "status": "started",
        }
        manifest["runs"].append(entry)

        print(f"\n--- Run {run_id}/{runs} -> {run_sub.name} ---")
        try:
            reset(args.sim_url.rstrip("/"), nodes_yaml, workload_yaml)
            time.sleep(float(args.sleep_after_reset))
            snap = step(
                args.sim_url.rstrip("/"),
                scheduler_conf_yaml,
                pods_dir,
                npu_granularity,
            )
        except Exception as e:  # noqa: BLE001
            entry["status"] = "error"
            entry["error"] = str(e)
            manifest["runs_failed"] += 1
            err_file = run_sub / "batch_run_error.txt"
            err_file.write_text(str(e), encoding="utf-8")
            print(f"ERROR run {run_id}: {e}", file=sys.stderr)
            if args.fail_fast:
                manifest_path = out_root / "batch_manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                return 1
            time.sleep(float(args.sleep_between_runs))
            continue

        if not isinstance(snap, dict):
            entry["status"] = "no_snapshot"
            manifest["runs_failed"] += 1
            msg = "step() did not return a snapshot dict (simulator may be stuck or /stepResult never completed)."
            entry["error"] = msg
            (run_sub / "batch_run_error.txt").write_text(msg + "\n", encoding="utf-8")
            print(f"WARN run {run_id}: {msg}", file=sys.stderr)
            if args.fail_fast:
                manifest_path = out_root / "batch_manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                return 1
        else:
            entry["status"] = "ok"
            entry["clock"] = snap.get("Clock") or snap.get("clock")
            manifest["runs_ok"] += 1

        time.sleep(float(args.sleep_between_runs))

    manifest_path = out_root / "batch_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nDone. Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
