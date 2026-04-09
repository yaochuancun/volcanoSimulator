# volcanoSimulator

This repository contains a **Volcano scheduling simulator (Go)** and a **Python workload client**: describe clusters, jobs, and scheduler plugins in YAML, drive the simulation over HTTP, and write task summaries, FlexNPU utilization, and standard CSV reports to disk. It also ships a **browser-based single-page Web UI** for batch runs across **multiple scheduler configs × multiple workload scale factors** and ZIP export—without using the command line for uploads.

License and third-party notes: root [**LICENSE**](LICENSE), [**LICENSE-NOTES.md**](LICENSE-NOTES.md). Input layout details: [**Submit_volcano_workloads/input_config/README.md**](Submit_volcano_workloads/input_config/README.md). **Architecture (English):** [**docs/architecture.md**](docs/architecture.md). **Per-package dependency notes:** [**Submit_volcano_workloads/requirements.txt**](Submit_volcano_workloads/requirements.txt).

---

## 1. Main features

### 1.1 Configuration-driven simulation (`Submit_volcano_workloads/input_config/`)

- **Cluster:** `cluster/*.yaml` — nodes, FlexNPU annotations (e.g. `flexnpu-core.percentage-list`, `flexnpu-memory.128mi-list`, `topologies`), and schedulable resources.  
- **Workload:** `workload/*.yaml` — Volcano Job list; supports `npuGranularityPercent` (**flexnpu_core** request/limit rounded up to the step; memory not rounded), `volcano.sh/flexnpu-num`, etc.  
- **Scheduling:** `plugins/*.yaml` — the `scheduler` block (actions, tiers, plugin args) is sent as scheduler config; `output.outDir` with `{date}` expands to a timestamp for the **CLI SimRun** result root.

### 1.2 Talking to the simulator (Go HTTP)

- **`/reset`** — send converted node and workload YAML; initialize cluster and job queue.  
- **`/step`** — send scheduler config; advance the scheduling round.  
- **`/stepResult`** — fetch current `Jobs`, `Nodes`, pods, simulation clock, etc.; may return placeholder **`"0"`** or JSON number **`0`** while the round is not ready (Python uses `str(result) == '0'`).

### 1.3 CLI artifacts (`SimRun.py` + `plugins` `outDir`)

Written flat under the directory resolved from **`plugins.yaml`** (typical path like `Submit_volcano_workloads/result/<timestamp>/`):

| File | Description |
| --- | --- |
| `tasksSUM.csv` | Pod name, Job, Phase, NodeName |
| `pod_phase_count.txt` | Pending / Running counts, etc. |
| `flexnpu_utilization.txt` | Node-level FlexNPU, per-card estimate, pod→card notes |
| `Node_desc.csv` / `POD_desc.csv` / `npu_chip.csv` / `summary.csv` | Stats and per-card reports |

### 1.4 Python modules (`Submit_volcano_workloads/input_config/`)

| Module | Role |
| --- | --- |
| `input_config_loader` | Disk or in-memory YAML → simulator `cluster` / `jobs` / scheduler conf; Web uploads use **`cluster_yaml_text_to_simulator_yaml`**, **`workload_doc_to_simulator_yaml`**, **`plugins_document_scheduler_and_outdir`**, etc. |
| `flexnpu_util_report` | Parse FlexNPU from `stepResult`; **`compute_flexnpu_snapshot`** |
| `output_csv_reports` | **`write_output_config_csvs`** — four CSV types |
| `workload_scale` | **`scale_workload_document`**: `replicas → max(1, ceil(replicas × factor))` for the Web matrix |
| `sim_metrics` | **`compute_chart_metrics`**: mean node allocation rate, first-snapshot **Running** pod count, fragmentation rate, etc., for Web charts |

### 1.5 Web UI (`sim_web_api.py` + `static/`)

- **Entry:** from `Submit_volcano_workloads`, run `uvicorn sim_web_api:app`; open the app root (e.g. `http://127.0.0.1:8765/`).  
- **Static assets:** `GET /` serves **`static/index.html`**; CSS/JS under **`/assets/*`** (same folder), registered separately from **`/api/*`** to avoid route clashes.  
- **Uploads:** **cluster.yaml**, **workload.yaml**, **multiple plugins.yaml** (one scheduler per file; each must include a **`scheduler`** block); **Workload scale** is a comma-separated list of numbers.  
- **Scaling rule:** for each scale factor, workload replicas become **`ceil(replicas × factor)`**, at least 1.  
- **Execution model:** **single user, one concurrent run** — if the previous worker thread is still alive, **`POST /api/runs`** returns **409**.  
- **Backend chain:** same as CLI: for each **(plugins_i, scale_j)** run **`reset` → `step`**; results under **`var/sim_web_runs/<run_id>/results/<algo>_scale_<s>/`** (overrides per-file `output.outDir` semantics for that run).  
- **Progress:** **`GET /api/status`** returns **`{ run_id, state }`** with **`state.progress_percent`**, **`state.message`**, **`state.chart`** for the bar and charts; **`GET /api/health`** checks whether the Go simulator is reachable (**`VOLCANO_SIM_URL`**, default `http://127.0.0.1:8006`).  
- **Frontend:** **Start Simulation** disabled when the simulator is **not OK**; health polling; custom English file buttons (avoids OS-locale strings like “No file chosen”).  
- **Export:** **`GET /api/runs/latest/export`** ZIPs the last **successful** run’s **`results/**`** plus **`manifest.json`** / **`chart_data.json`** (**409** if not finished).

### 1.6 Other directories

- **`Submit_volcano_workloads/common/`** — `JsonHttpClient`, etc.  
- **`Submit_volcano_workloads/figures/`** — legacy plotting (off the main path).  
- **`Volcano_simulator/`** and **`Submit_volcano_workloads/`** are siblings; build and run separately.

---

## 2. Architecture snapshot

```
┌─────────────────────────────────────────────────────────────┐
│  Submit_volcano_workloads/ (Python)                         │
│  SimRun.py ──► JsonHttpClient ──► HTTP JSON                  │
│  sim_web_api.py ──► same reset/step + FastAPI / static UI   │
│       │              ▲                                       │
│       ▼              │                                       │
│  input_config/     │     stepResult (Jobs, Nodes, …)       │
│  · loader / flexnpu │                                       │
│  · output_csv      │                                       │
│  · sim_metrics      │                                       │
│  · workload_scale   │                                       │
└──────────────────────┼───────────────────────────────────────┘
                       │  :8006 (default)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Volcano_simulator/cmd/sim (Go)                             │
│  /reset  /step  /stepResult                                 │
└─────────────────────────────────────────────────────────────┘
```

Layers, **Web API table**, **`stepResult` fields**, and where to edit: [**docs/architecture.md**](docs/architecture.md).

---

## 3. How to run

### 3.1 Start the simulator (Go)

Build and run under the simulator tree (default port **8006**, matches Python `sim_base_url` / **`VOLCANO_SIM_URL`**):

```bash
cd Volcano_simulator/cmd/sim
go build -o sim .
./sim
```

(On Windows, use `sim.exe` or equivalent.)

### 3.2 Python dependencies (use `requirements.txt`)

**Web and CLI share** one list: [**Submit_volcano_workloads/requirements.txt**](Submit_volcano_workloads/requirements.txt) (comments explain each package).

```bash
cd Submit_volcano_workloads
pip install -r requirements.txt
```

For **`SimRun.py` only**, you still need **`requests`**, **`PyYAML`**, **`prettytable`**, **`munch`**; the Web stack adds **FastAPI**, **uvicorn**, **python-multipart**, **pydantic v2**.

**Python 3.8+ recommended; 3.10+ preferred** for Pydantic v2 / FastAPI tooling.

### 3.3 Run one simulation from the CLI

With the simulator listening on **`http://localhost:8006`** (or your URL):

```bash
cd Submit_volcano_workloads
python SimRun.py
```

Edit **`cluster_path`**, **`workload_path`**, **`plugins_path`** in **`SimRun.py`** under **`if __name__ == '__main__':`**.

### 3.4 Web UI (upload + matrix + export)

1. Keep the Go simulator running.  
2. Optional env: **`VOLCANO_SIM_URL`**, e.g. `http://127.0.0.1:8006`.  
3. Start the ASGI server:

```bash
cd Submit_volcano_workloads
uvicorn sim_web_api:app --host 127.0.0.1 --port 8765
```

4. Open **`http://127.0.0.1:8765/`** in a browser.

**Local storage layout (usually `.gitignore`d):**

```
Submit_volcano_workloads/var/sim_web_runs/<run_id>/
  input/              # Normalized cluster.yaml, workload.yaml, plugin_*.yaml
  results/            # One subdir per algorithm × scale; same CSV/text as CLI
  manifest.json       # Run metadata and metric points
  chart_data.json     # Aggregated structure for replaying charts in the UI
```

### 3.5 Change input and output paths (CLI)

- **Inputs:** **`cluster_path`** / **`workload_path`** / **`plugins_path`** in **`SimRun.py`**.  
- **Output:** **`output.outDir`** in **`plugins.yaml`** (supports **`{date}`**).

### 3.6 Troubleshooting

| Symptom | Check |
| --- | --- |
| Web shows **Simulator: not OK** | Is Go listening on **`VOLCANO_SIM_URL`**? Firewall? Can **`requests`** POST **`/stepResult`** from this machine? |
| **Start Simulation** grayed out | Health check failing, or a previous run still in progress (**409**). |
| Progress bar stuck | **`/api/status`** should return **`{ "state": { "progress_percent": ... } }`**; the frontend also accepts a legacy flat JSON shape. |
| Dependency conflicts | Use **`python -m venv .venv`** then **`pip install -r requirements.txt`**. |

### 3.7 Notes and hygiene

- Input subfolders: **`Submit_volcano_workloads/input_config/README.md`**.  
- Do not commit large result trees: see root **`.gitignore`** (**`Submit_volcano_workloads/result/`**, **`var/`**, etc.).

---

If scheduling behavior, ports, or HTTP contracts change, treat **`Volcano_simulator/cmd/sim/main.go`**, **`Submit_volcano_workloads/SimRun.py`**, and **`Submit_volcano_workloads/sim_web_api.py`** as the source of truth.
