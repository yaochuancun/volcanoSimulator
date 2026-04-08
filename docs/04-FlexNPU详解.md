# 04-FlexNPU详解：GPU资源调度模型

> 本文档深入讲解 FlexNPU 资源模型，这是 VolcanoSimulator 的核心特性之一，用于模拟可切分的 GPU 资源。

---

## 一、为什么需要 FlexNPU？

### 1.1 传统 GPU 的问题

**现实中 GPU 的两种使用方式：**

```
方式 1：整卡分配（独占式）
┌─────────────────┐
│     GPU 0       │  ← 任务 A 独占 100% 算力
│    100% 算力     │
│     32GB 显存    │
└─────────────────┘
问题：任务只需要 30% 算力，剩下的 70% 浪费了！

方式 2：虚拟化切片（共享式）
┌─────────────────┐
│  GPU 0          │
├────────┬────────┤
│ 任务 A  │ 任务 B │
│  30%   │  50%   │
│  10GB  │  20GB  │
└────────┴────────┘
优势：资源按需分配，提高利用率
```

**FlexNPU 就是模拟这种 GPU 虚拟化技术。**

### 1.2 FlexNPU 的核心能力

1. **算力切分**：一张 100% 算力的卡可以分成多份
2. **显存切分**：显存也可以按需分配
3. **多卡支持**：一个任务可以用多张卡的资源
4. **粒度控制**：设置最小分配粒度（如 10% 一档）

---

## 二、资源模型

### 2.1 三种资源键

```go
// 算力资源（百分比）
volcano.sh/flexnpu-core.percentage

// 显存资源（单位：128Mi）
volcano.sh/flexnpu-memory.128mi

// 卡数量（一个任务用几张卡）
volcano.sh/flexnpu-num
```

**为什么显存单位是 128Mi？**
- 方便用整数表示，避免浮点数精度问题
- 128Mi 是 2 的幂次，便于计算
- 实际显存 = 数值 × 128Mi

### 2.2 节点级资源定义

**节点（机器）需要声明有哪些 GPU 卡：**

```yaml
apiVersion: v1
kind: Node
metadata:
  name: node-1
  annotations:
    # 这台机器有 4 张卡，每张卡的算力和显存
    volcano.sh/flexnpu-core.percentage-list: "[100, 100, 100, 100]"
    volcano.sh/flexnpu-memory.128mi-list: "[64, 64, 64, 64]"
    # 含义：4 张卡，每张 100% 算力、8GB 显存（64×128Mi=8GB）
```

**列表格式：**
- 索引 0 → Card 0
- 索引 1 → Card 1
- ...

### 2.3 任务级资源请求

**任务（Pod）需要声明需要多少 GPU 资源：**

```yaml
apiVersion: v1
kind: Pod
metadata:
  annotations:
    volcano.sh/flexnpu-num: "2"  # 需要 2 张卡
spec:
  containers:
    - name: train
      resources:
        requests:
          volcano.sh/flexnpu-core.percentage: "35"   # 每张卡 35% 算力
          volcano.sh/flexnpu-memory.128mi: "16"      # 每张卡 2GB 显存
```

**这个任务的资源需求：**
- 用 2 张卡
- 每张卡 35% 算力
- 每张卡 2GB 显存
- 总共：70% 算力、4GB 显存

---

## 三、粒度取整机制

### 3.1 为什么需要取整？

**场景：** 粒度设为 10%，任务请求 35% 算力

```
请求：35%
粒度：10%
取整后：40%（向上取整到 10 的倍数）

原因：调度器按粒度分配，35 不是 10 的倍数，无法精确分配
结果：实际分配 40%，多分配了 5%
```

### 3.2 双轨统计

为了追踪这种"过度分配"，系统采用 **双轨统计**：

```
┌──────────────────────────────────────────────────────────────┐
│                     资源统计双轨制                            │
├────────────────────────┬─────────────────────────────────────┤
│      利用率             │           分配率                     │
├────────────────────────┼─────────────────────────────────────┤
│ 基于原始需求（35%）      │  基于实际分配（40%）                 │
│                        │                                     │
│ 计算：35% / 总容量       │  计算：40% / 总容量                  │
│                        │                                     │
│ 意义：真实的资源需求       │  意义：实际占用的资源                │
├────────────────────────┼─────────────────────────────────────┤
│ 用于：分析资源是否真的      │  用于：了解资源分配情况              │
│      被充分利用          │                                     │
└────────────────────────┴─────────────────────────────────────┘
```

**对比示例：**

| 指标 | 计算 | 含义 |
|------|------|------|
| 利用率 | 35% / 100% = 35% | 任务实际只需要 35% |
| 分配率 | 40% / 100% = 40% | 但因为粒度，分配了 40% |
| 浪费 | 40% - 35% = 5% | 5% 的算力被浪费 |

### 3.3 代码实现

**input_config_loader.py 中的取整逻辑：**

```python
def _ceil_to_step(value: float, step: float) -> float:
    """向上取整到 step 的整数倍"""
    if step <= 0:
        return value
    return math.ceil(value / step) * step


def _round_resource_map(resources: Dict, granularity_percent: float) -> None:
    """对 flexnpu_core 做粒度取整"""
    key = "volcano.sh/flexnpu-core.percentage"
    if key not in resources:
        return
    
    raw_value = float(resources[key])
    rounded_value = _ceil_to_step(raw_value, granularity_percent)
    
    # 写回取整后的值
    resources[key] = str(int(rounded_value))
```

**同时记录原始值：**

```python
def _normalize_task_templates(tasks: List[Dict], granularity: float) -> None:
    """规范 task 结构，同时记录原始 core 值"""
    raw_by_container: Dict[str, float] = {}
    
    for container in pod_spec.get("containers", []):
        cname = container.get("name", "__default__")
        res = container.get("resources", {})
        
        # 取整前记录原始值
        raw_value = float(res["requests"]["volcano.sh/flexnpu-core.percentage"])
        raw_by_container[cname] = raw_value
        
        # 取整后写回
        rounded = _ceil_to_step(raw_value, granularity)
        res["requests"]["volcano.sh/flexnpu-core.percentage"] = str(rounded)
    
    # 将原始值写入注解，供报表使用
    meta = task.setdefault("metadata", {})
    ann = meta.setdefault("annotations", {})
    ann["volcano.sh/flexnpu-core.percentage-raw-by-container"] = json.dumps(raw_by_container)
```

---

## 四、Pod → GPU 卡分配估算

### 4.1 问题定义

调度完成后，我们知道：
- 每个 Pod 被分配到哪个节点
- 每个 Pod 请求了多少 GPU 资源

但我们不知道：
- Pod 具体用了节点上的哪几张卡？
- 每张卡上跑了哪些 Pod？

**为什么要估算？**
- 真实的 K8s 中，GPU 分配是 kubelet 做的，调度器不知道
- 但为了分析利用率，我们需要知道每张卡的使用情况
- 所以用算法估算一个"合理"的分配方案

### 4.2 轮询分配算法

```python
def estimate_card_usage(node, pods, granularity):
    """
    轮询分配算法
    
    策略：
    1. 节点有 N 张卡
    2. 每个 Pod 需要 M 张卡
    3. 按 Pod 顺序，轮流分配给各张卡
    
    示例：
    节点：4 张卡 (0, 1, 2, 3)
    Pod A：需要 2 张卡 → 分配 card 0, 1
    Pod B：需要 1 张卡 → 分配 card 2
    Pod C：需要 2 张卡 → 分配 card 3, 0
    """
    
    # 1. 解析节点的卡列表
    card_list = parse_card_list(node["annotations"])
    # [("card0", 100, 64), ("card1", 100, 64), ...]
    
    # 2. 按顺序为每个 Pod 分配卡
    pod_card_shares = {}
    next_card_index = 0
    
    for pod in pods:
        # Pod 需要多少张卡
        num_cards = int(pod["annotations"].get("volcano.sh/flexnpu-num", 1))
        
        # 计算每张卡的资源需求
        total_core = pod["core_request"]
        total_mem = pod["mem_request"]
        core_per_card = total_core / num_cards
        mem_per_card = total_mem / num_cards
        
        # 轮询分配
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

### 4.3 分配示例

```
节点 node-1：4 张卡，每张 100% 算力、64 单位显存

待分配 Pod（按调度顺序）：
  Pod A: 2 张卡，每张 30% 算力、16 显存
  Pod B: 1 张卡，每张 40% 算力、32 显存
  Pod C: 2 张卡，每张 20% 算力、8 显存

轮询分配过程：
  Pod A → Card 0 (30%, 16) + Card 1 (30%, 16)
  Pod B → Card 2 (40%, 32)
  Pod C → Card 3 (20%, 8) + Card 0 (20%, 8)

最终结果：
  Card 0: Pod A (30%) + Pod C (20%) = 50% 利用率
  Card 1: Pod A (30%) = 30% 利用率
  Card 2: Pod B (40%) = 40% 利用率
  Card 3: Pod C (20%) = 20% 利用率
```

---

## 五、利用率计算

### 5.1 节点级利用率

```python
def compute_node_utilization(node, pods):
    """
    计算节点的 GPU 利用率
    """
    # 1. 获取节点总容量
    total_core = sum(card["core"] for card in node["cards"])
    total_mem = sum(card["memory"] for card in node["cards"])
    
    # 2. 计算已分配资源（取整后）
    allocated_core = sum(pod["allocated_core"] for pod in pods)
    allocated_mem = sum(pod["allocated_memory"] for pod in pods)
    
    # 3. 计算实际使用资源（取整前）
    utilized_core = sum(pod["raw_core"] for pod in pods)
    utilized_mem = sum(pod["raw_memory"] for pod in pods)
    
    # 4. 计算比率
    return {
        "core_allocation_rate": allocated_core / total_core * 100,
        "core_utilization_rate": utilized_core / total_core * 100,
        "memory_allocation_rate": allocated_mem / total_mem * 100,
        "memory_utilization_rate": utilized_mem / total_mem * 100,
    }
```

### 5.2 卡级利用率

```python
def compute_card_utilization(card_list, pod_card_shares):
    """
    计算每张 GPU 卡的利用率
    """
    card_usage = {card["id"]: {"core": 0, "memory": 0} for card in card_list}
    
    # 累加各 Pod 在各卡上的使用
    for pod_name, cards in pod_card_shares.items():
        for card in cards:
            card_id = card["card_id"]
            card_usage[card_id]["core"] += card["core"]
            card_usage[card_id]["memory"] += card["memory"]
    
    # 计算比率
    for card in card_list:
        card_id = card["id"]
        usage = card_usage[card_id]
        card["core_utilization"] = usage["core"] / card["capacity"] * 100
        card["memory_utilization"] = usage["memory"] / card["memory"] * 100
    
    return card_list
```

---

## 六、报表输出

### 6.1 flexnpu_utilization.txt

```
================================================================================
FlexNPU 利用率报告
================================================================================

节点: node-1
总卡数: 4

按节点汇总：
  算力 - 已分配: 140.0% / 总计: 400.0% / 分配率: 35.00%
  算力 - 实际需求: 125.0% / 总计: 400.0%  / 利用率: 31.25%
  显存 - 已分配: 48.0 / 总计: 256.0 / 分配率: 18.75%
  显存 - 实际需求: 48.0 / 总计: 256.0 / 利用率: 18.75%

按卡详情：
  Card 0: 核心分配率 50.00% / 利用率 50.00% / 显存分配率 24.00%
  Card 1: 核心分配率 30.00% / 利用率 30.00% / 显存分配率 12.00%
  Card 2: 核心分配率 40.00% / 利用率 40.00% / 显存分配率 16.00%
  Card 3: 核心分配率 20.00% / 利用率 20.00% / 显存分配率 12.00%

Pod 分配详情：
  pod-A (job-1) -> Card 0 (30% 核心, 16 显存) + Card 1 (30% 核心, 16 显存)
  pod-B (job-2) -> Card 2 (40% 核心, 32 显存)
  pod-C (job-3) -> Card 3 (20% 核心, 8 显存) + Card 0 (20% 核心, 8 显存)

================================================================================
总计：
  总节点数: 1
  总卡数: 4
  总 Pod 数: 3
  平均核心利用率: 35.00%
  平均核心分配率: 35.00%
================================================================================
```

### 6.2 CSV 报表

**Node_desc.csv：**
```csv
node_name,flexnpu_core_allocated,flexnpu_core_total,flexnpu_core_allocation_rate,flexnpu_core_utilized,flexnpu_core_utilization_rate,flexnpu_memory_allocated,flexnpu_memory_total,flexnpu_memory_allocation_rate,flexnpu_memory_utilized,flexnpu_memory_utilization_rate
node-1,140.0,400.0,35.0,125.0,31.25,48.0,256.0,18.75,48.0,18.75
```

**npu_chip.csv：**
```csv
node_name,card_id,flexnpu_core_capacity,flexnpu_core_allocated,flexnpu_core_allocation_rate,flexnpu_core_utilized,flexnpu_core_utilization_rate,flexnpu_memory_capacity,flexnpu_memory_allocated,flexnpu_memory_allocation_rate,flexnpu_memory_utilized,flexnpu_memory_utilization_rate
node-1,0,100.0,50.0,50.0,50.0,50.0,64.0,24.0,37.5,24.0,37.5
node-1,1,100.0,30.0,30.0,30.0,30.0,64.0,16.0,25.0,16.0,25.0
...
```

---

## 七、使用建议

### 7.1 粒度选择

| 粒度 | 适用场景 | 优缺点 |
|------|----------|--------|
| 0%（不取整） | 理论研究 | 无浪费，但不真实 |
| 5% | 高精度虚拟化 | 浪费少，但调度复杂 |
| 10% | 平衡选择 | 推荐默认值 |
| 25% | 粗粒度虚拟化 | 简单，但浪费多 |
| 50% | 近似整卡 | 接近传统 GPU |

### 7.2 资源规划

**建议的节点配置：**
```yaml
nodes:
  - name: gpu-node
    annotations:
      # 8 张卡，每张 100% 算力、32GB 显存
      volcano.sh/flexnpu-core.percentage-list: "[100,100,100,100,100,100,100,100]"
      volcano.sh/flexnpu-memory.128mi-list: "[256,256,256,256,256,256,256,256]"
```

**建议的任务配置：**
```yaml
jobs:
  - spec:
      npuGranularityPercent: 10  # 10% 粒度
      tasks:
        - replicas: 4
          template:
            metadata:
              annotations:
                volcano.sh/flexnpu-num: "2"  # 用 2 张卡
            spec:
              containers:
                - resources:
                    requests:
                      volcano.sh/flexnpu-core.percentage: "35"  # 每张 35%
                      volcano.sh/flexnpu-memory.128mi: "64"     # 每张 8GB
```

---

## 八、总结

FlexNPU 是 VolcanoSimulator 的核心创新点：

```
┌────────────────────────────────────────────────────────────────┐
│                      FlexNPU 资源模型                           │
├────────────────────────────────────────────────────────────────┤
│  1. 可切分：一张物理 GPU 可以虚拟化成多个切片                    │
│  2. 双轨统计：区分 "原始需求" 和 "实际分配"                     │
│  3. 粒度控制：支持设置最小分配粒度                              │
│  4. 轮询估算：估算 Pod 到 GPU 卡的映射                          │
│  5. 详细报表：节点级、卡级的利用率和分配率                       │
└────────────────────────────────────────────────────────────────┘
```

**使用 FlexNPU 可以：**
- 模拟 GPU 虚拟化场景
- 分析资源分配效率
- 发现资源浪费点
- 优化调度策略

---

准备好了解数据流和交互协议了吗？请继续阅读 `05-数据流与交互.md`！
