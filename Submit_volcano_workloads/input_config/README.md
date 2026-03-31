# input_config

Sample **cluster**, **workload**, and **plugins** inputs. Each file lives in its own subdirectory:

| Path | Role |
| --- | --- |
| `cluster/cluster.yaml` | Node list + FlexNPU topology |
| `workload/workload.yaml` | Volcano JobList + `npuGranularityPercent` |
| `plugins/plugins.yaml` | Scheduler plugin tiers + `output` (semantic only for HTTP mode) |
| `plugins/plugins_mvp.yaml` | MVP sample **without `gang`** (matches current simulator scope) |

JSON Schemas for IDE validation: [`../schemas/`](../schemas/).
