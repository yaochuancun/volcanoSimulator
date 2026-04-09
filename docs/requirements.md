# Volcano Scheduling Simulator — Requirements

**Version note:** This document describes capabilities **already implemented** in this repository, as the baseline for product/delivery requirements and observable behavior. Implementation details are authoritative in the source code.

---

## 1. Goals and scope

### 1.1 Objectives

Use **YAML** locally to describe the cluster, Volcano jobs, and scheduler plugins; drive an **HTTP**-based, **discrete-event-style** simulation with an embedded Volcano framework; and produce **task listings, FlexNPU utilization analysis, and structured CSV reports** for scheduler validation and resource visibility.

### 1.2 Scope boundaries

| In scope | Out of scope |
| --- | --- |
| Go simulator process (nodes, Jobs, Tasks/Pods, scheduling Session, Pod phases, simulation clock) | Real Kubernetes / Volcano cluster integration |
| Python client: config conversion, HTTP calls, writing results and reports | Changing the Volcano scheduler algorithm core (client does not rewrite scheduling logic) |
| FlexNPU annotation parsing, granularity rounding, utilization/allocation estimates and reports | Production-grade HA, multi-tenant isolation |

---

## 2. System architecture

- **Volcano_simulator** (Go, `cmd/sim`): HTTP listener (default **8006**), maintains cluster and job state, exposes `reset` / `step` / `stepResult` (and `stepResultAnyway`).
- **Submit_volcano_workloads** (Python): `SimRun.py` is the entry point; `input_config/` loads and converts YAML; `flexnpu_util_report` and `output_csv_reports` produce statistics and CSVs.

**Data flow:** Read `cluster` / `workload` / `plugins` YAML → `reset` → `step` with scheduler config → poll `stepResult` until a valid snapshot → write `tasksSUM.csv`, `pod_phase_count.txt`, `flexnpu_utilization.txt`, and the four CSV types (under **`phase1/`** when two-phase output is enabled; see code and README).

---

## 3. Configuration inputs

### 3.1 Cluster (`input_config/cluster/*.yaml`)

- Node list: names, labels, annotations, capacity/spec, etc.
- **FlexNPU**-related node annotations, e.g.:
  - `volcano.sh/flexnpu-core.percentage-list`, `volcano.sh/flexnpu-memory.128mi-list` (JSON maps of capacity per card ID);
  - Extensions agreed with the simulator (e.g. `topologies`).

### 3.2 Workload (`input_config/workload/*.yaml`)

- Top-level **JobList**-style document with multiple Volcano `batch.volcano.sh/v1alpha1` **Jobs**.
- **`spec.npuGranularityPercent` (optional)**  
  - When **> 0**: **only** container **`volcano.sh/flexnpu-core.percentage` (flexnpu_core)** **requests / limits** are **rounded up** to the granularity step; **`volcano.sh/flexnpu-memory.128mi` is not** subject to granularity.  
  - While rounding: write to **`tasks[].template.metadata.annotations`** the key  
    `volcano.sh/flexnpu-core.percentage-raw-by-container` (JSON: container name → pre-round value) so reports can separate **true demand** from **rounded allocation**.
- Task templates may use `tasks[].spec`; the loader normalizes to Volcano-compatible **`tasks[].template.spec`** (adds a `template` wrapper when needed).
- **`runningTime`** (optional) on a task: after resources are allocated and the Pod is **Running**, the simulator releases resources after this duration (simulation seconds); see `simPhase2Ready` and **phase2** outputs in the client.
- Job/PodGroup may set **`volcano.sh/flexnpu-num`** (JSON: container name → number of cards per container) for per-card estimation.

### 3.3 Plugins and output directory (`input_config/plugins/*.yaml`)

- **`scheduler`:** Scheduler configuration (actions, tiers, plugin arguments), sent as the YAML string in the `/step` request body.
- **`output.outDir`:** Result root directory; supports **`{date}`**, expanded to `YYYY-MM-DD-HH-MM-SS`; relative paths are resolved from the client’s current working directory.

---

## 4. Simulator (Go) requirements

### 4.1 Lifecycle and HTTP API

- **`/reset`:** Accepts node YAML and workload YAML, initializes the simulation; may refuse reset if jobs are still active (defined error code/flag).
- **`/step`:** Accepts scheduler configuration and advances the current scheduling round.
- **`/stepResult`:** May return a placeholder such as **`"0"`** when the round is not stable or jobs are incomplete; otherwise returns JSON including **Jobs, Nodes, simulation clock**, etc. (see `simulator.Info`), including **`simPhase2Ready`** when applicable.
- **`/stepResultAnyway`:** Returns a variant snapshot (possibly fewer fields than full `stepResult`).

### 4.2 Job / Pod modeling

- Building a Job from YAML creates one **Pod** per Task; **`Task.Template.Annotations` are merged into the Pod** (then Volcano group annotations are applied) so FlexNPU raw-core annotations from the workload appear on Pods in **stepResult**.
- **Pod `CreationTimestamp`:** When the Job is **admitted to the cluster** in simulation, **all** Tasks of that Job receive the **same** simulation clock value (**not** wall clock, **not** per-Pod staggered).
- **Pod `status.startTime`:** Set on **Binding → Running** (and similar) transitions; simulation time when the container is considered started.
- Pod phase semantics: unscheduled and **Binding** may appear as **Pending** externally; after the Task is **Running**, the Pod is **Running** (see `syncSimulationPodPhases` in `main.go`).

### 4.3 Nodes and scheduling

- Embedded Volcano stack: Session, plugin registration, `OpenSessionV2`, configured action chain execution.
- Nodes may configure container startup delay, multi-container spacing, etc. (`V2Node` and related structs).

---

## 5. Python client requirements

### 5.1 Entry point (`SimRun.py`)

- Load **cluster / workload / plugins** from file paths.
- Call **`reset`**, **`step`**, poll **`/stepResult`** until a dict-like snapshot is returned.
- Inject workload **`npuGranularityPercent`** into the snapshot so per-card estimates match the loader’s rounding rules.
- **Phase 1 output:** Console summary, **`phase1/tasksSUM.csv`**, **`phase1/pod_phase_count.txt`**, **`phase1/flexnpu_utilization.txt`**, CSVs from **`write_output_config_csvs`**.
- **Phase 2 (when workload defines `runningTime`):** Poll until **`simPhase2Ready`**, then write **`phase2/pod_completion.csv`** and **`phase2/job_completion.csv`**.

### 5.2 Config loading (`input_config_loader`)

- `load_cluster_for_simulator` / `load_workload_for_simulator` / `load_plugins_for_simulator`: read YAML and produce strings/paths expected by the simulator.
- `workload_input_to_simulator_yaml`: **flexnpu_core** granularity rounding, **raw core** annotation, `tasks[].spec` → **`template.spec`** mapping, **`runningTime`** → simulator annotation.
- `resolve_out_dir_pattern`: expands `{date}` in `outDir`.

### 5.3 FlexNPU analysis (`flexnpu_util_report`)

- From **stepResult**: per-card capacity in node annotations, **`flexnpu-num`** on the Job, container requests on **Running/Binding** Pods.
- **Per-card policy:** Round-robin within the node’s card ID list using each container’s card count.
- **Dual track**  
  - **Utilization (raw):** flexnpu_core prefers Pod annotation **`volcano.sh/flexnpu-core.percentage-raw-by-container`** (pre-round values); otherwise falls back to spec requests.  
  - **Allocation (granular):** flexnpu_core uses spec requests (already rounded by the loader), then applies the same ceil as the loader using snapshot **`npuGranularityPercent`**; **memory ignores granularity**, so allocation and utilization may coincide numerically for memory.
- **`compute_flexnpu_snapshot`** for CSV reuse; **`format_flexnpu_report` / `print_flexnpu_utilization`** for text reports.

### 5.4 CSV reports (`output_csv_reports`)

For one valid snapshot, files are written **flat** under the given directory (e.g. **`phase1/`**):

| File | Summary |
| --- | --- |
| **Node_desc.csv** | Node-level flexnpu core/memory: scheduler **Used/Allocatable** (scalar scaled per internal convention) as allocated/total and **allocation_rate**; per-card estimate aggregates as utilized/total and **utilization_rate**. Rate columns are **0–100 numbers without a `%` suffix**; integers render as integers (e.g. `100`). |
| **POD_desc.csv** | **Running/Binding/Pending** Pods (scheduled Binding/Running require a node name): flex requests, **phase**, **submit_time** (simulation submit time from `metadata.creationTimestamp` etc., **not** `startTime`), **`status.startTime`** (empty if unset), **card_used_quantity** (JSON, granular core/mem per card). |
| **npu_chip.csv** | Per node and card: **flexnpu_core_allocation_rate**, **flexnpu_core_utilization_rate**, **flexnpu_memory_allocation_rate**, **flexnpu_memory_utilization_rate** (0–100, two decimals, **no `%`**). |
| **summary.csv** | Node count, Running+Binding count, Pending count. |

**Prerequisite:** The snapshot must contain valid **Nodes**; otherwise `compute_flexnpu_snapshot` fails and the four CSVs above are **not** written (`SimRun` may still write task summary and flexnpu text; see code).

---

## 6. Non-functional requirements

- **Runtime:** Go toolchain can build `Volcano_simulator/cmd/sim`; Python 3.8+ recommended, with dependencies such as `requests`, `PyYAML`, `prettytable` (see actual imports).
- **Network:** Client and simulator use **HTTP** by default on **localhost:8006** (configurable in `SimRun.py`).
- **Result directories:** Large result trees should be listed in **`.gitignore`** (e.g. `Submit_volcano_workloads/result/`).

---

## 7. Terminology and resource keys

| Term | Description |
| --- | --- |
| flexnpu_core | Resource name `volcano.sh/flexnpu-core.percentage` (percentage-style scalar; meaning aligned with node cap annotations) |
| flexnpu_memory | Resource name `volcano.sh/flexnpu-memory.128mi` |
| submit_time | Pod creation timestamp semantics when the Job is admitted in simulation; not the Running start instant |
| status.startTime | Simulation time when the Pod enters the Running path |
| allocation rate / utilization rate | With granularity on core: allocation uses rounded per-card demand vs capacity; utilization uses “true” per-card demand vs capacity (when raw-core annotation is present) |

---

## 8. Document maintenance

- Root [**README.md**](../README.md): quick start and directory overview.  
- [**Submit_volcano_workloads/input_config/README.md**](../Submit_volcano_workloads/input_config/README.md): input file roles.  
- [**architecture.md**](./architecture.md): modules, HTTP and main loop, data flow.  
- [**00-overview.md**](./00-overview.md): plain-language overview and doc map.  
- **This document:** requirements and implemented-feature summary; keep it in sync when APIs or CSV columns change.
