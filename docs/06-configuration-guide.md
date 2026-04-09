# 06 — Configuration guide: how to write experiment configs

> This document explains how to write cluster, workload, and plugin configuration files, plus common configuration patterns.

---

## 1. Configuration files overview

```
input_config/
├── cluster/              # Cluster config: defines your "virtual datacenter"
│   ├── cluster.yaml
│   └── cluster_1.yaml
├── workload/             # Workload config: defines what jobs to run
│   ├── workload.yaml
│   └── workload_1.yaml
└── plugins/              # Plugin config: defines scheduling policy
    ├── plugins.yaml
    └── plugins_mvp.yaml
```

---

## 2. Cluster configuration (`cluster/*.yaml`)

### 2.1 Basic structure

```yaml
# Cluster config defines machines in your "virtual datacenter"
nodes:
  - name: node-1                    # Machine name (unique)
    labels:                         # Labels (for scheduling filters)
      accelerator: npu
      rack: rack-a
    annotations:                    # Annotations (extra metadata)
      # Most important: how many GPU cards, compute % and memory per card
      volcano.sh/flexnpu-core.percentage-list: "[100, 100, 100, 100]"
      volcano.sh/flexnpu-memory.128mi-list: "[64, 64, 64, 64]"
    spec:
      unschedulable: false          # Schedulable? (set true for maintenance)
    status:
      capacity:                     # Total capacity
        cpu: "32"
        memory: "128Gi"
      allocatable:                  # Allocatable (minus system reserve)
        cpu: "28"
        memory: "120Gi"
```

### 2.2 FlexNPU annotations in detail

**`volcano.sh/flexnpu-core.percentage-list`**
- Format: JSON array
- Meaning: compute percentage per GPU card
- Example: `"[100, 100, 100, 100]"` = 4 cards, 100% compute each

**`volcano.sh/flexnpu-memory.128mi-list`**
- Format: JSON array
- Meaning: GPU memory per card in units of 128Mi
- Example: `"[64, 64, 64, 64]"` = 64×128Mi = 8GB per card

**Multi-node example:**

```yaml
nodes:
  # High-performance node: 8× A100, 100% compute each, 80GB VRAM each
  - name: gpu-a100-x8
    labels:
      accelerator: npu
      npu-type: a100
    annotations:
      volcano.sh/flexnpu-core.percentage-list: "[100,100,100,100,100,100,100,100]"
      volcano.sh/flexnpu-memory.128mi-list: "[640,640,640,640,640,640,640,640]"
    status:
      capacity:
        cpu: "128"
        memory: "1024Gi"

  # Mid-tier node: 4× T4, 100% compute each, 16GB VRAM each
  - name: gpu-t4-x4
    labels:
      accelerator: npu
      npu-type: t4
    annotations:
      volcano.sh/flexnpu-core.percentage-list: "[100,100,100,100]"
      volcano.sh/flexnpu-memory.128mi-list: "[128,128,128,128]"
    status:
      capacity:
        cpu: "32"
        memory: "128Gi"

  # CPU-only node: no GPU
  - name: cpu-only
    labels:
      accelerator: none
    status:
      capacity:
        cpu: "64"
        memory: "256Gi"
```

### 2.3 Using node labels

**For scheduling filters:**

```yaml
# Cluster config
nodes:
  - name: node-1
    labels:
      accelerator: npu      # Has GPU
      zone: zone-a            # Zone

# Workload config
jobs:
  - spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: accelerator
                    operator: In
                    values: ["npu"]
```

---

## 3. Workload configuration (`workload/*.yaml`)

### 3.1 Basic structure

```yaml
# Workload config defines what jobs to run
spec:
  npuGranularityPercent: 10       # Resource granularity (optional, default 0)

jobs:
  - metadata:
      name: training-job-1
      namespace: default
      labels:
        sub-time: "0"             # Submit time (seconds); 0 = submit immediately
        queue: default
    spec:
      minAvailable: 4             # Gang: need at least 4 Pods to start
      schedulerName: volcano
      tasks:
        - name: train
          replicas: 4             # Start 4 Pods
          template:
            metadata:
              annotations:
                volcano.sh/flexnpu-num: "2"  # 2 cards per Pod
            spec:
              containers:
                - name: train
                  image: pytorch/pytorch:latest
                  resources:
                    requests:
                      cpu: "4"
                      memory: "16Gi"
                      volcano.sh/flexnpu-core.percentage: "35"   # 35% compute per card
                      volcano.sh/flexnpu-memory.128mi: "64"      # 8GB VRAM per card
                    limits:
                      cpu: "8"
                      memory: "32Gi"
```

### 3.2 Key fields

**`spec.npuGranularityPercent`**
- Purpose: minimum step for GPU compute allocation
- Example: `10` means only 10%, 20%, 30%… not 35%
- If a job requests 35%, it rounds up to 40%

**`metadata.labels.sub-time`**
- Purpose: job submit time relative to simulation start
- Unit: seconds
- Example: `"0"` = submit at t=0, `"60"` = submit at t=60

**`spec.minAvailable`**
- Purpose: Gang scheduling parameter
- Meaning: at least this many Pods must be schedulable together
- Example: `4` means all four Pods start together or none do

**`volcano.sh/flexnpu-num`**
- Purpose: how many GPU cards each Pod needs
- Example: `"2"` means this Pod needs two cards
- Total demand = num × per-card request

### 3.3 Multi-job scenario

```yaml
spec:
  npuGranularityPercent: 10

jobs:
  # Job 1: large model training (8-way, submit immediately)
  - metadata:
      name: llm-training
      labels:
        sub-time: "0"
    spec:
      minAvailable: 8
      tasks:
        - name: train
          replicas: 8
          template:
            metadata:
              annotations:
                volcano.sh/flexnpu-num: "1"
            spec:
              containers:
                - resources:
                    requests:
                      volcano.sh/flexnpu-core.percentage: "100"  # exclusive full card
                      volcano.sh/flexnpu-memory.128mi: "640"     # 80GB VRAM

  # Job 2: small model (2 cards, submit after 30s)
  - metadata:
      name: small-training
      labels:
        sub-time: "30"
    spec:
      minAvailable: 2
      tasks:
        - name: train
          replicas: 2
          template:
            metadata:
              annotations:
                volcano.sh/flexnpu-num: "1"
            spec:
              containers:
                - resources:
                    requests:
                      volcano.sh/flexnpu-core.percentage: "50"
                      volcano.sh/flexnpu-memory.128mi: "128"

  # Job 3: inference (1 card, submit after 60s)
  - metadata:
      name: inference
      labels:
        sub-time: "60"
    spec:
      tasks:
        - name: serve
          replicas: 4
          template:
            metadata:
              annotations:
                volcano.sh/flexnpu-num: "1"
            spec:
              containers:
                - resources:
                    requests:
                      volcano.sh/flexnpu-core.percentage: "20"
                      volcano.sh/flexnpu-memory.128mi: "32"
```

### 3.4 Different priorities

```yaml
jobs:
  # High-priority job
  - metadata:
      name: urgent-job
    spec:
      priorityClassName: high-priority    # references PriorityClass
      tasks:
        - replicas: 2
          template:
            spec:
              containers:
                - resources:
                    requests:
                      volcano.sh/flexnpu-core.percentage: "60"

  # Low-priority job (may be preempted)
  - metadata:
      name: background-job
    spec:
      priorityClassName: low-priority
      tasks:
        - replicas: 4
          template:
            spec:
              containers:
                - resources:
                    requests:
                      volcano.sh/flexnpu-core.percentage: "30"
```

---

## 4. Plugin configuration (`plugins/*.yaml`)

### 4.1 Basic structure

```yaml
# Plugin config defines scheduling policy
scheduler:
  actions: "enqueue, allocate, backfill"  # Scheduling action order

  tiers:
    # Tier 1: required plugins (hard constraints)
    - plugins:
        - name: gang          # Gang scheduling
        - name: drf           # dominant resource fairness
        - name: proportion    # queue proportions

    # Tier 2: optimization plugins (soft constraints)
    - plugins:
        - name: nodeorder     # node selection
        - name: binpack       # tight packing

# Output settings
output:
  outDir: "./result/{date}"   # Result directory; {date} is replaced with a timestamp
```

### 4.2 Actions (scheduling actions)

**Order matters.**

```yaml
# Recommended 1: standard batch
actions: "enqueue, allocate, backfill"
# enqueue:   enqueue eligible Jobs
# allocate:  assign nodes to work
# backfill:  fill gaps with small jobs

# Recommended 2: with preemption
actions: "enqueue, allocate, preempt, backfill"
# preempt:   preempt lower-priority work

# Recommended 3: with reservation
actions: "enqueue, reserve, allocate, backfill"
# reserve:   reserve capacity for important jobs
```

### 4.3 Plugins (scheduling plugins)

**Tier 1: queues and resource management**

```yaml
tiers:
  - plugins:
      # Gang: start a group of tasks together
      - name: gang
        arguments:
          minJobExecutionTime: 10m    # min runtime to reduce thrashing from preemption

      # DRF: dominant resource fairness
      - name: drf
        arguments:
          preselectEnable: true       # preselection

      # Proportion: per-queue resource share
      - name: proportion
        arguments:
          decayFactor: 0.9            # decay factor
```

**Tier 2: node choice and optimization**

```yaml
    - plugins:
        # NodeOrder: node scoring
        - name: nodeorder
          arguments:
            leastResourceWeight: 100   # spread across nodes
            mostResourceWeight: 0      # binpack bias

        # Binpack: tight packing (higher utilization)
        - name: binpack
          arguments:
            weight: 10                 # weight

        # Predicates: hard constraint checks
        - name: predicates
          arguments:
            predicate.GPUSharingEnable: true   # enable GPU sharing
```

### 4.4 Common patterns

**Pattern 1: maximize utilization (binpack)**

```yaml
scheduler:
  actions: "enqueue, allocate, backfill"
  tiers:
    - plugins:
        - name: gang
        - name: drf
    - plugins:
        - name: binpack           # tight packing
          arguments:
            weight: 100
```

**Pattern 2: load spread**

```yaml
scheduler:
  actions: "enqueue, allocate, backfill"
  tiers:
    - plugins:
        - name: gang
        - name: drf
    - plugins:
        - name: nodeorder
          arguments:
            leastResourceWeight: 100   # spread across nodes
            mostResourceWeight: 0
```

**Pattern 3: with preemption**

```yaml
scheduler:
  actions: "enqueue, allocate, preempt, backfill"
  tiers:
    - plugins:
        - name: gang
        - name: priority        # priority
        - name: drf
    - plugins:
        - name: preempt         # preemption
        - name: nodeorder
```

**Pattern 4: queue isolation**

```yaml
scheduler:
  actions: "enqueue, allocate, backfill"
  tiers:
    - plugins:
        - name: gang
        - name: proportion      # queue share control
          arguments:
            queue.capabilities: "[gpu]"
    - plugins:
        - name: nodeorder
```

---

## 5. Full examples

### 5.1 Small experiment

```yaml
# cluster_small.yaml
nodes:
  - name: node-1
    labels:
      accelerator: npu
    annotations:
      volcano.sh/flexnpu-core.percentage-list: "[100, 100]"
      volcano.sh/flexnpu-memory.128mi-list: "[64, 64]"
    status:
      capacity:
        cpu: "16"
        memory: "64Gi"

  - name: node-2
    labels:
      accelerator: npu
    annotations:
      volcano.sh/flexnpu-core.percentage-list: "[100, 100]"
      volcano.sh/flexnpu-memory.128mi-list: "[64, 64]"
    status:
      capacity:
        cpu: "16"
        memory: "64Gi"
```

```yaml
# workload_small.yaml
spec:
  npuGranularityPercent: 10

jobs:
  - metadata:
      name: job-a
      labels:
        sub-time: "0"
    spec:
      minAvailable: 2
      tasks:
        - name: train
          replicas: 2
          template:
            metadata:
              annotations:
                volcano.sh/flexnpu-num: "1"
            spec:
              containers:
                - resources:
                    requests:
                      volcano.sh/flexnpu-core.percentage: "50"

  - metadata:
      name: job-b
      labels:
        sub-time: "5"
    spec:
      minAvailable: 2
      tasks:
        - name: train
          replicas: 2
          template:
            metadata:
              annotations:
                volcano.sh/flexnpu-num: "1"
            spec:
              containers:
                - resources:
                    requests:
                      volcano.sh/flexnpu-core.percentage: "60"
```

```yaml
# plugins_small.yaml
scheduler:
  actions: "enqueue, allocate, backfill"
  tiers:
    - plugins:
        - name: gang
        - name: drf
    - plugins:
        - name: nodeorder
        - name: binpack

output:
  outDir: "./result/small_{date}"
```

### 5.2 Large experiment

```yaml
# cluster_large.yaml
nodes:
  # 8-GPU nodes × 10
  - name: gpu-node-{1..10}
    labels:
      accelerator: npu
    annotations:
      volcano.sh/flexnpu-core.percentage-list: "[100,100,100,100,100,100,100,100]"
      volcano.sh/flexnpu-memory.128mi-list: "[640,640,640,640,640,640,640,640]"
    status:
      capacity:
        cpu: "128"
        memory: "1024Gi"
```

```yaml
# workload_large.yaml
spec:
  npuGranularityPercent: 5

jobs:
  # Large jobs × 5
  - metadata:
      name: large-job-{1..5}
      labels:
        sub-time: "{0,60,120,180,240}"
    spec:
      minAvailable: 8
      tasks:
        - name: train
          replicas: 8
          template:
            metadata:
              annotations:
                volcano.sh/flexnpu-num: "1"
            spec:
              containers:
                - resources:
                    requests:
                      volcano.sh/flexnpu-core.percentage: "100"

  # Small jobs × 20
  - metadata:
      name: small-job-{1..20}
      labels:
        sub-time: "{0,30,60,...}"
    spec:
      minAvailable: 2
      tasks:
        - name: train
          replicas: 2
          template:
            metadata:
              annotations:
                volcano.sh/flexnpu-num: "1"
            spec:
              containers:
                - resources:
                    requests:
                      volcano.sh/flexnpu-core.percentage: "30"
```

---

## 6. Configuration checklist

### 6.1 Cluster

- [ ] `nodes` is non-empty
- [ ] Every node has `name`
- [ ] `flexnpu-core.percentage-list` is valid (JSON array)
- [ ] `flexnpu-memory.128mi-list` is valid (JSON array)
- [ ] Both lists have the same length (compute + memory per card)
- [ ] `capacity` and `allocatable` are sensible

### 6.2 Workload

- [ ] `jobs` is non-empty
- [ ] Each Job has `metadata.name`
- [ ] `sub-time` is a numeric string (e.g. `"0"`)
- [ ] `tasks[0].replicas` is a positive integer
- [ ] `flexnpu-core.percentage` is between 0 and 100
- [ ] `flexnpu-num` is a positive integer
- [ ] Requests fit within total node capacity

### 6.3 Plugins

- [ ] `actions` is non-empty
- [ ] `tiers` has at least one tier
- [ ] Plugin names are correct (gang, drf, binpack, etc.)
- [ ] `output.outDir` is a valid path

---

## 7. FAQ

### 7.1 `"0"` response

**Symptom:** reset returns `"0"`

**Cause:** the previous simulation has not finished

**Fix:**

```bash
# Option 1: wait for the current run to finish
# Option 2: restart the Go simulator
pkill sim
./sim
```

### 7.2 Insufficient resources

**Symptom:** tasks stay Pending

**Check:**
1. Total node capacity vs. demand
2. Whether `minAvailable` is too large
3. Whether Gang preconditions are satisfied

### 7.3 Config seems ignored

**Symptom:** you changed config but results did not change

**Check:**
1. You edited the intended files
2. `SimRun.py` points at the right paths
3. Restart the Go simulator to clear state

---

## 8. Summary

**Three steps to writing config:**

1. **Cluster:** define your “datacenter”
   - How many machines?
   - How many GPUs per machine?
   - Compute and memory per card?

2. **Workload:** define your “jobs”
   - What jobs?
   - When to submit?
   - How much resource?

3. **Plugins:** define “scheduling policy”
   - Which algorithms?
   - Favor utilization or spread?
   - Where to write results?

**Treat configuration as code — version-control your experiments.**

---

Ready to go deeper into the code? Continue with [07 — Code walkthrough](07-code-walkthrough.md).
