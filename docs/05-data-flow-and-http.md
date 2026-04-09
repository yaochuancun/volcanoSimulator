# 05 Data flow and HTTP: detailed protocol reference

> This document describes the HTTP communication protocol between the Python client and the Go simulator in detail, including API definitions, data formats, and interaction flows.

---

## 1. Communication architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Python client                             │
│                 (localhost: any port)                        │
│                                                              │
│  ┌─────────────┐      ┌─────────────────────┐               │
│  │  JsonHttp   │──────│    requests.post    │               │
│  │  Client     │      │    requests.get     │               │
│  └─────────────┘      └─────────────────────┘               │
└──────────────────────────────────────────────────────────────┘
                           │
                           │ HTTP/1.1
                           │ Content-Type: application/json
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                     Go simulator                             │
│                  (localhost:8006)                            │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              http.ListenAndServe(":8006")             │   │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │   │
│  │  │ /reset   │  │ /step    │  │ /stepResult      │   │   │
│  │  │ Handler  │  │ Handler  │  │ Handler          │   │   │
│  │  └──────────┘  └──────────┘  └──────────────────┘   │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. API overview

| Endpoint | Method | Purpose | Request body | Response |
|----------|--------|---------|--------------|----------|
| `/reset` | POST | Initialize simulation | JSON | JSON / `"0"` |
| `/step` | POST | Advance scheduling | JSON | `"1"` |
| `/stepResult` | POST/GET | Fetch result | JSON (optional) | JSON / `"0"` |
| `/stepResultAnyway` | GET | Force-fetch result | - | JSON |

---

## 3. Detailed API definitions

### 3.1 POST /reset — initialize simulation

**Purpose:** Reset the simulation environment and load new cluster and workload configuration.

**Request:**
```http
POST /reset HTTP/1.1
Host: localhost:8006
Content-Type: application/json

{
  "period": "-1",
  "nodes": "cluster:\n  - metadata:\n      name: node-1\n    ...",
  "workload": "jobs:\n  - metadata:\n      name: job-1\n    ..."
}
```

**Field reference:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `period` | string | Yes | Scheduler config load period (seconds); `-1` means load once only |
| `nodes` | string | Yes | Cluster configuration as a YAML string |
| `workload` | string | Yes | Workload configuration as a YAML string |

**Response (success):**
```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "Done": false,
  "V1Nodes": [
    {
      "metadata": {"name": "node-1"},
      "status": {"capacity": {...}, "allocatable": {...}}
    }
  ],
  "Clock": "0001-01-01 00:00:00 +0000 UTC"
}
```

**Response (failure — jobs still running):**
```http
HTTP/1.1 200 OK
Content-Type: text/plain

0
```

**Notes:**

- If jobs are still running in the simulator, returning `"0"` means reset was refused.
- Wait for the current simulation to finish, or restart the simulator process.

---

### 3.2 POST /step — advance scheduling

**Purpose:** Send scheduler configuration and trigger one scheduling round.

**Request:**
```http
POST /step HTTP/1.1
Host: localhost:8006
Content-Type: application/json

{
  "conf": "actions: enqueue, allocate, backfill\ntiers:\n  - plugins:\n      - name: gang\n      ..."
}
```

**Field reference:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `conf` | string | Yes | Scheduler configuration as a YAML string |

**Scheduler configuration format:**
```yaml
actions: "enqueue, allocate, backfill"
tiers:
  - plugins:
      - name: gang
      - name: drf
      - name: proportion
  - plugins:
      - name: nodeorder
      - name: binpack
```

**Response:**
```http
HTTP/1.1 200 OK
Content-Type: text/plain

1
```

**Notes:**

- `"1"` only means the configuration was accepted, not that scheduling has finished.
- Scheduling runs asynchronously; poll `/stepResult` for the outcome.

---

### 3.3 GET/POST /stepResult — fetch result

**Purpose:** Retrieve the current simulation state.

**Request (GET):**
```http
GET /stepResult HTTP/1.1
Host: localhost:8006
```

**Request (POST with empty JSON):**
```http
POST /stepResult HTTP/1.1
Host: localhost:8006
Content-Type: application/json

{
  "none": ""
}
```

**Response (scheduling complete):**
```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "NotCompletion": false,
  "Nodes": {
    "node-1": {
      "Name": "node-1",
      "Allocatable": {...},
      "Used": {...},
      "Tasks": {...}
    }
  },
  "Jobs": {
    "default/job-1": {
      "Name": "job-1",
      "Tasks": {
        "task-0": {
          "Name": "task-0",
          "Status": "Running",
          "NodeName": "node-1",
          "Pod": {...}
        }
      }
    }
  },
  "Done": true,
  "V1Nodes": [...],
  "Pods": [...],
  "Clock": "0001-01-01 00:00:15 +0000 UTC"
}
```

**Response (still scheduling):**
```http
HTTP/1.1 200 OK
Content-Type: text/plain

0
```

**Key fields:**

| Field | Type | Description |
|-------|------|-------------|
| `NotCompletion` | bool | Whether any tasks are still incomplete |
| `Nodes` | object | Node state; keys are node names |
| `Jobs` | object | Job state; keys are `"namespace/name"` |
| `Clock` | string | Current simulation time |
| `Done` | bool | Whether everything is complete (inverse of `NotCompletion` in intent) |
| `V1Nodes` | array | Condensed node list |
| `Pods` | array | Pod list |

---

### 3.4 GET /stepResultAnyway — force-fetch result

**Purpose:** Return the current state regardless of whether scheduling has finished.

**Request:**
```http
GET /stepResultAnyway HTTP/1.1
Host: localhost:8006
```

**Response:**
```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "NotCompletion": true,
  "Nodes": {...},
  "Jobs": {...}
}
```

**Difference from `/stepResult`:**

- `/stepResult`: returns `"0"` while scheduling; JSON after completion.
- `/stepResultAnyway`: always returns JSON (fields may be fewer).

---

## 4. End-to-end interaction flow

### 4.1 Sequence diagram

```
Python client                Go simulator
    │                            │
    │── POST /reset ────────────→│
    │   {nodes, workload}        │
    │                            │  Initialize ClusterInfo
    │                            │  Load Nodes and Jobs
    │←─── JSON ack ─────────────│
    │                            │
    │── POST /step ─────────────→│
    │   {scheduler_conf}         │
    │                            │  Parse scheduler config
    │                            │  loadNewSchedulerConf = true
    │←─── "1" ──────────────────│
    │                            │
    │  (Go main loop runs scheduling)
    │                            │
    │── GET /stepResult ────────→│
    │                            │  Scheduling done?
    │←─── "0" ──────────────────│  No → return "0"
    │                            │
    │  (wait 200ms)               │
    │                            │
    │── GET /stepResult ────────→│
    │                            │  Scheduling done?
    │←─── JSON result ──────────│  Yes → return JSON
    │                            │
    │  (Python builds reports)    │
    │                            │
```

### 4.2 State machine

```
┌─────────────┐     reset      ┌─────────────┐
│   Idle      │───────────────→│ Initializing│
│             │                │             │
└─────────────┘                └──────┬──────┘
     ▲                                │
     │                                │ complete
     │                                ▼
     │                          ┌─────────────┐
     │     ┌────────────────────│   Ready     │
     │     │   stepResult JSON  │             │
     │     │                    └──────┬──────┘
     │     │                           │ step
     │     │                           ▼
     │     │                    ┌─────────────┐
     │     │    stepResult "0"  │ Scheduling  │
     │     └────────────────────│             │
     │                          └──────┬──────┘
     │                                 │
     └─────────────────────────────────┘
```

---

## 5. Data structures in detail

### 5.1 ClusterInfo serialization format

```json
{
  "Nodes": {
    "node-1": {
      "Name": "node-1",
      "Allocatable": {
        "MilliCPU": 32000,
        "Memory": 137438953472,
        "ScalarResources": {
          "volcano.sh/flexnpu-core.percentage": 400000,
          "volcano.sh/flexnpu-memory.128mi": 256000
        }
      },
      "Used": {
        "MilliCPU": 8000,
        "Memory": 34359738368,
        "ScalarResources": {
          "volcano.sh/flexnpu-core.percentage": 100000,
          "volcano.sh/flexnpu-memory.128mi": 64000
        }
      },
      "Idle": {...},
      "Tasks": {
        "task-0": {...}
      }
    }
  }
}
```

**Note:**

- Resource values use **MilliValue** (e.g. 100% = 100000).
- On the Python side, divide by 1000 to get practical values.

### 5.2 JobInfo serialization format

```json
{
  "default/job-1": {
    "Namespace": "default",
    "Name": "job-1",
    "UID": "job-1-uid",
    "Queue": "default",
    "Tasks": {
      "task-0": {
        "Name": "task-0",
        "Status": "Running",
        "NodeName": "node-1",
        "Pod": {
          "metadata": {
            "name": "job-1-task-0",
            "namespace": "default",
            "creationTimestamp": "0001-01-01T00:00:00Z",
            "annotations": {
              "volcano.sh/flexnpu-core.percentage-raw-by-container": "{\"train\": 35.0}"
            }
          },
          "spec": {
            "containers": [
              {
                "name": "train",
                "resources": {
                  "requests": {
                    "volcano.sh/flexnpu-core.percentage": "40"
                  }
                }
              }
            ]
          },
          "status": {
            "phase": "Running",
            "startTime": "0001-01-01T00:00:05Z"
          }
        }
      }
    }
  }
}
```

---

## 6. Error handling

### 6.1 Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Connection refused | Simulator not running | Start the Go simulator |
| Returns `"0"` | Jobs still running | Wait for completion or restart simulator |
| JSON parse error | Response was `"0"` | Check `str(result) == '0'` |
| Empty `Nodes` | Cluster not loaded correctly | Check cluster YAML |
| Empty `Jobs` | Workload not loaded correctly | Check workload YAML |

### 6.2 Retry strategy

```python
class JsonHttpClient:
    def get_json(self, path, json=None, max_retries=10):
        for attempt in range(max_retries):
            try:
                response = requests.post(url, json=json, timeout=10)
                response.raise_for_status()
                return response.json()
            except requests.RequestException:
                if attempt == max_retries - 1:
                    raise
                time.sleep(0.1)  # backoff
```

---

## 7. Debugging tips

### 7.1 Testing with curl

```bash
# Test reset
curl -X POST http://localhost:8006/reset \
  -H "Content-Type: application/json" \
  -d '{
    "period": "-1",
    "nodes": "cluster:\n  - metadata:\n      name: node-1",
    "workload": "jobs: []"
  }'

# Test step
curl -X POST http://localhost:8006/step \
  -H "Content-Type: application/json" \
  -d '{"conf": "actions: enqueue, allocate\ntiers: []"}'

# Fetch result
curl http://localhost:8006/stepResult
```

### 7.2 Logs

The Go simulator prints logs to the console:

```
reset...
node-1 :
Allocatable: map[cpu:32000 memory:137438953472 ...]
Idle: map[...]
reset done
wait for conf...
load conf:
actions: enqueue, allocate, backfill
...
```

### 7.3 Data validation

```python
# Validate Nodes structure
assert "Nodes" in resultdata
for node_name, node_info in resultdata["Nodes"].items():
    assert "Allocatable" in node_info
    assert "Used" in node_info
    assert "Tasks" in node_info

# Validate Jobs structure
assert "Jobs" in resultdata
for job_id, job_info in resultdata["Jobs"].items():
    assert "Tasks" in job_info
    for task_id, task_info in job_info["Tasks"].items():
        assert "Pod" in task_info
        assert "Status" in task_info
```

---

## 8. Summary

**HTTP API design principles:**

1. **Simple:** Four endpoints cover all behavior.
2. **Asynchronous:** `step` and `stepResult` are separate to support long-running scheduling.
3. **Resilient:** Failures return `"0"` instead of hard errors.
4. **Complete:** Full cluster state is returned for analysis.

**Key takeaways:**

- `/reset` initializes; response is JSON or `"0"`.
- `/step` triggers scheduling; response is `"1"`.
- `/stepResult` polls for results; response is JSON or `"0"`.
- Resource values are MilliValue; divide by 1000.

---

Ready to write configuration files? Continue with [`06-configuration-guide.md`](06-configuration-guide.md).
