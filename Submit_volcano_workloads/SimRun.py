from common.utils.json_http_client import JsonHttpClient
from input_config.flexnpu_util_report import print_flexnpu_utilization
from input_config.output_csv_reports import write_output_config_csvs
from input_config.input_config_loader import (
    load_cluster_for_simulator,
    load_plugins_for_simulator,
    load_workload_for_simulator,
)
import time
import datetime
import csv
import json
import munch
import os
from json import dumps
#from figures.draw_pod_figures import draw_pods_figures
import prettytable


def _get_key_or_empty(data, key):
    pods = munch.munchify(data[key])
    return pods if pods is not None else []

def reset(sim_base_url, nodes_yaml, workload_yaml):
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

def step(sim_base_url, scheduler_conf_yaml, pods_result_url, jobs_result_url):

    client = JsonHttpClient(sim_base_url)

    task_headers = ["Pod_name", "Job_name", "Phase", "NodeName"]
    phase_summary_path_name = "pod_phase_count.txt"

    data = client.get_json('/step', json={
        'conf': scheduler_conf_yaml,
    })

    wait = 0.2
    alljoblists = []
    pending_count = 0
    running_count = 0
    other_phase_count = 0
    allpodruntime = []
    succeed_table = prettytable.PrettyTable(task_headers)
    countJct = [0]
    while True:
        # 等待一段时间后获取集群信息，若返回1则表示集群未调度满一个周期，再等等
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

            output_config_dir = os.path.join(pods_result_url, "output_config")
            os.makedirs(output_config_dir, exist_ok=True)
            write_output_config_csvs(resultdata, output_config_dir)

            job_result = os.path.join(jobs_result_url, 'coutJCT.csv')
            with open(job_result, "w", encoding='utf-8', newline='') as file1:
                writer1 = csv.writer(file1)
                writer1.writerow(['Job Name', 'Job Completed Time(s)'])
                writer1.writerow(['N/A', 'not applicable (pods remain Pending/Running)'])

            countJct = [0]
            alljoblists.append(['summary', 0])
            break

    JCT_table = prettytable.PrettyTable(['Job Name', 'Job Completed Time(s)'])
    JCT_table.add_row(['N/A', 'not applicable (pods remain Pending/Running)'])

    print(JCT_table)

    pod_result_1 = os.path.join(pods_result_url, 'tasksSUM.md')
    file2 = open(pod_result_1, 'w', encoding='utf-8')
    file2.write(str(succeed_table) + "\n")
    file2.write(f"Pending: {pending_count}  Running: {running_count}  Other: {other_phase_count}\n")
    file2.write('Total%d个Task。\n' % (pending_count + running_count + other_phase_count))
    if len(allpodruntime) > 0:
        file2.write('Task average time：%.2fs，Minimum time：%.2fs，Maximum time：%.2fs。 \n' % (
            sum(allpodruntime) / len(allpodruntime), min(allpodruntime), max(allpodruntime)))
    else:
        file2.write('（当前仿真不统计 task 运行时长）\n')

    job_result_1 = os.path.join(jobs_result_url, 'coutJCT.md')
    file3 = open(job_result_1, 'w', encoding='utf-8')
    file3.write(str(JCT_table) + "\n")
    file3.write('Summary: ' + "\n")
    file3.write('Total%d个Job。\n' % (len(countJct)))
    if countJct and countJct != [0]:
        file3.write('Job average time：%.2fs，Minimum time：%.2fs，Maximum time：%.2fs。\n' % (
            sum(countJct) / len(countJct), min(countJct), max(countJct)))
        file3.write('Jobs MakeSpan is：%.2fs。\n' % max(countJct))
    else:
        file3.write('JCT 未计算（Pod 保持 Pending/Running）。\n')

    time.sleep(0.5)
    file2.close()
    file3.close()

if __name__ == '__main__':

    sim_base_url = 'http://localhost:8006'
    _base_dir = os.path.dirname(os.path.abspath(__file__))
    cluster_path = os.path.join(_base_dir, 'input_config', 'cluster', 'cluster.yaml')
    workload_path = os.path.join(_base_dir, 'input_config', 'workload', 'workload.yaml')
    plugins_path = os.path.join(_base_dir, 'input_config', 'plugins', 'plugins.yaml')

    nodes_yaml = load_cluster_for_simulator(cluster_path)
    workload_yaml = load_workload_for_simulator(workload_path)
    scheduler_conf_yaml, result_root = load_plugins_for_simulator(plugins_path)

    now = datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
    pods_result_url = os.path.join(result_root, 'tasks', now)
    jobs_result_url = os.path.join(result_root, 'jobs', now)
    os.makedirs(pods_result_url, exist_ok=True)
    os.makedirs(jobs_result_url, exist_ok=True)

    print("Cluster config:", cluster_path)
    print("Workload config:", workload_path)
    print("Plugins config:", plugins_path)
    print("Result root:", result_root)
    print()

    print("**************************************************** simulation run ****************************************************")
    print("-----------------------------------------------------------------")
    reset(sim_base_url, nodes_yaml, workload_yaml)
    time.sleep(1)
    step(sim_base_url, scheduler_conf_yaml, pods_result_url, jobs_result_url)
    time.sleep(1)
    print("-----------------------------------------------------------------")

    print("****************************************************！！！Simulation Stop！！！****************************************************")

    time.sleep(1)
