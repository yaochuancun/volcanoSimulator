from common.utils.json_http_client import JsonHttpClient
import time
import datetime
import csv
import json
import munch
import os
import math
import shutil
from json import dumps
#from figures.draw_pod_figures import draw_pods_figures
import prettytable
from figures.figures import draw_job_figures
from figures.figures import draw_job_figures1
from figures.figures import draw_job_figures2
import matplotlib.pyplot as plt
import numpy as np


def _plot_style_context():
    """Use IEEE style if registered (e.g. pip install scienceplots + import); else default.

    Note: plt.style.context('ieee') only fails inside __enter__, so we must check availability first.
    """
    try:
        import scienceplots  # noqa: F401 — registers 'ieee' in plt.style.available when installed
    except ImportError:
        pass
    if "ieee" in plt.style.available:
        return plt.style.context("ieee")
    return plt.style.context("default")


def _get_key_or_empty(data, key):
    pods = munch.munchify(data[key])
    return pods if pods is not None else []

def reset(sim_base_url, node_file_url, workload_file_url):
    client = JsonHttpClient(sim_base_url)

    with open(node_file_url, 'r', encoding='utf-8') as file:
        nodes_file = file.read()

    with open(workload_file_url, 'r', encoding='utf-8') as file:
        workload_file = file.read()

    dicData = client.get_json('/reset', json={
        'period': "-1",
        'nodes': nodes_file,
        'workload': workload_file,
    })

    if str(dicData) == "0":
        print("still job runs，can not reset")
    else:
        print("---Simualtion Reset---")
        #print("仿真环境node信息：")
        #print(dumps(dicData["nodes"], indent=4))
        #print(_get_key_or_empty(dicData, "nodes"))

def step(sim_base_url, conf_file_url, pods_result_url, jobs_result_url, figures_result_url, scheduler):

    with _plot_style_context():

        client = JsonHttpClient(sim_base_url)

        task_headers = ["Pod_name", "Job_name", "Phase", "NodeName"]
        phase_summary_path_name = "pod_phase_count.txt"

        with open(conf_file_url, 'r', encoding='utf-8') as file:  # conf2是nodeorder
            conf_file = file.read()
        data = client.get_json('/step', json={
            'conf': conf_file,
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
        tail_allpodruntime1 = []
        colorid = []
        save_filename = scheduler + ".pdf"
        if len(allpodruntime) > 0:
            tail_allpodruntime2 = sorted(allpodruntime)[int(len(allpodruntime) * math.pow(0.99, len(alljoblists))):len(allpodruntime)]
            task_id = []
            for i, podruntime in enumerate(allpodruntime):
                if podruntime in tail_allpodruntime2:
                    colorid.append('red')
                else:
                    colorid.append('steelblue')
                task_id.append(i)
            print("Task completed%-1：", tail_allpodruntime1)
            print("Task completed%-2：", tail_allpodruntime2)
            task_id.reverse()
            plt.barh(range(len(allpodruntime)), allpodruntime, color=colorid, tick_label=task_id, height=0.2)
            plt.yticks(np.arange(len(allpodruntime)), ())
            plt.xticks(size=8, font='Times New Roman')
            plt.xlim(0)
        else:
            plt.figure(figsize=(4, 2))
            plt.text(0.5, 0.5, f"Pending={pending_count}  Running={running_count}", ha='center', va='center', fontsize=10)
            plt.axis('off')
        plt.savefig(os.path.join(figures_result_url, save_filename), dpi=1600)
        plt.show()

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
    node_file_url = 'common/nodes/nodes_7-0.yaml'
    workload_file_url = 'common/workloads/AI-workloads/wsl_test_mrp-2.yaml'

    if os.path.exists(os.path.join(os.getcwd(), "volcano-sim-result/")):
        shutil.rmtree(os.path.join(os.getcwd(), "volcano-sim-result/"))
    os.makedirs(os.path.join(os.getcwd(), "volcano-sim-result/"), exist_ok=False)
    print("Delete history folder！！！\n")

    for i in range(1):
        print("**************************************************** " + str(i+1) + " test: ****************************************************")

        # schedulers = ["GANG_LRP", "GANG_MRP", "GANG_BRA", "SLA_LRP", "SLA_MRP", "SLA_BRA",
        #               "GANG_DRF_LRP", "GANG_DRF_MRP", "GANG_DRF_BRA", "GANG_BINPACK", "SLA_BINPACK", "GANG_DRF_BINPACK"]

        # schedulers = ["GANG_LRP", "GANG_MRP", "GANG_BRA", "SLA_LRP", "SLA_MRP", "SLA_BRA",
        #               "GANG_DRF_LRP", "GANG_DRF_MRP", "GANG_DRF_BRA", "GANG_BINPACK", "SLA_BINPACK", "GANG_DRF_BINPACK", "Default"]

        #schedulers = ["GANG_BINPACK", "GANG_LRP", "GANG_MRP", "GANG_BRA", "DRF_BINPACK", "DRF_LRP", "DRF_MRP", "DRF_BRA", "SLA_BINPACK", "SLA_LRP", "SLA_MRP", "SLA_BRA"]
        #schedulers = ["GANG_BINPACK", "DRF_BINPACK", "SLA_BINPACK"]
        # schedulers = ["SLA_LRP", "SLA_MRP", "SLA_BRA", "DRF_LRP", "DRF_MRP", "DRF_BRA", "GANG_LRP", "GANG_MRP", "GANG_BRA",
        #               "GANG_DRF_LRP", "GANG_DRF_MRP", "GANG_DRF_BRA", "GANG_DRF_BINPACK"]
        schedulers = ["GANG_LRP", "GANG_MRP", "GANG_BRA"]
        for scheduler in schedulers:
            now = datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
            # conf_file_url = 'common/scheduler_conf/conf_1.yaml'
            conf_file_url = 'common/scheduler_conf_sim/' + str(scheduler) + '.yaml'
            pods_result_url = "volcano-sim-result/tasks/" + str(now) + "-" + str(scheduler)
            jobs_result_url = "volcano-sim-result/jobs/" + str(now) + "-" + str(scheduler)
            figures_result_url = "volcano-sim-result/figures/" + str(now) + "-" + str(scheduler)

            os.makedirs(pods_result_url, exist_ok=True)
            os.makedirs(jobs_result_url, exist_ok=True)
            os.makedirs(figures_result_url, exist_ok=True)

            print("-----------------------------------------------------------------")
            print("In scheduling algorithm: " + str(scheduler) + "， simulation test：")
            reset(sim_base_url, node_file_url, workload_file_url)
            time.sleep(1)
            step(sim_base_url, conf_file_url, pods_result_url, jobs_result_url, figures_result_url, scheduler)
            time.sleep(1)
            #draw_pods_figures(os.path.join(pods_result_url, 'tasksSUM.csv'), figures_result_url, scheduler)
            print("-----------------------------------------------------------------")
            print("")

    print("****************************************************！！！Simulation Stop！！！****************************************************")

    time.sleep(1)
    # draw_job_figures(
    #     'volcano-sim-result/jobs',
    #     'volcano-sim-result/figures/box'
    # )
    # draw_job_figures1(
    #     'volcano-sim-result/jobs',
    #     'volcano-sim-result/figures/avg'
    # )
    draw_job_figures2(
        'volcano-sim-result/jobs',
        'volcano-sim-result/figures/JCTmakespan'
    )
