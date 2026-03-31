"""从 MySQL 读取 Alibaba 轨迹子集并统计任务运行时间、CPU、内存等（独立脚本，需可连库环境）。"""

import json
import pymysql


def read_json_file(filename):
    """读取 JSON 文件。"""
    with open(filename, "r") as fp:
        data = json.load(fp)
        return data

def read_sql_file(cursor):
    """使用已打开的 cursor 查询 job 与实例表，筛选低 CPU+低内存且任务数足够的 job。"""
    # 连接由调用方建立；此处仅执行查询
    #connection = pymysql.connect(host='10.4.21.218', user='root', password='123', db='alibaba trace')
    #cursor = connection.cursor()
    #print('Connect mysql succeed!')

    # 筛选 task 数较多的 job
    sql1 = "select * from getjob_0_modified where job_tasknumber > %d" % 10000
    cursor.execute(sql1)
    result1 = cursor.fetchall()
    print("SQL job number: ",len(result1))

    # 逐 job 拉实例并过滤资源画像
    alljobdict = []
    for i, row1 in enumerate(result1):

        # 按 job_name 查实例
        sql2 = "select * from batch_instance_1_0 where job_name='%s'" % row1[0]
        cursor.execute(sql2)
        result2 = cursor.fetchall()

        # 组装该 job 的实例列表
        podlist = {}
        podlist['job.tasks'] = []
        for j, row2 in enumerate(result2):

            # 解析实例行字段（仅筛选条件用到的列）
            request_cpu = int(row2[10])
            request_mem = float(row2[12])

            # 示例筛选：中高 CPU 请求 + 低内存
            if (request_cpu >= 100 and request_cpu < 150) and (request_mem < 0.1):
                podlist['job.tasks'].append(row2)

        if(len(podlist['job.tasks']) >= 6):
            alljobdict.append(podlist)

    return alljobdict

def avg(data: list):
    """列表算术平均。"""
    return sum(data) / len(data)


if __name__ == '__main__':
    # 直连示例库并打印统计
    print("-----------------------------------------------------------------------")
    print("-----------------------------------------------------------------------")
    connection = pymysql.connect(host='10.4.21.218', user='root', password='123', db='alibaba trace')
    cursor = connection.cursor()
    print('Connect mysql succeed!')

    data = read_sql_file(cursor)
    print('job cnt:', len(data))
    print('job tasks:', [len(job['job.tasks']) for job in data])

    running_time_s = []
    start_time_ms = []
    cpu, max_cpu, ram, max_ram = [], [], [], []
    for job in data:
        for task in job['job.tasks']:
            #print(task)
            running_time_s.append(int(task[6]) - int(task[5]))
            start_time_ms.append(int(task[5]))
            cpu.append(float(task[10]/100))
            max_cpu.append(float(task[11]/100))
            ram.append(int(task[12]*1024))
            max_ram.append(int(task[13]*1024))

    print("-----------------------------------------------------------------------")
    print('task avg running time(s): ', avg(running_time_s), ', max: ', max(running_time_s), ', min:', min(running_time_s))
    print('task avg cpu(core): ', avg(cpu), ', max: ', max(cpu), ', min: ', min(cpu))
    print('task avg max_cpu(core): ', avg(max_cpu), ', max: ', max(max_cpu), ', min: ', min(max_cpu))
    print('task avg ram(MB): ', avg(ram), ', max: ', max(ram), ', min: ', min(ram))
    print('task avg max_ram(MB): ', avg(max_ram), ', max: ', max(max_ram), ', min: ', min(max_ram))

    # 关闭连接
    print("-----------------------------------------------------------------------")
    cursor.close()
    connection.close()
    print("Close mysql succeed!")
    print("-----------------------------------------------------------------------")
    print("-----------------------------------------------------------------------")







