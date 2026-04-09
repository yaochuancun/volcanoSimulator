# VolcanoSimulator — Plain-language overview

> In one sentence: **this is a simulation system that runs on your machine and mimics Kubernetes + Volcano scheduling so you can test and validate scheduling policies without a real K8s cluster.**

---

## 1. What is this? What problem does it solve?

### Pain points in the real world

Imagine you operate an AI training fleet:
- 100 machines, each with 8 GPUs
- 1,000 training jobs waiting to run
- Jobs vary in size—some need 8 GPUs, others only 1
- You want to try a new scheduling policy and see if jobs finish faster and GPU utilization improves

**The dilemma:**
- Test on production? Too risky for live workloads
- Build a full test cluster? Expensive at that scale
- Hack a toy simulator? Too crude to reflect real scheduler behavior

### What VolcanoSimulator offers

On **a single laptop** you can:
1. **Simulate** a cluster of 100 machines and 800 GPUs
2. **Simulate** submission and execution of 1,000 jobs
3. **Run** real Volcano scheduler code (not a toy reimplementation)
4. **Inspect** outcomes: which jobs land on which machines, GPU utilization, completion times
5. **Compare** different scheduling policies

**Core traits: lightweight, faithful, observable**

---

## 2. What does the system look like?

### Two main parts

```
┌─────────────────────────────────────────────────────────────────┐
│                      VolcanoSimulator                           │
├──────────────────────────────┬──────────────────────────────────┤
│     Python client            │        Go simulator              │
│   (config + control + reports)│   (scheduler core + state)      │
├──────────────────────────────┼──────────────────────────────────┤
│  • Read YAML configs         │  • Hold virtual cluster state    │
│  • HTTP to drive simulation   │  • Run real Volcano scheduler   │
│  • Build reports from results │  • Advance simulated time/tasks  │
│  • FlexNPU utilization views │  • Return cluster snapshots      │
└──────────────────────────────┴──────────────────────────────────┘
                    ↑↓ HTTP (port 8006)
```

### Analogy: city-building game

| Real world | City-builder game | VolcanoSimulator |
|------------|-------------------|------------------|
| Real city | In-game city | Virtual K8s cluster |
| Citizens | NPCs | Virtual workloads |
| City administration | Game engine | Go simulator |
| Save/load | Save files | YAML configs |
| UI and stats | Game UI | Python client |

**Python client** = UI + save manager  
- Pick a save (read YAML)  
- “Start game” (`/reset`)  
- “Next turn” (`/step`)  
- View stats (CSV and utilization reports)

**Go simulator** = engine  
- World state (cluster)  
- Rules (scheduling)  
- Time (simulation clock)

---

## 3. Workflow: one full simulation

### Prepare → run → observe

```
Step 1 — Prepare (write config)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Three YAML files:

  cluster.yaml      →  What machines exist in the “datacenter”?
  workload.yaml     →  What jobs should run?
  plugins.yaml      →  Which scheduling policy?

Step 2 — Run (Python drives Go)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The Python client:

  ① Read YAML → build HTTP payloads
  ② POST /reset → initialize cluster and jobs
  ③ POST /step  → run one scheduling round
  ④ Poll /stepResult → read snapshot
  ⑤ Repeat ③④ until scheduling stabilizes (per your script)

Step 3 — Observe (reports)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Outputs typically include:

  tasksSUM.csv           →  Per-task status and placement
  flexnpu_utilization.txt →  GPU / FlexNPU utilization narrative
  Node_desc.csv          →  Per-node resource picture
  POD_desc.csv           →  Per-pod detail
  npu_chip.csv           →  Per-card utilization
  summary.csv            →  Aggregate stats
```

---

## 4. Core concepts

### 4.1 Cluster

**Reality:** physical servers on a network  
**Simulation:** an in-memory structure describing each virtual machine

```yaml
# cluster.yaml excerpt
nodes:
  - name: node-1
    labels:
      accelerator: npu
    annotations:
      volcano.sh/flexnpu-core.percentage-list: "[100,100,100,100]"
      volcano.sh/flexnpu-memory.128mi-list: "[64,64,64,64]"
    status:
      allocatable:
        cpu: "32"
        memory: "128Gi"
```

### 4.2 Job / workload

**Reality:** e.g. a PyTorch training job  
**Simulation:** YAML describing resources and submit time

```yaml
# workload.yaml excerpt
jobs:
  - metadata:
      name: training-job-1
      labels:
        sub-time: "0"
    spec:
      npuGranularityPercent: 10
      tasks:
        - replicas: 4
          template:
            spec:
              containers:
                - name: train
                  resources:
                    requests:
                      volcano.sh/flexnpu-core.percentage: "35"
                      volcano.sh/flexnpu-memory.128mi: "16"
```

### 4.3 Scheduling

**Reality:** Kubernetes/Volcano places Pods on nodes  
**Simulation:** Volcano framework applies the configured plugin chain to virtual jobs and nodes

```yaml
# plugins.yaml excerpt
scheduler:
  actions: "enqueue, allocate, backfill"
  tiers:
    - plugins:
        - name: gang
        - name: drf
        - name: binpack
```

### 4.4 FlexNPU (GPU-style model)

This project models **splittable** “NPU/GPU” capacity.

**Traditional GPU:** a card is fully assigned to one consumer or not  
**FlexNPU:** a 100% card can be shared—e.g. job A 30%, job B 50%, 20% idle

**Why:** mirrors virtualization patterns (MIG, vGPU, etc.) the simulator is meant to study.

---

## 5. How to read the docs

The series goes **from coarse to fine**:

| Document | Contents | Audience |
|----------|----------|----------|
| `00-overview.md` | This file | Everyone—quick orientation |
| `architecture.md` | Architecture, modules, HTTP, data flow | Design and integration |
| `02-python-client.md` | Python side in depth | Client / Web backend contributors |
| `03-go-simulator.md` | Go simulator in depth | Simulator contributors |
| `04-flexnpu.md` | FlexNPU resource model | GPU/NPU scheduling research |
| `05-data-flow-and-http.md` | HTTP protocol and flow | Integration and debugging |
| `06-configuration-guide.md` | Writing YAML configs | Experiment users |
| `07-code-walkthrough.md` | Key code paths line by line | Deep implementation dive |

**Suggested order:**  
1. This overview  
2. `architecture.md`  
3. Then pick topic guides as needed  

Also see root **`README.md`**, **`docs/requirements.md`**, and the Web UI under **`Submit_volcano_workloads/sim_web_api.py`** + **`static/`**.

---

## 6. Quick start (~5 minutes)

### 1. Start the Go simulator

```bash
cd Volcano_simulator/cmd/sim
go build -o sim .
./sim
# listens on localhost:8006
```

### 2. Run the Python client

```bash
cd Submit_volcano_workloads
pip install -r requirements.txt
python SimRun.py
```

### 3. Inspect results

```bash
ls result/<timestamp>/
# e.g. tasksSUM.csv, flexnpu_utilization.txt
```

---

## 7. One-line summary

**VolcanoSimulator = a lightweight way to simulate and test Kubernetes + Volcano scheduling from YAML configs on your laptop.**

Next: read **`architecture.md`** for the full structural picture.
