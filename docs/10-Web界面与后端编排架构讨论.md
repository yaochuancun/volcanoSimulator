# Web 界面与后端编排 — 架构设计（修订版）

本文档描述目标场景的**架构取向**，不涉及具体实现代码。与 [`architecture.md`](./architecture.md)、[`requirements.md`](./requirements.md) 的关系：在「HTTP + YAML + SimRun」之上，增加 **Web 层**与 **常驻 Python 仿真 Worker（方案 B）**；**不**要求多用户或多会话并发。

---

## 0. 范围与非目标

| 项 | 约定 |
| --- | --- |
| **并发模型** | **单用户、单任务并发**：同一时刻最多一个仿真在执行；新任务可 **拒绝入队** 或 **覆盖/取消** 当前任务（产品二选一，实现时固定一种）。 |
| **集成形态** | **仅采用方案 B**：Python 作为常驻 **仿真 Worker 服务**；Web 后端 **不** `subprocess` 调用 `SimRun.py`。 |
| **多算法** | 支持在一次交互中 **录入多条调度算法配置**，形成 **实验批（batch）**；Worker **串行** 对每条算法执行完整仿真并产出可对比结果。 |
| **Workload 放大** | 支持 **多 workload 条目** 与各自 **放大系数**（如副本倍率、实例数乘子）；由 Worker 在生成 YAML 或内存结构时 **展开**，再送入单次 `reset`→`step` 流程。 |
| **结果导出** | 支持将单次 run 或整批 batch 的 **phase1 / phase2 产物**（CSV、文本报表等）打包 **下载**（见 §5）。 |

**非目标**：多租户隔离、多用户同时跑仿真、Web 多副本间共享 run 状态（可按单机部署假设简化）。

---

## 1. 现状简要（约束）

| 组件 | 现状 |
| --- | --- |
| Go `cmd/sim` | 单进程、全局状态；默认 **8006**；`reset` 清空后再跑。 |
| Python `SimRun.py` | 读 YAML → `reset` → `step` → 轮询 `stepResult`；输出 **`outDir/phase1/`**、可选 **`phase2/`**。 |
| 指标 | 分配率等来自现有 CSV / FlexNPU 报表；**碎片率**宜在 Worker 或公共模块 **统一定义计算**，再暴露给前端。 |

在 **单并发** 下，**无需** 为每个请求起独立仿真器进程；**一个** Go sim 实例 + Worker 内 **互斥**（或单线程执行队列）即可满足顺序执行 batch 内多轮算法。

---

## 2. 推荐分层（逻辑视图）

```
┌─────────────────────────────────────────────────────────────┐
│  Browser                                                     │
│  · 集群 / 多 workload + 放大系数 / 多调度算法（列表录入）      │
│  · 图表：分配率、碎片率等（消费聚合 JSON）                     │
│  · 导出：下载 ZIP 或分文件（由 Web 代理 Worker 导出接口）      │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTPS / JSON（可选 SSE 轮询状态）
┌───────────────────────────▼─────────────────────────────────┐
│  Web Backend（新建，可极简）                                  │
│  · 单活跃 run/batchId；拒绝或取消策略                         │
│  · 表单校验 → 调用 Worker API；转发导出流                    │
│  · 可选：把 Worker 返回的 metrics 再裁剪给图表               │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP（内网）
┌───────────────────────────▼─────────────────────────────────┐
│  Python 仿真 Worker（方案 B，常驻）                           │
│  · POST /runs 或 /batches：落盘临时目录、展开 workload 放大     │
│  · 对 batch 内每个 algorithm_i：reset → 全量 step → 写 out_i   │
│  · 聚合 metrics；提供 GET status / result / export            │
└───────────────────────────┬─────────────────────────────────┘
                            │ localhost:8006（固定）
┌───────────────────────────▼─────────────────────────────────┐
│  Go cmd/sim（单实例）                                         │
└─────────────────────────────────────────────────────────────┘
```

**原则**：浏览器不直连仿真器；仿真器端口不暴露公网；Worker 与 sim 同机或同 Pod 网络即可。

---

## 3. 方案 B：Worker 职责与接口形态

将现有 `SimRun` 流水线迁入 Worker 进程内（模块复用，非子进程）：

| 能力 | 说明 |
| --- | --- |
| **接收 batch** | 请求体包含：集群描述、workload 列表（每项含 **放大系数**）、**算法配置列表**（每项对应一份与 `plugins.yaml` 中 `scheduler` 语义一致的片段或整文件）。 |
| **展开** | Worker 根据放大系数生成 **等价 workload YAML**（或内存结构），保证与现有 `load_*_for_simulator` 契约一致。 |
| **串行仿真** | 对 `algorithm_1 … algorithm_k`：**依次** `reset`、写入本轮 plugins、跑满 step、写 **`outDir/<algo_id>/phase1|phase2`**，避免并行 `reset` 竞态。 |
| **状态** | `batchId` + `currentAlgorithmIndex` + `phase`；单 Worker 内 **全局锁** 保证同时只有一个 batch 在执行。 |
| **健康** | `/health` 探针；与 sim 的 TCP 连通性可选检查。 |

**Web 后端**只做：鉴权（若需要）、单任务门闸、静态资源与 **导出流代理**（避免 CORS 与鉴权泄露）。

---

## 4. 多算法与 workload 放大（数据语义）

### 4.1 多调度算法

- 前端维护 **算法配置数组**（顺序有意义时可作为对比维度标签）。
- 一次提交 = 一个 **batch**；Worker 为每个元素产出 **独立子目录**（如 `out/batch_xxx/algo_default/`、`out/batch_xxx/algo_gang/`）。
- 图表 API 返回结构建议包含 **`runs[]`** 或 **`byAlgorithm: { [algoKey]: metrics }`**，便于并列柱状图 / 折线对比。

### 4.2 Workload 放大系数

- 每条 workload 行除模板字段外带 **`scale` 或 `replicaMultiplier`**（命名在 OpenAPI 中固定）。
- **展开规则** 需在文档与实现中写死一种（例如：`replicas *= ceil(scale)`，或按 task 数复制）；与现有 `workload.yaml` schema 对齐，避免仿真器侧歧义。
- 放大在 **每轮算法仿真前** 已固化到 YAML；若未来需要「同一算法、多档放大」对比，可定义为 **batch 的二维展开**（产品层再扩展）。

---

## 5. 结果展示与导出

### 5.1 图表数据

- **阶段一**：自各 `phase1/` CSV、`flexnpu_utilization.txt` 解析为 JSON。  
- **阶段二**：`phase2/*.csv` 供完成时间类图表。  
- **碎片率**：Worker 计算后写入 **`metrics.fragmentation`**（或等价字段），前端只渲染。

### 5.2 导出（必选能力）

| 方式 | 说明 |
| --- | --- |
| **ZIP** | `GET /batches/{id}/export` → `application/zip`，内含整棵 `outDir` 或仅 `phase1`+`phase2`+ 元数据 `manifest.json`（含算法名、时间戳）。 |
| **单文件** | 可选 `?path=` 限定单个 CSV，便于脚本拉取。 |

Web 层将响应当 **附件下载**（`Content-Disposition`），浏览器「导出结果」按钮直连该 API（经后端代理亦可）。

---

## 6. API 形状（示例，供 OpenAPI 细化）

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/batches` | 提交集群 + workloads[]（含 scale）+ algorithms[]；返回 `{ batchId }` |
| `GET` | `/batches/{id}/status` | `idle \| running \| succeeded \| failed`，含当前算法索引、进度文案 |
| `GET` | `/batches/{id}/metrics` | 聚合图表 JSON（含按算法分桶） |
| `GET` | `/batches/{id}/export` | ZIP 流 |
| `DELETE` | `/batches/{id}` | 可选：取消运行并清理临时目录 |

单用户单并发时，`POST /batches` 若已有 `running`，返回 **409** 或 **202 覆盖**（与产品约定一致）。

---

## 7. Go 仿真器侧

- **单实例 + 固定端口** 即可。  
- Worker 在 batch 内切换算法时：**必须** `reset` 后再应用新 plugins，与现有一致。  
- 无需「多进程多端口」；除非将来放宽为多用户并发再引入实例池。

---

## 8. 演进顺序（实现参考）

1. 抽出 Worker 进程与 **batch 串行** 状态机；内嵌现有 `SimRun` 逻辑。  
2. 固定 **workload 放大** 与 **多算法目录布局** 契约。  
3. Web 后端：单门闸 + 代理 metrics / export。  
4. 前端：表单（多行 workload、多段算法）、图表、导出按钮。  
5. 碎片率公式与单元测试。

---

## 9. 风险（缩小范围后仍保留）

- 单 batch 内 **算法数量 × step 数** 过大导致超时：Worker 与前端需 **总超时** 与 **进度反馈**。  
- 任意 YAML/大展开：临时磁盘配额与 **展开结果大小** 上限。  
- 仍仅针对 **仿真器**，与真实集群无关。

---

## 10. 相关文档

- [`architecture.md`](./architecture.md) — 仿真器与 Python 模块划分。  
- [`requirements.md`](./requirements.md) — 功能与报表基线。  
- [`01-架构全景.md`](./01-架构全景.md) — 系列文档交叉索引时可链回本文。

---

*文档状态：已按「单用户单并发、方案 B、多算法 batch、workload 放大、结果导出」修订。*
