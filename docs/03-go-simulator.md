# 03 Go simulator deep dive: how the scheduling kernel works

> This document explains the Go simulator implementation in depth: the HTTP service, the main simulation loop, the Volcano scheduling framework, and state management.

---

## 1. Overall structure

```
Volcano_simulator/
├── cmd/
│   └── sim/
│       └── main.go              # Single entry: HTTP + main loop
├── pkg/
│   ├── scheduler/               # Volcano scheduling framework
│   │   ├── api/                 # Data types (JobInfo, NodeInfo, etc.)
│   │   ├── framework/           # Framework (Session, Action)
│   │   ├── actions/             # Actions (enqueue, allocate, etc.)
│   │   ├── plugins/             # Plugins (gang, drf, etc.)
│   │   ├── cache/               # Scheduler cache
│   │   └── util/                # Utilities
│   └── simulator/
│       └── utils.go             # Simulator helpers
└── vendor/                      # Third-party deps (Volcano, K8s, etc.)
```

---

## 2. main.go: brain and heart

### 2.1 Global state

```go
var (
    // Cluster state (in memory)
    cluster = &schedulingapi.ClusterInfo{
        Nodes:          make(map[string]*schedulingapi.NodeInfo),
        Jobs:           make(map[schedulingapi.JobID]*schedulingapi.JobInfo),
        Queues:         make(map[schedulingapi.QueueID]*schedulingapi.QueueInfo),
        NamespaceInfo:  make(map[schedulingapi.NamespaceName]*schedulingapi.NamespaceInfo),
    }
    
    // Pending submission queue (sorted by submit time)
    jobQueue = util.NewPriorityQueue(func(l, r interface{}) bool {
        return l.(*schedulingapi.JobInfo).SubTimestamp.Time.Before(
            r.(*schedulingapi.JobInfo).SubTimestamp.Time)
    })
    
    // Scheduler configuration
    acts  []framework.Action    // Action chain
    tiers []conf.Tier           // Plugin tiers
    cfg   []conf.Configuration  // Plugin configuration
    
    // Control flags
    loadNewSchedulerConf = true   // Whether new config is pending
    notCompletion        = false  // Whether any work is still incomplete
    restartFlag          = true   // Whether a restart is in progress
    cnt                  = int64(0)      // Loop counter
    
    // Simulation clock
    schedulingapi.NowTime = metav1.NewTime(time.Time{})
)
```

### 2.2 main() flow

```go
func main() {
    // 1. Initialize default queue and namespace
    initDefaultQueueAndNamespace()
    
    // 2. Register Volcano plugins
    actions.InitV2()
    
    // 3. Start HTTP server (background goroutine)
    go server()
    
    // 4. Main simulation loop
    for true {
        // Wait until reset finished or work completed
        for !notCompletion || restartFlag {
            time.Sleep(200 * time.Millisecond)
        }
        
        // Submit jobs whose submit time has been reached
        submitDueJobs()
        
        // Advance container creation (Binding → Running)
        advanceContainerCreation()
        
        // Wait for new scheduler configuration
        waitForSchedulerConf()
        
        // Run scheduling
        runScheduling()
        
        // Sync Pod phases
        syncSimulationPodPhases()
        
        // Update completion state
        notCompletion = clusterHasBindingTask() || !jobQueue.Empty()
        
        // Advance time by one second
        schedulingapi.NowTime = metav1.NewTime(
            schedulingapi.NowTime.Add(time.Second))
        cnt++
    }
}
```

### 2.3 HTTP service (server())

```go
func server() {
    http.HandleFunc("/reset", reset)           // Initialize
    http.HandleFunc("/step", step)             // Advance scheduling
    http.HandleFunc("/stepResult", stepResult) // Fetch result
    http.HandleFunc("/stepResultAnyway", stepResultAnyway)
    
    http.ListenAndServe(":8006", nil)
}
```

**Why HTTP instead of direct calls?**

- Decoupling: Python and Go can restart independently.
- Debuggability: endpoints can be exercised with curl.
- Extensibility: remote simulators become feasible later.

### 2.4 reset handler

**Role:** initialize the simulation environment.

```go
func reset(w http.ResponseWriter, r *http.Request) {
    // 1. If work is still running, stop first
    if notCompletion {
        restartFlag = true
        time.Sleep(1 * time.Second)
        jobQueue = NewPriorityQueue(...)  // Clear queue
    }
    
    // 2. Reset cluster state
    cluster = &schedulingapi.ClusterInfo{...}
    initDefaultQueueAndNamespace()
    
    // 3. Parse request body
    body, _ := ioutil.ReadAll(r.Body)
    var workload simulator.WorkloadType
    json.Unmarshal(body, &workload)
    
    // 4. Load nodes
    nodes := simulator.Yaml2Nodes([]byte(workload.Nodes))
    for _, node := range nodes["cluster"] {
        nodeInfo := schedulingapi.NewNodeInfo(&node.Node)
        cluster.Nodes[nodeInfo.Name] = nodeInfo
        
        // Set container creation parameters
        nodeInfo.CtnCreationTime = node.CtnCreationTime
        nodeInfo.CtnCreationExtraTime = node.CtnCreationExtraTime
        nodeInfo.CtnCreationTimeInterval = node.CtnCreationTimeInterval
    }
    
    // 5. Load jobs
    jobs := simulator.Yaml2Jobs([]byte(workload.Workload))
    for _, job := range jobs["jobs"] {
        jobInfo := schedulingapi.NewJobInfoV2(job)
        
        // Submit time (parsed from label)
        if subTime, ok := job.Labels["sub-time"]; ok {
            timestamp, _ := strconv.Atoi(subTime)
            jobInfo.SubTimestamp = metav1.NewTime(
                time.Time{}.Add(time.Duration(timestamp) * time.Second))
        }
        
        jobQueue.Push(jobInfo)
    }
    
    notCompletion = true
    restartFlag = false
    
    // 6. Acknowledge
    info := simulator.Info{Done: false, ...}
    resp, _ := json.Marshal(info)
    w.Write(resp)
}
```

### 2.5 step handler

**Role:** accept scheduler configuration and trigger scheduling.

```go
func step(w http.ResponseWriter, r *http.Request) {
    body, _ := ioutil.ReadAll(r.Body)
    var scheduler_conf simulator.ConfType
    json.Unmarshal(body, &scheduler_conf)
    
    // Parse scheduler configuration
    acts, tiers, cfg, _ = scheduler.UnmarshalSchedulerConfV2(scheduler_conf.Conf)
    
    // Signal new config (main loop will run scheduling)
    loadNewSchedulerConf = true
    
    w.Write([]byte("1"))  // Success
}
```

### 2.6 stepResult handler

**Role:** return the current simulation state.

```go
func stepResult(w http.ResponseWriter, r *http.Request) {
    // If scheduling is still in progress, return "0" (not ready)
    if loadNewSchedulerConf && notCompletion {
        w.Write([]byte("0"))
        return
    }
    
    // Build response
    info := simulator.Info{
        NotCompletion: notCompletion,
        Nodes:         cluster.Nodes,
        Jobs:          cluster.Jobs,
        Clock:         schedulingapi.NowTime.Local().String(),
    }
    
    resp, _ := json.Marshal(info)
    w.Write(resp)
}
```

---

## 3. Main simulation loop in detail

### 3.1 Submitting jobs when due

```go
func submitDueJobs() {
    for !jobQueue.Empty() {
        front := jobQueue.Pop().(*schedulingapi.JobInfo)
        
        // Not yet submit time: put back
        if schedulingapi.NowTime.Time.Before(front.SubTimestamp.Time) {
            jobQueue.Push(front)
            break
        }
        
        // Admit into cluster
        cluster.Jobs[front.UID] = front
        
        // Set Pod creation timestamps
        for _, task := range front.Tasks {
            task.Pod.SetCreationTimestamp(schedulingapi.NowTime)
        }
    }
}
```

### 3.2 Advancing container creation

**Why simulate container creation?**

In real Kubernetes, after the scheduler binds a Pod to a node, the kubelet must pull images and start containers—this takes time. The simulator models that delay.

```go
func advanceContainerCreation() {
    // 1. Decrement countdown for all Binding tasks
    for _, node := range cluster.Nodes {
        for _, task := range node.Tasks {
            if task.Status == schedulingapi.Binding {
                task.CtnCreationCountDown -= 1
            }
        }
    }
    
    // 2. Per node, at each interval promote eligible Binding tasks to Running
    for _, node := range cluster.Nodes {
        if cnt % node.CtnCreationTimeInterval != 0 {
            continue
        }
        
        // Earliest-created Binding task with countdown <= 0
        var earliestTask *schedulingapi.TaskInfo
        for _, task := range node.Tasks {
            if task.Status == schedulingapi.Binding && task.CtnCreationCountDown <= 0 {
                if earliestTask == nil || 
                   task.Pod.CreationTimestamp.Before(&earliestTask.Pod.CreationTimestamp) {
                    earliestTask = task
                }
            }
        }
        
        if earliestTask != nil {
            earliestTask.Status = schedulingapi.Running
            earliestTask.Pod.Status.Phase = v1.PodRunning
            earliestTask.Pod.Status.StartTime = schedulingapi.NowTime.DeepCopy()
        }
    }
}
```

**Parameters:**

- `CtnCreationTime`: base creation time (seconds).
- `CtnCreationExtraTime`: extra random time (seconds).
- `CtnCreationTimeInterval`: interval between container creations on the same node (seconds).

### 3.3 Running scheduling

```go
func runScheduling() {
    ssn := framework.OpenSessionV2(cluster, tiers, cfg)
    
    for _, action := range acts {
        action.Execute(ssn)
    }
    
    // Close session (release resources)
    // framework.CloseSession(ssn)  // Currently disabled due to a bug
}
```

---

## 4. Volcano scheduling framework

### 4.1 Three-layer structure

```
┌─────────────────────────────────────────────────────────┐
│                    Action layer                         │
│  Defines the scheduling "execution pipeline"            │
│  e.g. enqueue → allocate → backfill                     │
├─────────────────────────────────────────────────────────┤
│                    Plugin layer                         │
│  Defines scheduling "decision policy"                   │
│  e.g. gang plugin for gang scheduling                   │
├─────────────────────────────────────────────────────────┤
│                   Framework layer                       │
│  Provides scheduling "infrastructure"                   │
│  Session, Statement, Cache, etc.                         │
└─────────────────────────────────────────────────────────┘
```

### 4.2 Session: scheduling context

**Session is the "workspace" for one scheduling pass:**

```go
type Session struct {
    UID        uuid.UUID
    
    // Cluster snapshot
    Nodes  []*NodeInfo
    Jobs   map[JobID]*JobInfo
    Queues map[QueueID]*QueueInfo
    
    // Plugin-registered callbacks
    NamespaceOrderFns []CompareFn
    QueueOrderFns     []CompareFn
    JobOrderFns       []CompareFn
    TaskOrderFns      []CompareFn
    PredicateFns      map[string]PredicateFn
    NodeOrderFns      map[string]NodeOrderFn
    
    // Operations recorded this cycle
    Statements []*Statement
}
```

**Why Session?**

- **Isolation:** each scheduling pass is independent.
- **Consistency:** scheduling runs on a snapshot, avoiding mid-flight inconsistency.
- **Rollback:** `Statement` records operations so failures can roll back.

### 4.3 Statement: operation log

**Statement records one scheduling operation and supports rollback:**

```go
type Statement struct {
    operations []Operation
}

type Operation interface {
    Execute()
    Rollback()
}

// Example: allocate operation
type AllocateOperation struct {
    task *TaskInfo
    node *NodeInfo
}

func (op *AllocateOperation) Execute() {
    op.node.AddTask(op.task)
    op.task.NodeName = op.node.Name
}

func (op *AllocateOperation) Rollback() {
    op.node.RemoveTask(op.task)
    op.task.NodeName = ""
}
```

### 4.4 Action: scheduling stages

**Action is a scheduling "phase":**

| Action | Role | Analogy |
|--------|------|---------|
| `enqueue` | Admit eligible jobs into the scheduling queue | Registering for a race |
| `allocate` | Assign nodes to tasks awaiting schedule | Assigning seats |
| `preempt` | Preempt lower-priority workloads | Taking a seat |
| `reclaim` | Reclaim unfair share across queues | Redistributing |
| `backfill` | Pack small jobs into leftover capacity | Filling gaps |
| `elect` | Elect tasks that reserve resources | Holding seats |

**Core logic of the allocate action:**

```go
func (alloc *Action) Execute(ssn *framework.Session) {
    // 1. Collect pending tasks
    var jobs []*JobInfo
    for _, job := range ssn.Jobs {
        if job.IsPending() && ssn.JobValid(job) {
            jobs = append(jobs, job)
        }
    }
    
    // 2. Order (priority, submit time, etc.)
    sortJobs(jobs, ssn.JobOrderFns)
    
    // 3. Assign a node per task
    for _, job := range jobs {
        for _, task := range job.Tasks {
            node := selectNode(task, ssn.Nodes, ssn.PredicateFns, ssn.NodeOrderFns)
            
            if node != nil {
                ssn.Statement.Allocate(task, node)
            }
        }
    }
    
    // 4. Commit all operations (or roll back)
    ssn.Statement.Commit()
}
```

### 4.5 Plugin: scheduling policy

**Plugin holds scheduling "decision logic":**

```go
type Plugin interface {
    Name() string
    
    // Ordering
    NamespaceOrderFn() CompareFn
    QueueOrderFn() CompareFn
    JobOrderFn() CompareFn
    TaskOrderFn() CompareFn
    
    // Filtering
    PredicateFn() PredicateFn
    
    // Scoring
    NodeOrderFn() NodeOrderFn
}
```

**Common plugins:**

| Plugin | Purpose | Typical use |
|--------|---------|-------------|
| `gang` | Gang scheduling: all-or-nothing for a group | Distributed training |
| `drf` | Dominant resource fairness | Multi-tenant fairness |
| `proportion` | Weighted share per queue | Queue capacity mix |
| `binpack` | Tight packing on nodes | Higher utilization |
| `nodeorder` | Order nodes by resources, labels, etc. | Node selection |
| `predicates` | Predicate filters for hard constraints | Feasibility checks |

**gang plugin example:**

```go
func (gp *gangPlugin) JobValid(job *JobInfo) *ValidationResult {
    // Gang: task count must be >= MinAvailable
    if len(job.Tasks) < job.MinAvailable {
        return &ValidationResult{
            Pass:    false,
            Reason:  "NotEnoughTasks",
            Message: "Not enough tasks",
        }
    }
    return &ValidationResult{Pass: true}
}

func (gp *gangPlugin) TaskOrderFn() CompareFn {
    // Same priority inside gang; order by creation time
    return func(l, r interface{}) bool {
        return l.(*TaskInfo).Pod.CreationTimestamp.Before(
            &r.(*TaskInfo).Pod.CreationTimestamp)
    }
}
```

---

## 5. State management

### 5.1 Pod state machine

```
Pending ──→ Pipelined ──→ Binding ──→ Running
  │              │            │           │
  │              │            │           │
  └──────────────┴────────────┴───────────┘
         (In simulation, often simplified: Pending → Running)
```

**States:**

- `Pending`: awaiting schedule.
- `Pipelined`: resources reserved, waiting for gang conditions.
- `Binding`: bound to a node, container starting.
- `Running`: container ready, task executing.

### 5.2 Phase sync

```go
func syncSimulationPodPhases() {
    for _, job := range cluster.Jobs {
        for _, task := range job.Tasks {
            switch task.Status {
            case schedulingapi.Pending,
                 schedulingapi.Pipelined,
                 schedulingapi.Binding:
                task.Pod.Status.Phase = v1.PodPending
            default:
                task.Pod.Status.Phase = v1.PodRunning
            }
        }
    }
}
```

**External simplification:**

- While scheduling (`Pending` / `Pipelined` / `Binding`) → expose as `Pending`.
- When `Running` → expose as `Running`.

Python clients only need two phases, which keeps client logic simple.

---

## 6. Key design choices

### 6.1 Why discrete time (1-second ticks)?

| Approach | Pros | Cons | Choice |
|----------|------|------|--------|
| Continuous time | Precise | Hard to control | No |
| Discrete (1 s) | Simple, controllable | Limited precision | Yes |
| Event-driven | Efficient | More complex | No |

**Takeaway:** For validating scheduling policy, one-second granularity is enough and easy to implement.

### 6.2 Why all in memory?

| Approach | Pros | Cons | Choice |
|----------|------|------|--------|
| Memory | Fast, simple | Lost on restart | Yes |
| Database | Durable | Heavier dependencies | No |
| Files | Durable | Slower | No |

**Takeaway:** The simulator does not need persistence; reset-on-restart matches experiment workflows.

### 6.3 Why HTTP instead of gRPC?

| Approach | Pros | Cons | Choice |
|----------|------|------|--------|
| HTTP/REST | Simple, easy to debug | Slightly slower | Yes |
| gRPC | Fast, typed | More moving parts | No |
| Direct calls | Fastest | Tight coupling | No |

**Takeaway:** HTTP is sufficient and easy to probe with curl.

---

## 7. Summary

Core design of the Go simulator:

```
HTTP API (accepts control commands)
      ↓
Main simulation loop (time advance + event handling)
      ↓
Volcano framework (real scheduler code)
      ↓
In-memory state (virtual cluster)
```

**Highlights:**

1. **Faithful:** runs real Volcano code, not a toy reimplementation.
2. **Controllable:** discrete time; each step is explicit.
3. **Observable:** rich state in responses.
4. **Lightweight:** in-memory, single machine.

---

Ready to go deeper on the FlexNPU resource model? Continue with [`04-flexnpu.md`](04-flexnpu.md).
