"""Volcano 仿真器客户端入口：通过 HTTP 调用 /reset、/step、/stepResult，并将结果写入本地目录。

配置来自 input_config 下的 cluster / workload / plugins YAML；产物（CSV、FlexNPU 报表等）
写入 plugins 中 output.outDir 解析后的路径。
"""

from common.utils.json_http_client import JsonHttpClient
from input_config.flexnpu_util_report import print_flexnpu_utilization
from input_config.output_csv_reports import write_output_config_csvs
from input_config.input_config_loader import (
    load_cluster_for_simulator,
    load_plugins_for_simulator,
    load_workload_for_simulator,
)
import time
import csv
import munch
import os
#from figures.draw_pod_figures import draw_pods_figures
import prettytable


def _get_key_or_empty(data, key):
    """从响应字典取 key，转为 munch 对象；缺失或 None 时返回空列表。"""
    pods = munch.munchify(data[key])
    return pods if pods is not None else []


def reset(sim_base_url, nodes_yaml, workload_yaml):
    """调用仿真器 /reset，下发节点与作业负载 YAML，初始化一次仿真环境。"""
    client = JsonHttpClient(sim_base_url)

    dicData = client.get_json('/reset', json={
        'period': "-1",
        'nodes': nodes_yaml,
        'workload': workload_yaml,
    })

    if str(dicData) == "0":
        print("still job runs，can not reset")
    else:
        print("---Simualtion Reset---")
        #print("仿真环境node信息：")
        #print(dumps(dicData["nodes"], indent=4))
        #print(_get_key_or_empty(dicData, "nodes"))

def step(sim_base_url, scheduler_conf_yaml, pods_result_url):
    """调用 /step 推进调度，轮询 /stepResult 直至有有效快照；将 Pod 列表、阶段统计、
    FlexNPU 利用率及统计 CSV 写入 ``pods_result_url`` 目录。
    """
    client = JsonHttpClient(sim_base_url)

    task_headers = ["Pod_name", "Job_name", "Phase", "NodeName"]
    phase_summary_path_name = "pod_phase_count.txt"

    client.get_json('/step', json={
        'conf': scheduler_conf_yaml,
    })

    wait = 0.2
    pending_count = 0
    running_count = 0
    other_phase_count = 0
    succeed_table = prettytable.PrettyTable(task_headers)
    while True:
        # 等待后拉取集群快照；若仍为 '0' 表示本周期尚未结束，继续轮询
        time.sleep(wait)
        resultdata = client.get_json('/stepResult', json={
            'none': "",
        })
        #print("11111: \n", resultdata)
        if str(resultdata) == '0':
            continue
        else:
            print("---Simulation Start---")
            pod_result = os.path.join(pods_result_url, 'tasksSUM.csv')
            phase_summary_path = os.path.join(pods_result_url, phase_summary_path_name)

            pending_count = 0
            running_count = 0
            other_phase_count = 0
            rows_out = []
            succeed_table = prettytable.PrettyTable(task_headers)

            for jobName, job in resultdata["Jobs"].items():
                for taskName, task in job["Tasks"].items():
                    pod = task.get("Pod") or {}
                    status = pod.get("status") or {}
                    phase = status.get("phase") or ""
                    node = task.get("NodeName") or ""
                    labels = (pod.get("metadata") or {}).get("labels") or {}
                    job_label = labels.get("job", jobName)
                    rows_out.append([taskName, job_label, phase, node])
                    succeed_table.add_row([taskName, job_label, phase, node])
                    if phase == "Pending":
                        pending_count += 1
                    elif phase == "Running":
                        running_count += 1
                    else:
                        other_phase_count += 1

            with open(pod_result, "w", encoding='utf-8', newline='') as file0:
                writer = csv.writer(file0)
                writer.writerow(task_headers)
                writer.writerows(rows_out)

            total_pods = pending_count + running_count + other_phase_count
            summary_lines = [
                f"Pending: {pending_count}",
                f"Running: {running_count}",
            ]
            if other_phase_count:
                summary_lines.append(f"Other phase: {other_phase_count}")
            summary_lines.append(f"Total pods: {total_pods}")
            summary_text = "\n".join(summary_lines) + "\n"

            with open(phase_summary_path, "w", encoding="utf-8") as sf:
                sf.write(summary_text)

            print(summary_text)
            print(succeed_table)

            flexnpu_txt = print_flexnpu_utilization(resultdata)
            with open(
                os.path.join(pods_result_url, "flexnpu_utilization.txt"),
                "w",
                encoding="utf-8",
            ) as flex_fp:
                flex_fp.write(flexnpu_txt)

            write_output_config_csvs(resultdata, pods_result_url)

            break

    JCT_table = prettytable.PrettyTable(['Job Name', 'Job Completed Time(s)'])
    JCT_table.add_row(['N/A', 'not applicable (pods remain Pending/Running)'])

    print(JCT_table)

    time.sleep(0.5)

if __name__ == '__main__':
    # 默认连本地仿真服务；结果目录由 plugins.yaml 的 output.outDir（含 {date}）决定

    sim_base_url = 'http://localhost:8006'
    _base_dir = os.path.dirname(os.path.abspath(__file__))
    cluster_path = os.path.join(_base_dir, 'input_config', 'cluster', 'cluster_1.yaml')
    workload_path = os.path.join(_base_dir, 'input_config', 'workload', 'workload_1.yaml')
    plugins_path = os.path.join(_base_dir, 'input_config', 'plugins', 'plugins_mvp.yaml')

    nodes_yaml = load_cluster_for_simulator(cluster_path)
    workload_yaml = load_workload_for_simulator(workload_path)
    scheduler_conf_yaml, result_root = load_plugins_for_simulator(plugins_path)

    os.makedirs(result_root, exist_ok=True)
    pods_result_url = result_root

    print("Cluster config:", cluster_path)
    print("Workload config:", workload_path)
    print("Plugins config:", plugins_path)
    print("Result root:", result_root)
    print()

    print("**************************************************** simulation run ****************************************************")
    print("-----------------------------------------------------------------")
    reset(sim_base_url, nodes_yaml, workload_yaml)
    time.sleep(1)
    step(sim_base_url, scheduler_conf_yaml, pods_result_url)
    time.sleep(1)
    print("-----------------------------------------------------------------")

    print("****************************************************！！！Simulation Stop！！！****************************************************")

    time.sleep(1)
