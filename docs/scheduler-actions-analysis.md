## 1. Allocate

Source code: [allocate.go](https://github.com/volcano-sh/volcano/blob/master/pkg/scheduler/actions/allocate/allocate.go)

### Main Purpose

- Assigns pending/ready tasks (Task/Pod) to the most appropriate cluster nodes based on job queues and node states.

### Key Data Structures

- **Session**: The scheduling context, encompassing the scheduling cycle, including task queues, node lists, resource status, etc.
- **TaskInfo**: Represents a scheduling task (i.e., a Pod), encapsulating resource requests, status, etc.
- **NodeInfo**: Represents a node in the cluster; includes allocated and idle resources and bound Tasks.
- **JobInfo**: Represents a job queue; made up of multiple Tasks.

### Major Functions and Workflow

- **allocate.New()**  
  Instantiates the Action (called upon registration), returns the Action object.

- **allocate.Action().Initialize()/Execute()**  
  The core scheduling entry point, executed in the scheduling loop:
  1. Iterates all pending/ready tasks in the Session.
  2. For each Task, fetches its JobInfo, queue, priority, etc.
  3. Invokes core allocation logic (typically `session.Allocate(job, task)`):
     - Selects the optimal node (NodeInfo) according to scheduling policies.
     - Performs resource checks, affinity checks, filtering, etc.
     - On success, records the task-node assignment and resource changes; on failure, retries or rolls back as needed.
  4. After assignment, commits the binding (possibly asynchronously).

#### Data Flow Summary

- Input: Session.Tasks (all tasks to be scheduled), Session.Nodes
- Processing: Iterate tasks → Policy filtering → Node selection → Resource allocation/recording
- Output: Allocation results, updated node and task states in Session

---

## 2. Backfill

Source code: [backfill.go](https://github.com/volcano-sh/volcano/blob/master/pkg/scheduler/actions/backfill/backfill.go)

### Main Purpose

- Optimizes resource usage by scheduling lower-priority or supplementary jobs into underutilized nodes, improving cluster resource efficiency.

### Key Data Structures

- **Session**: As above, the global scheduling state.
- **JobInfo**: Encapsulates jobs as a whole; focuses on jobs eligible for backfill scheduling.
- **TaskInfo**: As above.

### Major Functions and Workflow

- **backfill.New()**  
  Action factory function.

- **backfill.Action().Execute()**
  1. Filters Jobs and Tasks in the Session that are eligible for backfilling (e.g., pending, partly satisfied resources).
  2. For each Backfill Job:
     - Iterates over Tasks, attempting to schedule on available nodes (often using resource fragments).
     - Tasks that meet requirements are bound (session.Backfill()), similar to Allocate but without preemption.
  3. Updates resource usage tables and task states.

#### Data Flow Summary

- Input: All tasks eligible for backfill (Session.Tasks, possibly with priority or other filters)
- Processing: Fragment matching → Task binding → Resource updates
- Output: Backfill assignment results, improved resource utilization

---

## 3. Enqueue

Source code: [enqueue.go](https://github.com/volcano-sh/volcano/blob/master/pkg/scheduler/actions/enqueue/enqueue.go)

### Main Purpose

- Detects jobs/tasks/pods that meet scheduling criteria but have not yet entered the Ready/Pending queue, "waking" them up for the next scheduling round.

### Key Data Structures

- **Session**: The global scheduling context.
- **JobInfo**: Contains jobs and their various states.
- **TaskInfo**: Task representation.
- Queue structures (pendingQueue, readyQueue, etc.).

### Major Functions and Workflow

- **enqueue.New()**  
  Action factory function.

- **enqueue.Action().Execute()**
  1. Iterates over all Jobs and Tasks within the Session.
  2. Determines if each meets the conditions for enqueuing (Ready or Pending):
     - Examples: All dependencies satisfied, resource becomes available.
  3. Jobs/Tasks that qualify are moved from the waiting queue to the pending/ready queue.
  4. Updates queue states and statistics.

#### Data Flow Summary

- Input: Session (all tasks and their current states)
- Processing: Status checking → State transition (waiting → ready/pending)
- Output: Updated scheduling queues, ready for subsequent actions

---

## 4. Preempt

Source code: [preempt.go](https://github.com/volcano-sh/volcano/blob/master/pkg/scheduler/actions/preempt/preempt.go)

### Main Purpose

- Enables high-priority jobs to obtain resources by "preempting" lower-priority jobs—i.e., evicting/killing them to free up resources for critical workloads.

### Key Data Structures

- **Session**: Context for resource and status management.
- **TaskInfo**: Includes task priority, resource allocation status, etc.
- **NodeInfo**: Cluster node and its task/resource allocation.
- **Victim**: A structure representing a preemption victim (the job/task to be evicted).

### Major Functions and Workflow

- **preempt.New()**  
  Action factory function.

- **preempt.Action().Execute()**
  1. Iterates all high-priority pending jobs/tasks.
  2. For high-priority tasks that cannot be scheduled directly, identifies preemption candidates ("Victims"—already scheduled low-priority tasks).
  3. Once Victims are chosen, calls `session.Preempt(victim)` or similar APIs to perform the eviction (e.g., evict/kill the pod).
  4. After successful preemption, retries assignment for the high-priority task.
  5. Updates all relevant resource and status tables, and marks Victims as evicted.

#### Data Flow Summary

- Input: High-priority pending tasks, current resource allocation states
- Processing: Victim selection → Eviction → State updates → Retry assignment
- Output: Updated task/resource/queue states

### Remarks

- Preempt requires careful consideration of resources, priorities, and policy to avoid excessive disruption.
- Preemption behavior is often governed by policies (e.g., minAvailable, priority isolation, preemption limits).

---