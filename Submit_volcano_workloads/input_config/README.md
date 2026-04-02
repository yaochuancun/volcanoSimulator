# input_config

Sample **cluster**, **workload**, and **plugins** inputs. Each file lives in its own subdirectory:

| Path | Role |
| --- | --- |
| `cluster/cluster.yaml` | Node list + FlexNPU topology |
| `workload/workload.yaml` | Volcano JobList + `npuGranularityPercent`（仅对 flexnpu_core 上取整） |
| `plugins/plugins.yaml` | Scheduler plugin tiers + `output` (semantic only for HTTP mode) |
| `plugins/plugins_mvp.yaml` | MVP sample **without `gang`** (matches current simulator scope) |

JSON Schemas for IDE validation: [`../schemas/`](../schemas/).
