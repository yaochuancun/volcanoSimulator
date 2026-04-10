"""Read Alibaba trace subset from MySQL; aggregate task runtime, CPU, memory, etc. (standalone script; needs DB access)."""

import json
import pymysql


def read_json_file(filename):
    """Read a JSON file."""
    with open(filename, "r") as fp:
        data = json.load(fp)
        return data

def read_sql_file(cursor):
    """Query job and instance tables with an open cursor; filter jobs with low CPU, low memory, and enough tasks."""
    # Connection owned by caller; only execute queries here
    #connection = pymysql.connect(host='10.4.21.218', user='root', password='123', db='alibaba trace')
    #cursor = connection.cursor()
    #print('Connect mysql succeed!')

    # Jobs with many tasks
    sql1 = "select * from getjob_0_modified where job_tasknumber > %d" % 10000
    cursor.execute(sql1)
    result1 = cursor.fetchall()
    print("SQL job number: ",len(result1))

    # Per job: fetch instances and filter by resource profile
    alljobdict = []
    for i, row1 in enumerate(result1):

        # Instances by job_name
        sql2 = "select * from batch_instance_1_0 where job_name='%s'" % row1[0]
        cursor.execute(sql2)
        result2 = cursor.fetchall()

        # Build instance list for this job
        podlist = {}
        podlist['job.tasks'] = []
        for j, row2 in enumerate(result2):

            # Parse instance row (columns used in filter)
            request_cpu = int(row2[10])
            request_mem = float(row2[12])

            # Example filter: medium-high CPU request + low memory
            if (request_cpu >= 100 and request_cpu < 150) and (request_mem < 0.1):
                podlist['job.tasks'].append(row2)

        if(len(podlist['job.tasks']) >= 6):
            alljobdict.append(podlist)

    return alljobdict

def avg(data: list):
    """Arithmetic mean of a list."""
    return sum(data) / len(data)


if __name__ == '__main__':
    # Example: connect to DB and print stats
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

    # Close connection
    print("-----------------------------------------------------------------------")
    cursor.close()
    connection.close()
    print("Close mysql succeed!")
    print("-----------------------------------------------------------------------")
    print("-----------------------------------------------------------------------")







