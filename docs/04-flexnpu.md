# 04 FlexNPU deep dive: GPU resource scheduling model

> This document explains the FlexNPU resource model in depth. It is one of VolcanoSimulator’s core features and is used to simulate splittable GPU resources.

---

## 1. Why FlexNPU?

### 1.1 Problems with traditional GPUs

**Two ways GPUs are used in practice:**

```
Mode 1: Whole-card allocation (exclusive)
┌─────────────────┐
│     GPU 0       │  ← Job A monopolizes 100% compute
│   100% compute   │
│    32GB VRAM     │
└─────────────────┘
Problem: The job only needs 30% compute; the other 70% is wasted!

Mode 2: Virtualized slices (shared)
┌─────────────────┐
│  GPU 0          │
├────────┬────────┤
│ Job A  │ Job B  │
│  30%   │  50%   │
│  10GB  │  20GB  │
└────────┴────────┘
Benefit: Resources are allocated on demand; utilization improves
```

**FlexNPU simulates this kind of GPU virtualization.**

### 1.2 Core capabilities of FlexNPU

1. **Compute splitting**: A card with 100% compute can be divided into multiple shares
2. **Memory splitting**: VRAM can also be allocated on demand
3. **Multi-card support**: A single task can use resources from multiple cards
4. **Granularity control**: Set a minimum allocation step (e.g. 10% increments)

---

## 2. Resource model

### 2.1 Three resource keys

```go
// Compute resource (percentage)
volcano.sh/flexnpu-core.percentage

// Memory resource (unit: 128Mi)
volcano.sh/flexnpu-memory.128mi

// Number of cards (how many cards one task uses)
volcano.sh/flexnpu-num
```

**Why is memory in units of 128Mi?**
- Integers avoid floating-point precision issues
- 128Mi is a power of two, which simplifies arithmetic
- Actual VRAM = value × 128Mi

### 2.2 Node-level resource definition

**A node (machine) must declare which GPU cards it has:**

```yaml
apiVersion: v1
kind: Node
metadata:
  name: node-1
  annotations:
    # This machine has 4 cards; per-card compute and memory
    volcano.sh/flexnpu-core.percentage-list: "[100, 100, 100, 100]"
    volcano.sh/flexnpu-memory.128mi-list: "[64, 64, 64, 64]"
    # Meaning: 4 cards, each 100% compute and 8GB VRAM (64×128Mi = 8GB)
```

**List format:**
- Index 0 → Card 0
- Index 1 → Card 1
- …

### 2.3 Task-level resource requests

**A task (Pod) must declare how much GPU resource it needs:**

```yaml
apiVersion: v1
kind: Pod
metadata:
  annotations:
    volcano.sh/flexnpu-num: "2"  # Needs 2 cards
spec:
  containers:
    - name: train
      resources:
        requests:
          volcano.sh/flexnpu-core.percentage: "35"   # 35% compute per card
          volcano.sh/flexnpu-memory.128mi: "16"      # 2GB VRAM per card
```

**This task’s resource demand:**
- Uses 2 cards
- 35% compute per card
- 2GB VRAM per card
- Totals: 70% compute, 4GB VRAM

---

## 3. Granularity rounding

### 3.1 Why round?

**Scenario:** Granularity is 10%; the task requests 35% compute

```
Request: 35%
Step: 10%
After rounding: 40% (rounded up to a multiple of 10)

Reason: The scheduler allocates in step units; 35 is not a multiple of 10, so it cannot be allocated exactly
Outcome: 40% is actually allocated—5% over-request
```

### 3.2 Dual-track accounting

To track this “over-allocation,” the system uses **dual-track statistics**:

```
┌──────────────────────────────────────────────────────────────┐
│              Resource accounting (dual track)                 │
├────────────────────────┬─────────────────────────────────────┤
│   Utilization          │        Allocation rate               │
├────────────────────────┼─────────────────────────────────────┤
│ Based on raw demand    │  Based on actual allocation          │
│ (35%)                  │  (40%)                               │
│                        │                                     │
│ Compute: 35% / total   │  Compute: 40% / total                │
│                        │                                     │
│ Meaning: true resource │  Meaning: resources actually          │
│ need                   │  reserved                             │
├────────────────────────┼─────────────────────────────────────┤
│ Use for: whether       │  Use for: understanding how         │
│ resources are really   │  resources are being assigned         │
│ used                   │                                     │
└────────────────────────┴─────────────────────────────────────┘
```

**Comparison example:**

| Metric | Calculation | Meaning |
|--------|-------------|---------|
| Utilization | 35% / 100% = 35% | The task only needs 35% |
| Allocation rate | 40% / 100% = 40% | Because of granularity, 40% was allocated |
| Waste | 40% − 35% = 5% | 5% of compute is wasted |

### 3.3 Code implementation

**Rounding logic in `input_config_loader.py`:**

```python
def _ceil_to_step(value: float, step: float) -> float:
    """Round up to the nearest multiple of step."""
    if step <= 0:
        return value
    return math.ceil(value / step) * step


def _round_resource_map(resources: Dict, granularity_percent: float) -> None:
    """Apply granularity rounding to flexnpu_core."""
    key = "volcano.sh/flexnpu-core.percentage"
    if key not in resources:
        return
    
    raw_value = float(resources[key])
    rounded_value = _ceil_to_step(raw_value, granularity_percent)
    
    # Write back the rounded value
    resources[key] = str(int(rounded_value))
```

**Also recording raw values:**

```python
def _normalize_task_templates(tasks: List[Dict], granularity: float) -> None:
    """Normalize task structure and record raw core values."""
    raw_by_container: Dict[str, float] = {}
    
    for container in pod_spec.get("containers", []):
        cname = container.get("name", "__default__")
        res = container.get("resources", {})
        
        # Record raw value before rounding
        raw_value = float(res["requests"]["volcano.sh/flexnpu-core.percentage"])
        raw_by_container[cname] = raw_value
        
        # Write back after rounding
        rounded = _ceil_to_step(raw_value, granularity)
        res["requests"]["volcano.sh/flexnpu-core.percentage"] = str(rounded)
    
    # Store raw values in annotations for reporting
    meta = task.setdefault("metadata", {})
    ann = meta.setdefault("annotations", {})
    ann["volcano.sh/flexnpu-core.percentage-raw-by-container"] = json.dumps(raw_by_container)
```

---

## 4. Pod → GPU card assignment estimate

### 4.1 Problem statement

After scheduling we know:
- Which node each Pod landed on
- How much GPU resource each Pod requested

We do not know:
- Which specific cards on the node each Pod uses
- Which Pods run on each card

**Why estimate?**
- In real Kubernetes, the kubelet assigns GPUs; the scheduler does not see card-level placement
- For utilization analysis we still need per-card usage
- So we use an algorithm to estimate a “reasonable” assignment

### 4.2 Round-robin assignment algorithm

```python
def estimate_card_usage(node, pods, granularity):
    """
    Round-robin assignment algorithm
    
    Strategy:
    1. The node has N cards
    2. Each Pod needs M cards
    3. In Pod order, assign slots round-robin across cards
    
    Example:
    Node: 4 cards (0, 1, 2, 3)
    Pod A: needs 2 cards → cards 0, 1
    Pod B: needs 1 card  → card 2
    Pod C: needs 2 cards → cards 3, 0
    """
    
    # 1. Parse the node’s card list
    card_list = parse_card_list(node["annotations"])
    # [("card0", 100, 64), ("card1", 100, 64), ...]
    
    # 2. Assign cards to each Pod in order
    pod_card_shares = {}
    next_card_index = 0
    
    for pod in pods:
        # How many cards the Pod needs
        num_cards = int(pod["annotations"].get("volcano.sh/flexnpu-num", 1))
        
        # Per-card resource demand
        total_core = pod["core_request"]
        total_mem = pod["mem_request"]
        core_per_card = total_core / num_cards
        mem_per_card = total_mem / num_cards
        
        # Round-robin assignment
        assigned_cards = []
        for i in range(num_cards):
            card_idx = (next_card_index + i) % len(card_list)
            card_id, card_cap, card_mem = card_list[card_idx]
            
            assigned_cards.append({
                "card_id": card_id,
                "core": core_per_card,
                "memory": mem_per_card,
            })
        
        pod_card_shares[pod["name"]] = assigned_cards
        next_card_index += num_cards
    
    return pod_card_shares
```

### 4.3 Assignment example

```
Node node-1: 4 cards, each 100% compute and 64 memory units

Pods to place (in schedule order):
  Pod A: 2 cards, 30% compute and 16 memory per card
  Pod B: 1 card, 40% compute and 32 memory per card
  Pod C: 2 cards, 20% compute and 8 memory per card

Round-robin steps:
  Pod A → Card 0 (30%, 16) + Card 1 (30%, 16)
  Pod B → Card 2 (40%, 32)
  Pod C → Card 3 (20%, 8) + Card 0 (20%, 8)

Final outcome:
  Card 0: Pod A (30%) + Pod C (20%) = 50% utilization
  Card 1: Pod A (30%) = 30% utilization
  Card 2: Pod B (40%) = 40% utilization
  Card 3: Pod C (20%) = 20% utilization
```

---

## 5. Utilization calculation

### 5.1 Node-level utilization

```python
def compute_node_utilization(node, pods):
    """
    Compute GPU utilization for a node.
    """
    # 1. Node total capacity
    total_core = sum(card["core"] for card in node["cards"])
    total_mem = sum(card["memory"] for card in node["cards"])
    
    # 2. Allocated resources (after rounding)
    allocated_core = sum(pod["allocated_core"] for pod in pods)
    allocated_mem = sum(pod["allocated_memory"] for pod in pods)
    
    # 3. Actually used resources (before rounding)
    utilized_core = sum(pod["raw_core"] for pod in pods)
    utilized_mem = sum(pod["raw_memory"] for pod in pods)
    
    # 4. Ratios
    return {
        "core_allocation_rate": allocated_core / total_core * 100,
        "core_utilization_rate": utilized_core / total_core * 100,
        "memory_allocation_rate": allocated_mem / total_mem * 100,
        "memory_utilization_rate": utilized_mem / total_mem * 100,
    }
```

### 5.2 Per-card utilization

```python
def compute_card_utilization(card_list, pod_card_shares):
    """
    Compute utilization for each GPU card.
    """
    card_usage = {card["id"]: {"core": 0, "memory": 0} for card in card_list}
    
    # Sum usage from each Pod on each card
    for pod_name, cards in pod_card_shares.items():
        for card in cards:
            card_id = card["card_id"]
            card_usage[card_id]["core"] += card["core"]
            card_usage[card_id]["memory"] += card["memory"]
    
    # Ratios
    for card in card_list:
        card_id = card["id"]
        usage = card_usage[card_id]
        card["core_utilization"] = usage["core"] / card["capacity"] * 100
        card["memory_utilization"] = usage["memory"] / card["memory"] * 100
    
    return card_list
```

---

## 6. Report output

### 6.1 flexnpu_utilization.txt

```
================================================================================
FlexNPU utilization report
================================================================================

Node: node-1
Total cards: 4

Node summary:
  Compute — allocated: 140.0% / total: 400.0% / allocation rate: 35.00%
  Compute — actual demand: 125.0% / total: 400.0%  / utilization: 31.25%
  Memory — allocated: 48.0 / total: 256.0 / allocation rate: 18.75%
  Memory — actual demand: 48.0 / total: 256.0 / utilization: 18.75%

Per-card detail:
  Card 0: core allocation rate 50.00% / utilization 50.00% / memory allocation rate 24.00%
  Card 1: core allocation rate 30.00% / utilization 30.00% / memory allocation rate 12.00%
  Card 2: core allocation rate 40.00% / utilization 40.00% / memory allocation rate 16.00%
  Card 3: core allocation rate 20.00% / utilization 20.00% / memory allocation rate 12.00%

Pod placement detail:
  pod-A (job-1) -> Card 0 (30% core, 16 memory) + Card 1 (30% core, 16 memory)
  pod-B (job-2) -> Card 2 (40% core, 32 memory)
  pod-C (job-3) -> Card 3 (20% core, 8 memory) + Card 0 (20% core, 8 memory)

================================================================================
Totals:
  Nodes: 1
  Cards: 4
  Pods: 3
  Average core utilization: 35.00%
  Average core allocation rate: 35.00%
================================================================================
```

### 6.2 CSV reports

**Node_desc.csv:**

```csv
node_name,flexnpu_core_allocated,flexnpu_core_total,flexnpu_core_allocation_rate,flexnpu_core_utilized,flexnpu_core_utilization_rate,flexnpu_memory_allocated,flexnpu_memory_total,flexnpu_memory_allocation_rate,flexnpu_memory_utilized,flexnpu_memory_utilization_rate
node-1,140.0,400.0,35.0,125.0,31.25,48.0,256.0,18.75,48.0,18.75
```

**npu_chip.csv:**

```csv
node_name,card_id,flexnpu_core_capacity,flexnpu_core_allocated,flexnpu_core_allocation_rate,flexnpu_core_utilized,flexnpu_core_utilization_rate,flexnpu_memory_capacity,flexnpu_memory_allocated,flexnpu_memory_allocation_rate,flexnpu_memory_utilized,flexnpu_memory_utilization_rate
node-1,0,100.0,50.0,50.0,50.0,50.0,64.0,24.0,37.5,24.0,37.5
node-1,1,100.0,30.0,30.0,30.0,30.0,64.0,16.0,25.0,16.0,25.0
...
```

---

## 7. Usage recommendations

### 7.1 Choosing granularity

| Granularity | When to use | Pros and cons |
|-------------|-------------|---------------|
| 0% (no rounding) | Theoretical study | No waste, unrealistic |
| 5% | Fine-grained virtualization | Less waste, more scheduling complexity |
| 10% | Balanced default | Recommended default |
| 25% | Coarse virtualization | Simple, more waste |
| 50% | Near whole-card | Close to traditional GPUs |

### 7.2 Resource planning

**Suggested node configuration:**

```yaml
nodes:
  - name: gpu-node
    annotations:
      # 8 cards, each 100% compute and 32GB VRAM
      volcano.sh/flexnpu-core.percentage-list: "[100,100,100,100,100,100,100,100]"
      volcano.sh/flexnpu-memory.128mi-list: "[256,256,256,256,256,256,256,256]"
```

**Suggested job configuration:**

```yaml
jobs:
  - spec:
      npuGranularityPercent: 10  # 10% granularity
      tasks:
        - replicas: 4
          template:
            metadata:
              annotations:
                volcano.sh/flexnpu-num: "2"  # Use 2 cards
            spec:
              containers:
                - resources:
                    requests:
                      volcano.sh/flexnpu-core.percentage: "35"  # 35% per card
                      volcano.sh/flexnpu-memory.128mi: "64"     # 8GB per card
```

---

## 8. Summary

FlexNPU is a central idea in VolcanoSimulator:

```
┌────────────────────────────────────────────────────────────────┐
│                    FlexNPU resource model                       │
├────────────────────────────────────────────────────────────────┤
│  1. Splittable: one physical GPU can be virtualized into       │
│     multiple slices                                             │
│  2. Dual-track stats: separate “raw demand” from “actual         │
│     allocation”                                                 │
│  3. Granularity: configurable minimum allocation step           │
│  4. Round-robin estimate: approximate Pod → GPU card mapping    │
│  5. Rich reports: node- and card-level utilization and          │
│     allocation rates                                            │
└────────────────────────────────────────────────────────────────┘
```

**With FlexNPU you can:**
- Simulate GPU virtualization scenarios
- Analyze allocation efficiency
- Find where resources are wasted
- Tune scheduling policies

---

Ready for data flow and the interaction protocol? Continue with [`05-data-flow-and-http.md`](05-data-flow-and-http.md).
