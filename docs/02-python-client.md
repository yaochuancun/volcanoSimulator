# 02 Python client deep dive: controlling simulation and generating reports

> This document describes the Python client modules in detail: configuration loading, HTTP communication, resource analysis, and report generation.

---

## 1. Overall structure

```
Submit_volcano_workloads/
├── SimRun.py                    # Entry: main orchestration
├── sim_web_api.py               # Optional Web API / UI backend (serves static/)
├── static/                      # Web UI assets (HTML, JS, CSS)
├── common/
│   └── utils/
│       └── json_http_client.py  # HTTP client wrapper
├── input_config/                # Configuration system
│   ├── __init__.py
│   ├── input_config_loader.py   # YAML load and transform
│   ├── flexnpu_util_report.py   # FlexNPU resource analysis
│   ├── output_csv_reports.py    # CSV report generation
│   ├── README.md                # Configuration notes
│   ├── cluster/                 # Example cluster configs
│   ├── workload/                # Example workload configs
│   └── plugins/                 # Example plugin configs
└── figures/                     # Plotting scripts (legacy)
```

---

## 2. SimRun.py: the orchestrator

### 2.1 Role

**SimRun.py is the orchestrator for the whole simulation.** It:

1. Reads configuration files
2. Drives the Go simulator through init, scheduling, and result retrieval
3. Invokes report modules to produce outputs

### 2.2 Main flow walkthrough

```python
def main():
    # 1. Config paths (edit here to switch configs)
    sim_base_url = 'http://localhost:8006'
    cluster_path = 'input_config/cluster/cluster_1.yaml'
    workload_path = 'input_config/workload/workload_1.yaml'
    plugins_path = 'input_config/plugins/plugins.yaml'

    # 2. Load config (via input_config_loader)
    nodes_yaml = load_cluster_for_simulator(cluster_path)
    workload_yaml = load_workload_for_simulator(workload_path)
    npu_granularity = workload_npu_granularity_percent_from_file(workload_path)
    scheduler_conf_yaml, result_root = load_plugins_for_simulator(plugins_path)

    # 3. Create result directory
    os.makedirs(result_root, exist_ok=True)

    # 4. Run simulation
    reset(sim_base_url, nodes_yaml, workload_yaml)  # initialize
    time.sleep(1)
    step(sim_base_url, scheduler_conf_yaml, result_root, npu_granularity)  # schedule
```

### 2.3 reset in detail

**Purpose:** tell the Go simulator to start a **new** simulation from scratch.

```python
def reset(sim_base_url, nodes_yaml, workload_yaml):
    client = JsonHttpClient(sim_base_url)
    
    # POST /reset
    dicData = client.get_json('/reset', json={
        'period': "-1",           # -1: run scheduling config once
        'nodes': nodes_yaml,      # cluster YAML string
        'workload': workload_yaml, # workload YAML string
    })
    
    if str(dicData) == "0":
        print("Tasks still running; cannot reset")
    else:
        print("Simulation environment initialized")
```

**Notes:**

- `nodes_yaml` and `workload_yaml` are **strings**, not file paths
- Return value `"0"` means reset failed (work still in progress)

### 2.4 step in detail

**Purpose:** advance one scheduling round and collect results.

```python
def step(sim_base_url, scheduler_conf_yaml, pods_result_url, npu_granularity):
    client = JsonHttpClient(sim_base_url)
    
    # 1. Send scheduler configuration
    client.get_json('/step', json={'conf': scheduler_conf_yaml})
    
    # 2. Poll for results (scheduling is asynchronous)
    while True:
        time.sleep(0.2)  # 200 ms
        resultdata = client.get_json('/stepResult', json={'none': ""})
        
        if str(resultdata) == '0':
            continue  # not ready yet
        else:
            process_result(resultdata, pods_result_url, npu_granularity)
            break
```

**Polling diagram:**

```
Python          Go simulator
  │                 │
  │── POST /step ──→│  "Config received; schedule when ready"
  │                 │
  │←── returns "1" ─│
  │                 │
  │  (Go main loop runs scheduling on next tick)
  │                 │
  │── GET /stepResult ──→│ "Done yet?"
  │                      │
  │←── returns "0" ──────│ "Not yet"
  │                      │
  │── GET /stepResult ──→│ "Done yet?"
  │                      │
  │←── returns JSON ─────│ "Done; here is the snapshot"
```

### 2.5 Result processing

```python
def process_result(resultdata, pods_result_url, npu_granularity):
    # 1. Inject granularity for reports
    resultdata["npuGranularityPercent"] = float(npu_granularity or 0.0)
    
    # 2. tasksSUM.csv (task list)
    write_tasks_csv(resultdata, pods_result_url)
    
    # 3. pod_phase_count.txt (counts)
    write_phase_summary(resultdata, pods_result_url)
    
    # 4. flexnpu_utilization.txt (GPU utilization narrative)
    flexnpu_txt = print_flexnpu_utilization(resultdata)
    write_file(pods_result_url, "flexnpu_utilization.txt", flexnpu_txt)
    
    # 5. Four CSV report types
    write_output_config_csvs(resultdata, pods_result_url)
```

---

## 3. JsonHttpClient: HTTP layer

### 3.1 Design goals

Wrap HTTP calls with:

- Automatic JSON encode/decode
- Retry on failure
- Consistent error handling

### 3.2 Core code

```python
class JsonHttpClient:
    def __init__(self, base_url):
        self.base_url = base_url
    
    def get_json(self, path, json=None, max_retries=10):
        """
        Send an HTTP request with JSON handling.

        Args:
            path: API path, e.g. '/reset'
            json: Request body (Python dict, serialized to JSON)
            max_retries: Maximum retry attempts
        """
        url = self.base_url + path
        
        for attempt in range(max_retries):
            try:
                if json is not None:
                    # POST with JSON body
                    response = requests.post(url, json=json, timeout=10)
                else:
                    # GET
                    response = requests.get(url, timeout=10)
                
                response.raise_for_status()
                
                # Parse JSON; on failure return raw text
                try:
                    return response.json()
                except ValueError:
                    return response.text
                    
            except requests.RequestException as e:
                if attempt == max_retries - 1:
                    raise
                time.sleep(0.1)
```

### 3.3 Usage examples

```python
client = JsonHttpClient('http://localhost:8006')

# GET
result = client.get_json('/stepResultAnyway')

# POST with JSON body
result = client.get_json('/reset', json={
    'period': '-1',
    'nodes': 'yaml content...',
    'workload': 'yaml content...'
})
```

---

## 4. input_config_loader: YAML adapter

### 4.1 Role

Turn user-friendly YAML files into the shapes the simulator expects.

### 4.2 Main behaviors

#### 4.2.1 Cluster transformation

**User-facing YAML:**

```yaml
nodes:
  - name: node-1
    labels:
      accelerator: npu
    annotations:
      volcano.sh/flexnpu-core.percentage-list: "[100,100,100,100]"
    status:
      capacity:
        cpu: "32"
```

**Simulator-facing YAML:**

```yaml
cluster:
  - metadata:
      name: node-1
      labels:
        accelerator: npu
      annotations:
        volcano.sh/flexnpu-core.percentage-list: "[100,100,100,100]"
    spec:
      unschedulable: false
    status:
      capacity:
        cpu: "32"
```

**Transform function:**

```python
def cluster_input_to_simulator_yaml(doc):
    """Convert input_config-style cluster to simulator format."""
    nodes = doc.get("nodes", [])
    cluster = []
    
    for n in nodes:
        entry = {
            "metadata": {
                "name": n["name"],
                "labels": n.get("labels", {}),
                "annotations": n.get("annotations", {}),
            },
            "spec": n.get("spec", {"unschedulable": False}),
            "status": n.get("status", {}),
        }
        cluster.append(entry)
    
    return yaml.safe_dump({"cluster": cluster})
```

#### 4.2.2 Workload transformation

**Key behavior: granularity rounding**

```python
def workload_input_to_simulator_yaml(doc):
    """
    Convert workload config. Key steps:
    1. Round flexnpu_core requests up to granularity steps
    2. Record raw values in annotations
    3. Normalize task structure
    """
    granularity = float(doc.get("spec", {}).get("npuGranularityPercent", 0))
    
    for job in doc.get("jobs", []):
        for task in job.get("spec", {}).get("tasks", []):
            for container in task["template"]["spec"]["containers"]:
                resources = container.get("resources", {})
                
                raw_value = resources["requests"]["volcano.sh/flexnpu-core.percentage"]
                rounded_value = ceil_to_step(float(raw_value), granularity)
                
                resources["requests"]["volcano.sh/flexnpu-core.percentage"] = str(rounded_value)
                
                annotations = task["template"].setdefault("metadata", {}).setdefault("annotations", {})
                annotations["volcano.sh/flexnpu-core.percentage-raw-by-container"] = json.dumps({
                    container["name"]: float(raw_value)
                })
    
    return yaml.safe_dump({"jobs": jobs})
```

**Why round?**

With 10% granularity, a task requesting 35% compute:

- Raw: 35%
- Rounded up: 40% (next multiple of 10%)

The scheduler allocates 40%; reports can still show “requested 35%, allocated 40%” for utilization math.

#### 4.2.3 Plugin configuration

```python
def load_plugins_for_simulator(path):
    """
    Load plugins config. Returns:
    1. Scheduler config YAML string (for /step)
    2. Result output directory path
    """
    doc = yaml.safe_load(open(path))
    
    scheduler = doc["scheduler"]
    conf_str = yaml.safe_dump(scheduler)
    
    out_dir = doc.get("output", {}).get("outDir", "./result/{date}")
    out_dir = out_dir.replace("{date}", datetime.now().strftime("%Y-%m-%d-%H-%M-%S"))
    
    return conf_str, out_dir
```

---

## 5. flexnpu_util_report: resource analysis

### 5.1 Role

Analyze GPU-style resource usage and produce detailed utilization narratives.

### 5.2 Dual-track metrics

```
┌─────────────────────────────────────────────────────────────────┐
│                    FlexNPU resource view                        │
├───────────────────────────┬─────────────────────────────────────┤
│      Utilization          │         Allocation                  │
├───────────────────────────┼─────────────────────────────────────┤
│  Based on raw demand      │   Based on allocated (rounded)      │
│  (before rounding)        │                                     │
│                           │                                     │
│  Task asks 35% → use 35%  │   Task asks 35%, gets 40%           │
│                           │   → measure at 40%                    │
├───────────────────────────┼─────────────────────────────────────┤
│  Meaning: true demand     │   Meaning: capacity actually held   │
│                           │                                     │
│  Utilization = demand /   │   Allocation rate = allocated /   │
│  capacity                 │   capacity                          │
└───────────────────────────┴─────────────────────────────────────┘
```

**Why both?**

- Allocation-only views look “fully used” when everything is assigned.
- Rounding can waste capacity (35% need vs 40% slot).
- Comparing utilization (35%) vs allocation (40%) surfaces that gap.

### 5.3 Core idea: Pod → GPU card placement estimate

```python
def estimate_card_usage(node, pods, granularity):
    """
    Estimate how Pods map to GPU cards.

    Strategy: round-robin placement
    - Node has N cards
    - Each Pod needs M cards
    - Assign in order, cycling through cards
    """
    card_list = parse_card_list(node["annotations"])
    # e.g. [("card0", 100, 64), ("card1", 100, 64), ...]
    
    pod_card_shares = {}
    for pod in pods:
        num_cards = int(pod["annotations"].get("volcano.sh/flexnpu-num", 1))
        
        core_per_card = pod["core_request"] / num_cards
        mem_per_card = pod["mem_request"] / num_cards
        
        assigned_cards = []
        for i in range(num_cards):
            card_id = (next_card_index + i) % len(card_list)
            assigned_cards.append({
                "card": card_list[card_id],
                "core": core_per_card,
                "memory": mem_per_card,
            })
        
        pod_card_shares[pod["name"]] = assigned_cards
        next_card_index += num_cards
    
    return pod_card_shares
```

### 5.4 Sample report output

```
FlexNPU utilization report
==========================

Node: node-1
Total cards: 4

Node summary:
  Compute — allocated: 140.0% / total: 400.0% / allocation rate: 35.00%
  Compute — demand: 125.0% / total: 400.0% / utilization: 31.25%
  Memory — allocated: 48.0 / total: 256.0 / allocation rate: 18.75%

Per-card detail:
  Card 0: core allocation 40.00% / utilization 35.00% / memory allocation 16.00%
  Card 1: core allocation 35.00% / utilization 30.00% / memory allocation 14.00%
  Card 2: core allocation 35.00% / utilization 30.00% / memory allocation 12.00%
  Card 3: core allocation 30.00% / utilization 30.00% / memory allocation 10.00%

Pod placement:
  pod-A -> Card 0 (40% core, 16 memory)
  pod-B -> Card 1 (35% core, 14 memory)
  ...
```

---

## 6. output_csv_reports: CSV outputs

### 6.1 Role

Turn simulation snapshots into structured CSV for Excel, Pandas, etc.

### 6.2 Four CSV files

| File | Contents | Use |
|------|----------|-----|
| `Node_desc.csv` | Per-node resource rollup | Node load balance |
| `POD_desc.csv` | Per-Pod detail | Placement drill-down |
| `npu_chip.csv` | Per-GPU-card utilization | Card-level usage |
| `summary.csv` | Aggregate stats | Quick experiment readout |

### 6.3 Node_desc.csv example

```csv
node_name,flexnpu_core_allocated,flexnpu_core_total,flexnpu_core_allocation_rate,flexnpu_core_utilized,flexnpu_core_utilization_rate
node-1,140.0,400.0,35.0,125.0,31.25
node-2,200.0,400.0,50.0,180.0,45.0
```

**Fields:**

- `*_allocated`: resources after rounding (what was assigned)
- `*_utilized`: raw demand (before rounding)
- `*_rate`: percentage as 0–100 (no `%` suffix)

### 6.4 POD_desc.csv example

```csv
pod_name,job_name,phase,node_name,flexnpu_core_request,flexnpu_memory_request,submit_time,start_time,card_used_quantity
pod-A,job-1,Running,node-1,40.0,16,2026-04-07T10:00:00,2026-04-07T10:00:05,"{\"card0\": {\"core\": 40.0, \"memory\": 16}}"
```

---

## 7. Summary

Python client design in one picture:

```
Config loader (adapt YAML)
      ↓
HTTP client (talk to simulator)
      ↓
Result processing (analyze)
      ↓
Report writers (CSV / text)
```

**Loosely coupled modules on a data pipeline:**

- Change input YAML shape? → `input_config_loader`
- Change transport? → `json_http_client`
- Change report columns? → `output_csv_reports`
- Change resource model? → `flexnpu_util_report` and the Go side

This **single responsibility + data-driven** split keeps the client easy to follow and maintain.

---

Ready for the Go simulator internals? Continue with [`03-go-simulator.md`](03-go-simulator.md).
