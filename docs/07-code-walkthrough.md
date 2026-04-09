# 07 — Code walkthrough: key implementation, line by line

> This document walks through VolcanoSimulator’s critical code line by line so you can see how the system works in depth.

---

## 1. `SimRun.py` main flow

### 1.1 `main`

```python
def main():
    # 1. Configuration ---------------------------------------------------------
    sim_base_url = 'http://localhost:8006'  # Go simulator URL
    
    # Config file paths (edit here to switch configs)
    _base_dir = os.path.dirname(os.path.abspath(__file__))
    cluster_path = os.path.join(_base_dir, 'input_config', 'cluster', 'cluster_1.yaml')
    workload_path = os.path.join(_base_dir, 'input_config', 'workload', 'workload_1.yaml')
    plugins_path = os.path.join(_base_dir, 'input_config', 'plugins', 'plugins.yaml')

    # 2. Load config -----------------------------------------------------
    # input_config_loader turns YAML into a format the simulator understands
    nodes_yaml = load_cluster_for_simulator(cluster_path)
    workload_yaml = load_workload_for_simulator(workload_path)
    
    # Read granularity for downstream reports
    npu_granularity = workload_npu_granularity_percent_from_file(workload_path)
    
    # Load plugin config and get output directory
    scheduler_conf_yaml, result_root = load_plugins_for_simulator(plugins_path)

    # 3. Create result directory -------------------------------------------------
    os.makedirs(result_root, exist_ok=True)
    pods_result_url = result_root

    # 4. Run simulation -----------------------------------------------------
    print("仿真开始")
    
    # 4.1 Init: tell the Go simulator to load cluster and workload
    reset(sim_base_url, nodes_yaml, workload_yaml)
    time.sleep(1)  # wait for init to finish
    
    # 4.2 Schedule: advance scheduling and fetch results
    step(sim_base_url, scheduler_conf_yaml, pods_result_url, npu_granularity)
    
    print("仿真结束")
```

### 1.2 `reset`

```python
def reset(sim_base_url, nodes_yaml, workload_yaml):
    """Initialize the simulation environment."""
    client = JsonHttpClient(sim_base_url)

    # POST /reset
    # Note: nodes_yaml and workload_yaml are strings, not file paths
    dicData = client.get_json('/reset', json={
        'period': "-1",        # -1: run scheduler config only once
        'nodes': nodes_yaml,   # cluster config YAML string
        'workload': workload_yaml,  # workload config YAML string
    })

    # "0" means jobs are still running; cannot reset
    if str(dicData) == "0":
        print("still job runs，can not reset")
    else:
        print("---Simualtion Reset---")
```

### 1.3 Core logic of `step`

```python
def step(sim_base_url, scheduler_conf_yaml, pods_result_url, npu_granularity):
    """Advance scheduling and fetch results."""
    client = JsonHttpClient(sim_base_url)

    # 1. Send scheduler config -------------------------------------------------
    client.get_json('/step', json={
        'conf': scheduler_conf_yaml,
    })

    # 2. Poll for results -----------------------------------------------------
    wait = 0.2  # 200 ms
    while True:
        time.sleep(wait)
        
        # Pull current state
        resultdata = client.get_json('/stepResult', json={'none': ""})
        
        # "0" means scheduling is still in progress
        if str(resultdata) == '0':
            continue  # keep waiting
        else:
            # Got a result
            print("---Simulation Start---")
            
            # Inject granularity for reports
            if isinstance(resultdata, dict):
                resultdata["npuGranularityPercent"] = float(npu_granularity or 0.0)
            
            # Generate reports
            write_tasks_csv(resultdata, pods_result_url)
            write_flexnpu_report(resultdata, pods_result_url)
            write_output_config_csvs(resultdata, pods_result_url)
            
            break  # exit loop
```

---

## 2. `input_config_loader.py`

### 2.1 Cluster config conversion

```python
def cluster_input_to_simulator_yaml(doc: Dict[str, Any]) -> str:
    """
    Convert user-friendly cluster YAML to simulator format
    
    User writes:
        nodes:
          - name: node-1
            labels: {...}
    
    Simulator expects:
        cluster:
          - metadata:
              name: node-1
              labels: {...}
    """
    nodes = doc.get("nodes")
    if not nodes:
        raise ValueError("cluster config: missing 'nodes' list")

    cluster: List[Dict[str, Any]] = []
    for n in nodes:
        name = n.get("name")
        if not name:
            raise ValueError("cluster config: node entry missing 'name'")
        
        # Build simulator-style node definition
        entry: Dict[str, Any] = {
            "metadata": {
                "name": name,
                "labels": n.get("labels") or {},
                "annotations": n.get("annotations") or {},
            },
            "spec": n.get("spec") if n.get("spec") is not None else {"unschedulable": False},
            "status": n.get("status") or {},
        }
        cluster.append(entry)

    # Serialize back to YAML string
    return yaml.safe_dump({"cluster": cluster}, sort_keys=False, allow_unicode=True)
```

### 2.2 Workload config conversion (important)

```python
def workload_input_to_simulator_yaml(doc: Dict[str, Any]) -> str:
    """
    Convert workload config. Key behaviors:
    1. Round flexnpu_core up to granularity steps
    2. Record raw values in annotations
    3. Normalize task structure
    """
    spec_root = doc.get("spec") or {}
    granularity = float(spec_root.get("npuGranularityPercent") or 0)

    jobs = doc.get("jobs")
    if jobs is None:
        raise ValueError("workload config: missing 'jobs'")

    out_jobs: List[Dict[str, Any]] = []
    for job in jobs:
        # Deep copy to avoid mutating originals
        job_copy = yaml.safe_load(yaml.safe_dump(job, sort_keys=False, allow_unicode=True))
        jspec = job_copy.get("spec") or {}
        
        # Important: normalize task templates
        _normalize_task_templates(jspec.get("tasks"), granularity)
        job_copy["spec"] = jspec
        out_jobs.append(job_copy)

    return yaml.safe_dump({"jobs": out_jobs}, sort_keys=False, allow_unicode=True)


def _normalize_task_templates(tasks: Optional[List[Dict[str, Any]]], granularity: float) -> None:
    """
    Normalize task structure and round flexnpu_core to granularity.
    """
    if not tasks:
        return
    
    for task in tasks:
        # Wrap spec as template.spec (Volcano shape)
        if "template" not in task and "spec" in task:
            task["template"] = {"spec": task.pop("spec")}
        
        tmpl = task.get("template") or {}
        pod_spec = tmpl.get("spec") or {}
        
        # If granularity is set, round flexnpu_core
        if granularity and float(granularity) > 0:
            g = float(granularity)
            raw_by_c: Dict[str, float] = {}
            
            for container in pod_spec.get("containers") or []:
                if not isinstance(container, dict):
                    continue
                
                cname = str(container.get("name") or "__default__")
                res = container.get("resources") or {}
                
                # Handle requests and limits
                for rk in ("requests", "limits"):
                    m = res.get(rk)
                    if not isinstance(m, dict):
                        continue
                    
                    key = "volcano.sh/flexnpu-core.percentage"
                    if key not in m:
                        continue
                    
                    try:
                        v = float(m[key])
                    except (TypeError, ValueError):
                        continue
                    
                    # Round up to granularity step
                    rounded = _ceil_to_step(v, g)
                    
                    # Record raw value (for utilization math)
                    if rk == "requests":
                        raw_by_c[cname] = v
                    elif cname not in raw_by_c:
                        raw_by_c[cname] = v
                    
                    # Write rounded value back
                    if rounded == int(rounded):
                        m[key] = str(int(rounded))
                    else:
                        m[key] = str(rounded)
            
            # Store raw values in annotations for reports
            if raw_by_c:
                meta = tmpl.setdefault("metadata", {})
                ann = meta.setdefault("annotations", {})
                ann["volcano.sh/flexnpu-core.percentage-raw-by-container"] = json.dumps(
                    raw_by_c, ensure_ascii=False
                )
```

---

## 3. `main.go` main loop

### 3.1 Global state

```go
var (
    // Cluster state (all in memory)
    cluster = &schedulingapi.ClusterInfo{
        Nodes:          make(map[string]*schedulingapi.NodeInfo),
        Jobs:           make(map[schedulingapi.JobID]*schedulingapi.JobInfo),
        Queues:         make(map[schedulingapi.QueueID]*schedulingapi.QueueInfo),
        NamespaceInfo:  make(map[schedulingapi.NamespaceName]*schedulingapi.NamespaceInfo),
        RevocableNodes: make(map[string]*schedulingapi.NodeInfo),
    }
    
    // Pending jobs (priority queue ordered by submit time)
    jobQueue = util.NewPriorityQueue(func(l interface{}, r interface{}) bool {
        lv := l.(*schedulingapi.JobInfo)
        rv := r.(*schedulingapi.JobInfo)
        // Sort by SubTimestamp ascending
        return lv.SubTimestamp.Time.Before(rv.SubTimestamp.Time)
    })
    
    // Scheduler config
    acts  []framework.Action    // action chain
    tiers []conf.Tier           // plugin tiers
    cfg   []conf.Configuration  // plugin configuration
    
    // Control flags
    loadNewSchedulerConf = true   // new config waiting to be applied
    notCompletion        = false  // work still outstanding
    restartFlag          = true   // restart in progress
    cnt                  = int64(0)  // loop counter
    
    // Simulation clock (starts at 0)
    schedulingapi.NowTime = metav1.NewTime(time.Time{})
)
```

### 3.2 Main loop

```go
func main() {
    // 1. Default queue and namespace
    initDefaultQueueAndNamespace()
    
    // 2. Register Volcano plugins
    actions.InitV2()
    
    // 3. Start HTTP server (background goroutine)
    go server()
    
    fmt.Print("simulator start...")

    // 4. Main simulation loop
    for true {
        // 4.1 Idle if no work or restarting
        for !notCompletion || restartFlag {
            time.Sleep(time.Duration(0.2 * 1e9))  // 200 ms
        }

        // 4.2 Submit jobs whose time has come
        for !jobQueue.Empty() {
            front := jobQueue.Pop().(*schedulingapi.JobInfo)
            
            // Not yet submit time: put back
            if schedulingapi.NowTime.Time.Before(front.SubTimestamp.Time) {
                jobQueue.Push(front)
                break
            } else {
                // Admit into cluster
                cluster.Jobs[front.UID] = front
                
                // Set Pod creation time
                for _, task := range front.Tasks {
                    task.Pod.SetCreationTimestamp(schedulingapi.NowTime)
                }
            }
        }

        // 4.3 Advance container creation (Binding → Running)
        // Decrement countdown
        for _, node := range cluster.Nodes {
            for _, task := range node.Tasks {
                if task.Status == schedulingapi.Binding {
                    task.CtnCreationCountDown -= 1
                }
            }
        }
        
        // Promote eligible Binding tasks to Running
        for _, node := range cluster.Nodes {
            if node.CtnCreationTimeInterval != 0 && 
               cnt%node.CtnCreationTimeInterval != 0 {
                continue
            }
            
            // Earliest-created Binding task with countdown 0
            var selectTask *schedulingapi.TaskInfo
            for _, task := range node.Tasks {
                if task.Status != schedulingapi.Binding {
                    continue
                }
                if task.CtnCreationCountDown > 0 {
                    continue
                }
                if selectTask == nil || 
                   task.Pod.CreationTimestamp.Before(&selectTask.Pod.CreationTimestamp) {
                    selectTask = task
                }
            }
            
            // Move to Running
            if selectTask != nil {
                selectTask.Status = schedulingapi.Running
                selectTask.Pod.Status.Phase = v1.PodRunning
                selectTask.Pod.Status.StartTime = schedulingapi.NowTime.DeepCopy()
            }
        }

        // 4.4 Wait for new scheduler config
        if (cnt == 0) || (period != -1 && cnt%period == 0) {
            loadNewSchedulerConf = false
            fmt.Println("wait for conf...")
        }
        
        for !loadNewSchedulerConf {
            time.Sleep(time.Duration(1e9))  // 1 s
        }

        if restartFlag {
            continue
        }

        // 4.5 Run scheduling (core)
        ssn := framework.OpenSessionV2(cluster, tiers, cfg)
        for _, action := range acts {
            action.Execute(ssn)
        }

        // 4.6 Sync Pod phases (simplified external view)
        syncSimulationPodPhases()

        // 4.7 Update completion flag
        notCompletion = clusterHasBindingTask() || !jobQueue.Empty()

        // 4.8 Advance time by 1 second
        schedulingapi.NowTime = metav1.NewTime(
            schedulingapi.NowTime.Add(time.Duration(1e9)))
        cnt += 1
    }
}
```

---

## 4. HTTP handlers

### 4.1 `reset` handler

```go
func reset(w http.ResponseWriter, r *http.Request) {
    // 1. If work is still active, stop first
    if notCompletion {
        restartFlag = true
        loadNewSchedulerConf = true  // if waiting on config, unblock
        time.Sleep(time.Duration(1e9))
        
        // Clear queue
        jobQueue = util.NewPriorityQueue(...)
    }
    fmt.Println("reset...")

    // 2. Reset cluster state
    cluster = &schedulingapi.ClusterInfo{...}
    initDefaultQueueAndNamespace()

    // 3. Parse request
    body, _ := ioutil.ReadAll(r.Body)
    var workload simulator.WorkloadType
    json.Unmarshal(body, &workload)

    // 4. Load nodes
    err, nodes := simulator.Yaml2Nodes([]byte(workload.Nodes))
    for _, node := range nodes["cluster"] {
        nodeInfo := schedulingapi.NewNodeInfo(&node.Node)
        cluster.Nodes[nodeInfo.Name] = nodeInfo
        
        // Container creation parameters
        nodeInfo.CtnCreationTime = node.CtnCreationTime
        nodeInfo.CtnCreationExtraTime = node.CtnCreationExtraTime
        nodeInfo.CtnCreationTimeInterval = node.CtnCreationTimeInterval
    }

    // 5. Load jobs
    err, jobs := simulator.Yaml2Jobs([]byte(workload.Workload))
    for _, job := range jobs["jobs"] {
        jobInfo := schedulingapi.NewJobInfoV2(job)
        
        // Parse submit time
        if subTime, found := job.Labels["sub-time"]; found {
            if timestamp, err := strconv.Atoi(subTime); err == nil {
                jobInfo.SubTimestamp = metav1.NewTime(
                    time.Time{}.Add(time.Duration(timestamp * 1e9)))
            }
        }
        
        jobQueue.Push(jobInfo)
    }

    notCompletion = true
    fmt.Println("reset done")

    // 6. Acknowledge
    info := simulator.Info{Done: !notCompletion, ...}
    resp, _ := json.Marshal(info)
    restartFlag = false
    w.Write(resp)
}
```

### 4.2 `step` handler

```go
func step(w http.ResponseWriter, r *http.Request) {
    body, _ := ioutil.ReadAll(r.Body)
    var scheduler_conf simulator.ConfType
    json.Unmarshal(body, &scheduler_conf)

    // Parse scheduler config
    acts, tiers, cfg, err = scheduler.UnmarshalSchedulerConfV2(scheduler_conf.Conf)
    if err != nil {
        fmt.Println("error:", err)
        return
    }

    fmt.Println("load conf:")
    fmt.Println(scheduler_conf.Conf)

    // Signal new config; main loop will run scheduling
    loadNewSchedulerConf = true

    w.Write([]byte(`1`))
}
```

### 4.3 `stepResult` handler

```go
func stepResult(w http.ResponseWriter, r *http.Request) {
    // Still scheduling → "0"
    if loadNewSchedulerConf && notCompletion {
        w.Write([]byte(`0`))
        return
    }

    // Build payload
    var v1NodeList []*v1.Node
    for _, node := range cluster.Nodes {
        // Trimmed node view
        v1Node := util.BuildNode(node.Name, ...)
        v1NodeList = append(v1NodeList, v1Node)
    }

    var PodList []*v1.Pod
    for _, job := range cluster.Jobs {
        for _, task := range job.Tasks {
            PodList = append(PodList, task.Pod)
        }
    }

    info := simulator.Info{
        NotCompletion: notCompletion,
        Nodes:         cluster.Nodes,
        Jobs:          cluster.Jobs,
        Done:          !notCompletion,
        V1Nodes:       v1NodeList,
        Pods:          PodList,
        Clock:         schedulingapi.NowTime.Local().String(),
    }

    resp, _ := json.Marshal(info)
    w.Write(resp)
}
```

---

## 5. `flexnpu_util_report.py` core algorithms

### 5.1 Pod → card allocation estimate

```python
def estimate_card_usage(
    node_cards: List[Dict], 
    pods: List[Dict], 
    granularity: float
) -> Dict[str, List[Dict]]:
    """
    Estimate how Pods split across GPU cards
    
    Strategy: round-robin placement
    """
    pod_shares = {}
    next_idx = 0  # next card index to assign
    
    for pod in pods:
        # Parse Pod resource needs
        pod_name = pod["metadata"]["name"]
        annotations = pod["metadata"].get("annotations", {})
        
        # How many cards
        num_cards = int(annotations.get("volcano.sh/flexnpu-num", 1))
        
        # Total demand
        total_core = get_pod_core_request(pod)
        total_mem = get_pod_memory_request(pod)
        
        # Per-card share
        core_per_card = total_core / num_cards
        mem_per_card = total_mem / num_cards
        
        # Round to granularity (allocation side)
        if granularity > 0:
            core_per_card = math.ceil(core_per_card / granularity) * granularity
        
        # Round-robin assignment
        assigned = []
        for i in range(num_cards):
            card_idx = (next_idx + i) % len(node_cards)
            card = node_cards[card_idx]
            
            assigned.append({
                "card_id": card["id"],
                "core": core_per_card,
                "memory": mem_per_card,
            })
        
        pod_shares[pod_name] = assigned
        next_idx += num_cards
    
    return pod_shares
```

### 5.2 Utilization computation

```python
def compute_flexnpu_snapshot(resultdata: Dict) -> Dict:
    """
    Compute a FlexNPU utilization snapshot
    """
    nodes = resultdata.get("Nodes", {})
    jobs = resultdata.get("Jobs", {})
    granularity = resultdata.get("npuGranularityPercent", 0)
    
    snapshot = {}
    
    for node_name, node_info in nodes.items():
        # 1. Parse card list for the node
        cards = parse_node_cards(node_info)
        
        # 2. Pods on this node
        pods = get_pods_on_node(jobs, node_name)
        
        # 3. Estimate Pod→card mapping
        pod_shares = estimate_card_usage(cards, pods, granularity)
        
        # 4. Per-card utilization
        card_stats = {}
        for card in cards:
            card_id = card["id"]
            total_core = card["capacity"]
            total_mem = card["memory"]
            
            # Sum Pod usage on this card
            used_core = 0
            used_mem = 0
            for pod_name, shares in pod_shares.items():
                for share in shares:
                    if share["card_id"] == card_id:
                        used_core += share["core"]
                        used_mem += share["memory"]
            
            card_stats[card_id] = {
                "capacity_core": total_core,
                "used_core": used_core,
                "utilization_core": used_core / total_core * 100 if total_core > 0 else 0,
                "capacity_mem": total_mem,
                "used_mem": used_mem,
                "utilization_mem": used_mem / total_mem * 100 if total_mem > 0 else 0,
            }
        
        # 5. Node-level rollup
        total_capacity_core = sum(c["capacity"] for c in cards)
        total_used_core = sum(s["used_core"] for s in card_stats.values())
        
        snapshot[node_name] = {
            "cards": card_stats,
            "total_utilization_core": total_used_core / total_capacity_core * 100,
        }
    
    return snapshot
```

---

## 6. Key design patterns

### 6.1 Observer (polling)

The Python client observes the Go simulator by polling:

```python
# Python side
while True:
    result = client.get_json('/stepResult')
    if str(result) != '0':  # state changed
        handle_result(result)
        break
    time.sleep(0.2)
```

### 6.2 State machine

Pod phase transitions:

```
Pending ──[scheduler assigns]──> Pipelined ──[Gang satisfied]──> Binding ──[container start]──> Running
```

### 6.3 Strategy

Scheduler plugins implement different policies via interfaces:

```go
type Plugin interface {
    Name() string
    PredicateFn() PredicateFn
    NodeOrderFn() NodeOrderFn
    // ...
}

// Gang plugin
type gangPlugin struct{}
func (g *gangPlugin) Name() string { return "gang" }

// DRF plugin
type drfPlugin struct{}
func (d *drfPlugin) Name() string { return "drf" }
```

### 6.4 Factory

Registering Actions:

```go
func InitV2() {
    // Register Actions
    RegisterAction("enqueue", enqueue.New())
    RegisterAction("allocate", allocate.New())
    RegisterAction("preempt", preempt.New())
    RegisterAction("backfill", backfill.New())
}
```

---

## 7. Debugging tips

### 7.1 Logging in Go

```go
func main() {
    // Log at important points
    fmt.Printf("当前时间: %v\n", schedulingapi.NowTime)
    fmt.Printf("待提交队列长度: %d\n", jobQueue.Len())
    fmt.Printf("集群任务数: %d\n", len(cluster.Jobs))
}
```

### 7.2 Inspecting raw data in Python

```python
def step(...):
    resultdata = client.get_json('/stepResult', ...)
    
    # Print raw payload
    import json
    print(json.dumps(resultdata, indent=2))
    
    # Or write to disk
    with open('debug_result.json', 'w') as f:
        json.dump(resultdata, f, indent=2)
```

### 7.3 Using `pdb`

```python
import pdb

def some_function():
    # Breakpoint
    pdb.set_trace()
    
    # Execution pauses here; inspect variables and step
    result = process_data()
    return result
```

---

## 8. Summary

**Important code paths:**

```
Python:
  SimRun.py::main()
    → reset() → input_config_loader.load_*()
    → step() → JsonHttpClient.get_json()
    → flexnpu_util_report.compute_*()
    → output_csv_reports.write_*()

Go:
  main.go::main()
    → server() [goroutine]
    → main loop:
      - submitDueJobs()
      - advanceContainerCreation()
      - runScheduling() → framework.OpenSessionV2() → actions.Execute()
      - syncSimulationPodPhases()
```

**Core ideas:**
1. **Discrete time**: one-second ticks, easy to reason about
2. **Async boundary**: HTTP polling decouples the two sides
3. **Dual-track metrics**: raw demand vs allocated/rounded values
4. **Real scheduler**: runs actual Volcano code

---

You now have a full walkthrough of the docs: you can see how the system works, change code to add features, design experiments, and contribute to the project.

**Happy simulating.**
