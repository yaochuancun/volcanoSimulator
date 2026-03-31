"""从 JSON 文件或 MySQL（Alibaba 轨迹库）读取作业/实例数据，用于构造仿真 workload 的辅助脚本。"""

import json
import pymysql


def read_json_file(filename):
    """读取 JSON 文件并返回解析后的对象。"""
    with open(filename, "r") as fp:
        data = json.load(fp)
        return data

def read_sql_file(tracetimeid:int,workloadtypeid:int,jobconsist_tasknumber:int,job_tasknum:int,job_num:int):
    """按轨迹表与时间片筛选 Job，再按 CPU/内存/workload 类型过滤实例，组装为 job.tasks 列表结构。"""
    # 以下为查询过滤用的占位参数（部分逻辑已注释为固定阈值）
    requestcpu_min = 0
    requestcpu_max = 0
    requestmem_min = 0.0
    requestmem_max = 0.0
    instancerunningtime_min = 0
    instancerunningtime_max = 0

    # 连接 MySQL（主机与库名需与部署环境一致）
    connection = pymysql.connect(host='10.4.21.109', user='root', password='abcd2439774702', db='alibaba')
    cursor = connection.cursor()
    print('Connect mysql succeed!')

    # 先筛出满足 JCT 条件的 job_name
    sql1 = 'select job_name from getjob_5_modified_%d where jct > %d' % (tracetimeid, job_tasknum)
    cursor.execute(sql1)
    result1 = cursor.fetchall()
    print("SQL job number: ",len(result1))
    print("-----------------------------")

    # 再按 job_name 拉取实例行，按 workload 类型过滤
    alljobdict = []
    for i, row1 in enumerate(result1):

        # 按 job 名称关联实例表
        sql2 = "select job_name,start_time,end_time,cpu_avg,cpu_max,mem_avg,mem_max from batch_instance_2_%d " \
               "where job_name='%s'" % (tracetimeid, row1[0])
        cursor.execute(sql2)
        result2 = cursor.fetchall()

        # 聚合该 job 下通过筛选的实例
        singlejobdict = {}
        singlejobdict['job.tasks'] = []
        for j, row2 in enumerate(result2):

            # 实例字段：起止时间、CPU/内存请求与上限等
            jobname = row2[0]
            instance_starttime = int(row2[1])
            instance_endtime = int(row2[2])
            request_cpu = int(row2[3])
            limit_cpu = int(row2[4])
            request_mem = float(row2[5])
            limit_mem = float(row2[6])
            instance_runningtime = instance_endtime - instance_starttime

            # workloadtypeid 1：低 CPU、低内存
            if workloadtypeid == 1:
                if (request_cpu > 10 and request_cpu <= 50) and (request_mem <= 0.05) and (instance_runningtime >= 20):
                #if ((request_cpu >= requestcpu_min and request_cpu < requestcpu_max) and (request_mem >= requestmem_min and request_mem < requestmem_max)
                #        and (instance_runningtime >= instancerunningtime_min and instance_runningtime < instancerunningtime_max)):
                    singlejobdict['job.tasks'].append(row2)

            # workloadtypeid 2：低 CPU、高内存
            if workloadtypeid == 2:
                if (request_cpu > 10 and request_cpu <= 50) and (request_mem >= 0.7 and request_mem <= 0.8) and (instance_runningtime >= 20):
                #if ((request_cpu >= requestcpu_min and request_cpu < requestcpu_max) and (request_mem >= requestmem_min and request_mem < requestmem_max)
                #        and (instance_runningtime >= instancerunningtime_min and instance_runningtime < instancerunningtime_max)):
                    singlejobdict['job.tasks'].append(row2)

            # workloadtypeid 3：高 CPU、低内存
            if workloadtypeid == 3:
                if (request_cpu > 100 and request_cpu < 200) and (request_mem <= 0.05) and (instance_runningtime >= 20):
                #if ((request_cpu >= requestcpu_min and request_cpu < requestcpu_max) and (request_mem >= requestmem_min and request_mem < requestmem_max)
                #        and (instance_runningtime >= instancerunningtime_min and instance_runningtime < instancerunningtime_max)):
                    singlejobdict['job.tasks'].append(row2)

            # workloadtypeid 4：高 CPU、高内存
            if workloadtypeid == 4:
                if (request_cpu > 100 and request_cpu < 200) and (request_mem >= 0.7 and request_mem <= 0.8) and (instance_runningtime >= 20):
                    singlejobdict['job.tasks'].append(row2)
                #if ((request_cpu >= requestcpu_min and request_cpu < requestcpu_max) and (request_mem >= requestmem_min and request_mem < requestmem_max)
                #        and (instance_runningtime >= instancerunningtime_min and instance_runningtime < instancerunningtime_max)):

        if (len(singlejobdict['job.tasks']) >= jobconsist_tasknumber):
            alljobdict.append(singlejobdict)

        if (len(alljobdict) == job_num + 2):
            break

    return alljobdict









