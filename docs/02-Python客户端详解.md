# 02-Python客户端详解：如何控制仿真并生成报表

> 本文档详细介绍 Python 客户端的各个模块，包括配置加载、HTTP 通信、资源分析和报表生成。

---

## 一、整体结构

```
Submit_volcano_workloads/
├── SimRun.py                    # 入口：主流程控制
├── common/
│   └── utils/
│       └── json_http_client.py  # HTTP 通信封装
├── input_config/                # 配置系统
│   ├── __init__.py
│   ├── input_config_loader.py   # YAML 加载与转换
│   ├── flexnpu_util_report.py   # FlexNPU 资源分析
│   ├── output_csv_reports.py    # CSV 报表生成
│   ├── README.md                # 配置说明
│   ├── cluster/                 # 集群配置示例
│   ├── workload/                # 负载配置示例
│   └── plugins/                 # 插件配置示例
└── figures/                     # 作图脚本（历史遗留）
```

---

## 二、SimRun.py：指挥官

### 2.1 职责定位

**SimRun.py 是整个仿真的"指挥官"**，它负责：
1. 读取配置文件
2. 指挥 Go 仿真器初始化、调度、返回结果
3. 调用报表模块生成输出

### 2.2 主流程代码走读

```python
def main():
    # 1. 配置路径（你可以修改这里切换配置）
    sim_base_url = 'http://localhost:8006'
    cluster_path = 'input_config/cluster/cluster_1.yaml'
    workload_path = 'input_config/workload/workload_1.yaml'
    plugins_path = 'input_config/plugins/plugins.yaml'

    # 2. 加载配置（调用 input_config_loader）
    nodes_yaml = load_cluster_for_simulator(cluster_path)
    workload_yaml = load_workload_for_simulator(workload_path)
    npu_granularity = workload_npu_granularity_percent_from_file(workload_path)
    scheduler_conf_yaml, result_root = load_plugins_for_simulator(plugins_path)

    # 3. 创建结果目录
    os.makedirs(result_root, exist_ok=True)

    # 4. 执行仿真
    reset(sim_base_url, nodes_yaml, workload_yaml)  # 初始化
    time.sleep(1)
    step(sim_base_url, scheduler_conf_yaml, result_root, npu_granularity)  # 调度
```

### 2.3 reset 函数详解

**作用：** 告诉 Go 仿真器："重新开始一场新的仿真"

```python
def reset(sim_base_url, nodes_yaml, workload_yaml):
    client = JsonHttpClient(sim_base_url)
    
    # 发送 POST /reset 请求
    dicData = client.get_json('/reset', json={
        'period': "-1",           # -1 表示只执行一次调度配置
        'nodes': nodes_yaml,      # 集群配置的 YAML 字符串
        'workload': workload_yaml, # 负载配置的 YAML 字符串
    })
    
    if str(dicData) == "0":
        print("还有任务在运行，无法重置")
    else:
        print("仿真环境初始化完成")
```

**注意：**
- `nodes_yaml` 和 `workload_yaml` 是 **字符串**，不是文件路径
- 返回 "0" 表示重置失败（有任务在运行）

### 2.4 step 函数详解

**作用：** 推进一轮调度并获取结果

```python
def step(sim_base_url, scheduler_conf_yaml, pods_result_url, npu_granularity):
    client = JsonHttpClient(sim_base_url)
    
    # 1. 发送调度配置
    client.get_json('/step', json={'conf': scheduler_conf_yaml})
    
    # 2. 轮询结果（因为调度是异步的）
    while True:
        time.sleep(0.2)  # 等待 200ms
        resultdata = client.get_json('/stepResult', json={'none': ""})
        
        if str(resultdata) == '0':
            continue  # 还没完成，继续等
        else:
            # 拿到结果了！
            process_result(resultdata, pods_result_url, npu_granularity)
            break
```

**轮询机制图解：**

```
Python          Go 仿真器
  │                 │
  │── POST /step ──→│  "收到配置，标记需要调度"
  │                 │
  │←── 返回 "1" ────│
  │                 │
  │  (Go 主循环下一周期执行调度)
  │                 │
  │── GET /stepResult ──→│ "调度完成了吗？"
  │                      │
  │←── 返回 "0" ─────────│ "还没，再等会儿"
  │                      │
  │── GET /stepResult ──→│ "调度完成了吗？"
  │                      │
  │←── 返回 JSON ────────│ "完成了，给你结果"
```

### 2.5 结果处理

```python
def process_result(resultdata, pods_result_url, npu_granularity):
    # 1. 注入粒度参数（供报表使用）
    resultdata["npuGranularityPercent"] = float(npu_granularity or 0.0)
    
    # 2. 生成 tasksSUM.csv（任务清单）
    write_tasks_csv(resultdata, pods_result_url)
    
    # 3. 生成 pod_phase_count.txt（统计）
    write_phase_summary(resultdata, pods_result_url)
    
    # 4. 生成 flexnpu_utilization.txt（GPU 利用率）
    flexnpu_txt = print_flexnpu_utilization(resultdata)
    write_file(pods_result_url, "flexnpu_utilization.txt", flexnpu_txt)
    
    # 5. 生成四类 CSV 报表
    write_output_config_csvs(resultdata, pods_result_url)
```

---

## 三、JsonHttpClient：通信员

### 3.1 设计目的

封装 HTTP 请求，提供：
- 自动 JSON 序列化/反序列化
- 失败重试机制
- 统一的错误处理

### 3.2 核心代码

```python
class JsonHttpClient:
    def __init__(self, base_url):
        self.base_url = base_url
    
    def get_json(self, path, json=None, max_retries=10):
        """
        发送 HTTP 请求，自动处理 JSON
        
        参数:
            path: API 路径，如 '/reset'
            json: 请求体（Python 字典，会自动转 JSON）
            max_retries: 最大重试次数
        """
        url = self.base_url + path
        
        for attempt in range(max_retries):
            try:
                if json is not None:
                    # POST 请求，带 JSON 体
                    response = requests.post(url, json=json, timeout=10)
                else:
                    # GET 请求
                    response = requests.get(url, timeout=10)
                
                response.raise_for_status()
                
                # 尝试解析 JSON，如果失败返回原始文本
                try:
                    return response.json()
                except ValueError:
                    return response.text
                    
            except requests.RequestException as e:
                if attempt == max_retries - 1:
                    raise
                time.sleep(0.1)
```

### 3.3 使用示例

```python
client = JsonHttpClient('http://localhost:8006')

# GET 请求
result = client.get_json('/stepResultAnyway')

# POST 请求，带 JSON 体
result = client.get_json('/reset', json={
    'period': '-1',
    'nodes': 'yaml content...',
    'workload': 'yaml content...'
})
```

---

## 四、input_config_loader：翻译官

### 4.1 职责

把用户友好的 YAML 配置文件，翻译成仿真器能理解的格式。

### 4.2 核心功能

#### 4.2.1 集群配置转换

**用户写的（友好）：**
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

**仿真器需要的（标准）：**
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

**转换函数：**
```python
def cluster_input_to_simulator_yaml(doc):
    """把 input_config 风格的 cluster 转为仿真器格式"""
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

#### 4.2.2 负载配置转换

**关键特性：粒度取整**

```python
def workload_input_to_simulator_yaml(doc):
    """
    转换负载配置，关键功能：
    1. 对 flexnpu_core 按粒度向上取整
    2. 记录原始值到注解
    3. 规范 task 结构
    """
    granularity = float(doc.get("spec", {}).get("npuGranularityPercent", 0))
    
    for job in doc.get("jobs", []):
        for task in job.get("spec", {}).get("tasks", []):
            # 对容器的 flexnpu_core 请求做取整
            for container in task["template"]["spec"]["containers"]:
                resources = container.get("resources", {})
                
                # 取整并记录原始值
                raw_value = resources["requests"]["volcano.sh/flexnpu-core.percentage"]
                rounded_value = ceil_to_step(float(raw_value), granularity)
                
                # 写回取整后的值
                resources["requests"]["volcano.sh/flexnpu-core.percentage"] = str(rounded_value)
                
                # 记录原始值到注解（供报表使用）
                annotations = task["template"].setdefault("metadata", {}).setdefault("annotations", {})
                annotations["volcano.sh/flexnpu-core.percentage-raw-by-container"] = json.dumps({
                    container["name"]: float(raw_value)
                })
    
    return yaml.safe_dump({"jobs": jobs})
```

**为什么需要取整？**

假设粒度是 10%，一个任务请求 35% 的算力：
- 原始值：35%
- 取整后：40%（向上取整到 10 的倍数）

这样调度器可以按 40% 分配，但报表可以显示 "实际需求 35%，分配了 40%"，从而计算利用率。

#### 4.2.3 插件配置解析

```python
def load_plugins_for_simulator(path):
    """
    加载插件配置，返回：
    1. 调度器配置 YAML 字符串（用于 /step）
    2. 结果输出目录路径
    """
    doc = yaml.safe_load(open(path))
    
    # 提取调度器配置
    scheduler = doc["scheduler"]
    conf_str = yaml.safe_dump(scheduler)
    
    # 解析输出目录（支持 {date} 占位符）
    out_dir = doc.get("output", {}).get("outDir", "./result/{date}")
    out_dir = out_dir.replace("{date}", datetime.now().strftime("%Y-%m-%d-%H-%M-%S"))
    
    return conf_str, out_dir
```

---

## 五、flexnpu_util_report：分析师

### 5.1 职责

分析 GPU 资源的使用情况，生成详细的利用率报告。

### 5.2 核心概念：双轨统计

```
┌─────────────────────────────────────────────────────────────────┐
│                    FlexNPU 资源统计                             │
├───────────────────────────┬─────────────────────────────────────┤
│      利用率 Utilization    │         分配率 Allocation           │
├───────────────────────────┼─────────────────────────────────────┤
│  基于：原始需求（取整前）   │   基于：实际分配（取整后）           │
│                           │                                     │
│  任务请求 35%，按 35% 计算  │   任务请求 35%，实际分配 40%         │
│                           │   按 40% 计算                       │
├───────────────────────────┼─────────────────────────────────────┤
│  意义：真实的资源需求        │   意义：实际占用的资源               │
│                           │                                     │
│  利用率 = 需求 / 容量       │   分配率 = 分配 / 容量               │
└───────────────────────────┴─────────────────────────────────────┘
```

**为什么要区分？**
- 如果只看分配率，会认为资源利用率很高（都分配出去了）
- 但实际上可能因为粒度取整导致浪费（请求 35% 分配 40%，浪费 5%）
- 通过对比利用率（35%）和分配率（40%），可以发现这种浪费

### 5.3 核心算法：Pod → GPU 卡分配估算

```python
def estimate_card_usage(node, pods, granularity):
    """
    估算 Pod 在 GPU 卡上的分配情况
    
    策略：轮询分配
    - 节点有 N 张卡
    - 每个 Pod 需要 M 张卡
    - 按顺序轮流分配给各张卡
    """
    # 1. 解析节点的卡列表
    card_list = parse_card_list(node["annotations"])
    # 如：[("card0", 100, 64), ("card1", 100, 64), ...]
    
    # 2. 对每个 Pod 分配卡
    pod_card_shares = {}
    for pod in pods:
        # Pod 需要多少张卡
        num_cards = int(pod["annotations"].get("volcano.sh/flexnpu-num", 1))
        
        # 每张卡的资源需求
        core_per_card = pod["core_request"] / num_cards
        mem_per_card = pod["mem_request"] / num_cards
        
        # 轮询分配
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

### 5.4 报表输出示例

```
FlexNPU 利用率报告
==================

节点: node-1
总卡数: 4

按节点汇总：
  算力 - 已用: 140.0% / 总计: 400.0% / 分配率: 35.00%
  算力 - 实际需求: 125.0% / 总计: 400.0% / 利用率: 31.25%
  显存 - 已用: 48.0 / 总计: 256.0 / 分配率: 18.75%

按卡详情：
  Card 0: 核心分配率 40.00% / 利用率 35.00% / 显存分配率 16.00%
  Card 1: 核心分配率 35.00% / 利用率 30.00% / 显存分配率 14.00%
  Card 2: 核心分配率 35.00% / 利用率 30.00% / 显存分配率 12.00%
  Card 3: 核心分配率 30.00% / 利用率 30.00% / 显存分配率 10.00%

Pod 分配详情：
  pod-A -> Card 0 (40% 核心, 16 显存)
  pod-B -> Card 1 (35% 核心, 14 显存)
  ...
```

---

## 六、output_csv_reports：文书

### 6.1 职责

把仿真结果转换成结构化的 CSV 文件，方便后续分析（用 Excel、Pandas 等工具）。

### 6.2 生成的四个 CSV 文件

| 文件 | 内容 | 用途 |
|------|------|------|
| `Node_desc.csv` | 每个节点的资源汇总 | 看机器负载均衡 |
| `POD_desc.csv` | 每个 Pod 的详细信息 | 看任务分配详情 |
| `npu_chip.csv` | 每张 GPU 卡的利用率 | 看卡级资源使用 |
| `summary.csv` | 整体统计 | 快速了解实验结果 |

### 6.3 Node_desc.csv 示例

```csv
node_name,flexnpu_core_allocated,flexnpu_core_total,flexnpu_core_allocation_rate,flexnpu_core_utilized,flexnpu_core_utilization_rate
node-1,140.0,400.0,35.0,125.0,31.25
node-2,200.0,400.0,50.0,180.0,45.0
```

**字段说明：**
- `*_allocated`：实际分配的资源（取整后）
- `*_utilized`：实际需要的资源（取整前）
- `*_rate`：比率（0-100 的数字，不带 %）

### 6.4 POD_desc.csv 示例

```csv
pod_name,job_name,phase,node_name,flexnpu_core_request,flexnpu_memory_request,submit_time,start_time,card_used_quantity
pod-A,job-1,Running,node-1,40.0,16,2026-04-07T10:00:00,2026-04-07T10:00:05,"{\"card0\": {\"core\": 40.0, \"memory\": 16}}"
```

---

## 七、总结

Python 客户端的核心设计思想：

```
配置加载器（翻译）
      ↓
HTTP 客户端（通信）
      ↓
结果处理器（分析）
      ↓
报表生成器（输出）
```

**各模块独立，通过数据流连接：**
- 修改配置格式？只改 `input_config_loader`
- 修改通信方式？只改 `json_http_client`
- 修改报表内容？只改 `output_csv_reports`
- 修改资源模型？改 `flexnpu_util_report` 和 Go 端

这种 **"单一职责 + 数据驱动"** 的设计使得代码易于理解和维护。

---

准备好深入了解 Go 仿真器了吗？请继续阅读 `03-Go仿真器.md`！
