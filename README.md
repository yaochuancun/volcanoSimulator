# volcanoSimulator

本仓库包含 **Volcano 调度仿真器（Go）** 与 **仿真负载提交客户端（Python）**：用 YAML 描述集群、作业与调度插件，通过 HTTP 驱动仿真，并落盘任务摘要、FlexNPU 利用率及标准 CSV 报表。

许可与第三方说明见根目录 [**LICENSE**](LICENSE)、[**许可说明.md**](许可说明.md)。输入文件细分说明见 [**Submit_volcano_workloads/input_config/README.md**](Submit_volcano_workloads/input_config/README.md)。

---

## 一、主要功能

- **配置驱动仿真**（配置位于 `Submit_volcano_workloads/input_config/`）  
  - **集群**：`cluster/*.yaml` 描述节点、FlexNPU 注解（如 `flexnpu-core.percentage-list`、`flexnpu-memory.128mi-list`、`topologies`）及可调度资源。  
  - **负载**：`workload/*.yaml` 描述 Volcano Job 列表，支持 `npuGranularityPercent`（对 flexnpu request/limit 按粒度上取整）、`volcano.sh/flexnpu-num` 等。  
  - **调度**：`plugins/*.yaml` 中的 `scheduler` 块（actions、tiers、插件参数）会作为调度器配置下发；`output.outDir` 中的 `{date}` 会展开为时间戳，用于结果根目录。

- **与仿真器交互**  
  - `reset`：上传转换后的节点 YAML 与作业 YAML，初始化集群与 Job 队列。  
  - `step`：上传调度器配置，推进调度回合。  
  - `stepResult`：拉取当前 `Jobs`、`Nodes`、Pod 列表及仿真时钟等，用于统计与报表。

- **结果产物**（默认在 `plugins.yaml` 解析出的 `Submit_volcano_workloads/result/.../tasks/<时间戳>/` 等路径下）  
  - `tasksSUM.csv` / `tasksSUM.md`、`pod_phase_count.txt`  
  - `flexnpu_utilization.txt`：节点级 FlexNPU 利用率、逐卡估算、Pod→卡映射说明  
  - `output_config/`：`Node_desc.csv`、`POD_desc.csv`、`npu_chip.csv`、`summary.csv`  
  - `jobs/` 目录下 JCT 相关占位输出（`.csv` / `.md`）

- **Python 辅助模块**（`Submit_volcano_workloads/input_config/` 包）  
  - `input_config_loader`：将上述 YAML 转为仿真器期望的 `cluster` / `jobs` / scheduler conf 格式。  
  - `flexnpu_util_report`：基于 `stepResult` 解析 FlexNPU 标量资源与注解。  
  - `output_csv_reports`：生成四类 CSV 报表。

---

## 二、架构

```
┌─────────────────────────────────────────────────────────────┐
│  Submit_volcano_workloads/ (Python)                         │
│  SimRun.py ──► JsonHttpClient ──► HTTP JSON                  │
│       │              ▲                                       │
│       ▼              │                                       │
│  input_config/       │     stepResult（Jobs, Nodes, Pods…）   │
│  · loader / flexnpu  │                                       │
│  · output_csv        │                                       │
└──────────────────────┼───────────────────────────────────────┘
                       │  :8006
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Volcano_simulator/cmd/sim (Go)                             │
│  /reset  /step  /stepResult — 内嵌 Volcano 调度框架仿真循环   │
└─────────────────────────────────────────────────────────────┘
```

- **职责划分**  
  - **Go 仿真器**：维护 `ClusterInfo`、执行 enqueue/allocate 等 action、更新 Pod/Task 状态；不负责解析 `input_config` 目录结构。  
  - **Python 端**：负责配置转换、调用顺序、结果整理与 CSV/文本报告；**不修改**调度算法本身。

- **数据流概要**  
  1. Loader 读 `cluster` / `workload` / `plugins`，生成仿真器可消费的 YAML 字符串。  
  2. `reset` 将节点、负载写入仿真器内存。  
  3. `step` 加载 scheduler 配置并执行调度。  
  4. `stepResult` 返回快照；Python 写盘并生成 FlexNPU 与 `output_config` 报表。

- **其他目录**  
  - `Submit_volcano_workloads/common/`：如 `JsonHttpClient` 等通用工具（新特性优先放在 `input_config/`，见 `input_config/__init__.py` 说明）。  
  - `Submit_volcano_workloads/figures/`：历史作图脚本（当前主流程可不启用）。  
  - **`Volcano_simulator/`** 与 **`Submit_volcano_workloads/`** 为仓库内并列目录，需分别构建/运行。

---

## 三、运行方式

### 1. 启动仿真器（Go）

在仓库根目录进入仿真器工程并编译、运行（监听 **8006** 端口，与 `SimRun.py` 中 `sim_base_url` 一致）：

```bash
cd Volcano_simulator/cmd/sim
go build -o sim .
./sim
```

（Windows 下可执行 `main.exe` 或等价命令；具体以本机 Go 环境为准。）

### 2. Python 环境与依赖

建议使用 Python 3.8+，在客户端目录下安装依赖（至少包含 `requests`、`PyYAML`、`prettytable` 等）：

```bash
cd Submit_volcano_workloads
pip install requests pyyaml prettytable
```

### 3. 执行一次仿真

确认仿真器已监听 `http://localhost:8006` 后：

```bash
cd Submit_volcano_workloads
python SimRun.py
```

### 4. 修改输入与结果路径

- 在 **`Submit_volcano_workloads/SimRun.py`** 的 `if __name__ == '__main__':` 中修改：  
  `cluster_path`、`workload_path`、`plugins_path`（例如 `cluster_1.yaml`、`workload_1.yaml` 等）。  
- 结果根目录由 **`Submit_volcano_workloads/input_config/plugins/*.yaml`** 的 `output.outDir` 决定（支持 `{date}`）；其下会创建 `tasks/<时间戳>/`、`jobs/<时间戳>/` 等。

### 5. 说明与校验

- 各输入子目录说明见 **`Submit_volcano_workloads/input_config/README.md`**。  
- 本地仿真结果路径见根目录 **`.gitignore`**（如 `Submit_volcano_workloads/result/`），请勿将大结果目录提交到远端。

---

如有调度行为、端口或 API 变更，请以 **`Volcano_simulator/cmd/sim/main.go`** 与 **`Submit_volcano_workloads/SimRun.py`** 中的实际配置为准。
