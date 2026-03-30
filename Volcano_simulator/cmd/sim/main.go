package main

import (
	"encoding/json"
	"fmt"
	"io/ioutil"
	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"net/http"
	"strconv"
	"time"
	"volcano.sh/apis/pkg/apis/scheduling"
	"volcano.sh/volcano/cmd/sim/app/options"
	"volcano.sh/volcano/pkg/kube"
	"volcano.sh/volcano/pkg/scheduler"
	"volcano.sh/volcano/pkg/scheduler/actions"
	schedulingapi "volcano.sh/volcano/pkg/scheduler/api"
	"volcano.sh/volcano/pkg/scheduler/conf"
	"volcano.sh/volcano/pkg/scheduler/framework"
	"volcano.sh/volcano/pkg/scheduler/util"
	"volcano.sh/volcano/pkg/simulator"
)



var(
	loadNewSchedulerConf = true //用于标记是否已经接收到新的schedulerConf
	notCompletion = false //用于表示是否所有job都完成了
	restartFlag = true //表示正在reset
	cnt = int64(0) //循环次数
	period = int64(-1) //表示多少次循环（秒）获取一次scheduler conf，-1表示除了开始阶段以外不加载conf
	acts []framework.Action
	tiers []conf.Tier
	cfg []conf.Configuration
	cluster = &schedulingapi.ClusterInfo{ //创建cluster
		Nodes:          make(map[string]*schedulingapi.NodeInfo),
		Jobs:           make(map[schedulingapi.JobID]*schedulingapi.JobInfo),
		Queues:         make(map[schedulingapi.QueueID]*schedulingapi.QueueInfo),
		NamespaceInfo:  make(map[schedulingapi.NamespaceName]*schedulingapi.NamespaceInfo),
		RevocableNodes: make(map[string]*schedulingapi.NodeInfo),
	}
	jobQueue = util.NewPriorityQueue(func(l interface{}, r interface{}) bool { //用来按时间提交jobInfo，不是k8s中的数据结构
		lv := l.(*schedulingapi.JobInfo)
		rv := r.(*schedulingapi.JobInfo)
		return lv.SubTimestamp.Time.Before(rv.SubTimestamp.Time)
	})
	defaultQueue *scheduling.Queue //k8s中的queue

	startSimulate time.Time
	simulationTime time.Time
)

/*
一些说明:
1、未调度及 Binding（已绑节点、容器创建中）对外 Pod Phase 均为 Pending；Task 进入 Running 后对外为 Running，且不再因“工作量完成”变为 Succeeded。
2、Binding 表示调度已绑定节点、容器创建中；Task 进入 Running 后表示容器已就绪。
3、仿真结束条件：待提交队列为空且无仍处于 Binding 的任务（容器创建队列跑完）。
*/
func syncSimulationPodPhases() {
	for _, job := range cluster.Jobs {
		for _, task := range job.Tasks {
			switch task.Status {
			case schedulingapi.Pending, schedulingapi.Pipelined, schedulingapi.Binding:
				task.Pod.Status.Phase = v1.PodPending
			default:
				task.Pod.Status.Phase = v1.PodRunning
			}
		}
	}
}

func clusterHasBindingTask() bool {
	for _, job := range cluster.Jobs {
		for _, task := range job.Tasks {
			if task.Status == schedulingapi.Binding {
				return true
			}
		}
	}
	return false
}

func main() {

	var jsonDefaultQueue = []byte(`{
  "apiVersion": "scheduling.volcano.sh/v1beta1",
  "kind": "Queue",
  "generation": 1,
  "name": "default",
  "spec": {
    "reclaimable": true,
    "weight": 1
  },
  "status": {
    "state": "Open"
  }
}`)


	opts := &options.ServerOption{
		SchedulerName:  "volcano",
		SchedulePeriod: 5 * time.Minute,
		DefaultQueue:   "default",
		ListenAddress:  ":8080",
		KubeClientOptions: kube.ClientOptions{
			Master:     "",
			KubeConfig: "",
			QPS:        2000.0,
			Burst:      2000,
		},
		PluginsDir:                 "",
		HealthzBindAddress:         ":11251",
		MinNodesToFind:             100, //这些参数和pkg>scheduler>utils>scheduler_helper.go里的 CalculateNumOfFeasibleNodesToFind有关
		MinPercentageOfNodesToFind: 5,
		PercentageOfNodesToFind:    100,
	}
	opts.RegisterOptions() //将以上参数注册（设置为全局变量）


	var err error
	err, defaultQueue = simulator.Json2Queue(jsonDefaultQueue)
	if err != nil {
		fmt.Println("error:", err)
	}


	queueInfo := schedulingapi.NewQueueInfo(defaultQueue)

	namespaceInfo := &schedulingapi.NamespaceInfo{
		Name:   schedulingapi.NamespaceName("default"),
		Weight: 1,
	}

	actions.InitV2()                                                                   //注册插件，否则UnmarshalSchedulerConfV2无法运行

	cluster.Queues[queueInfo.UID] = queueInfo //将queue信息加入到cluster中
	cluster.NamespaceInfo[namespaceInfo.Name] = namespaceInfo //将namespace信息加入到cluster中

	go server() //用于监听发到后端的信息，完成上述初始化再开始监视

	fmt.Print("simulator start...")

	//一个循环1秒
	for true{

		for !notCompletion || restartFlag { //无job 或 等待reset，程序一开始会停在这，其执行到 等待加载conf 需要一点时间，因此reset后不能马上step
			time.Sleep(time.Duration(0.2*1e9))
		}

		//fmt.Println(schedulingapi.NowTime)

		//提交到时间的job
		for !jobQueue.Empty(){
			front:= jobQueue.Pop().(*schedulingapi.JobInfo)
			if schedulingapi.NowTime.Time.Before(front.SubTimestamp.Time) { //“当前时间”在“sub时间”之前
				jobQueue.Push(front)
				break
			} else{
				cluster.Jobs[front.UID] = front //提交jobInfo
				//fmt.Println(schedulingapi.NowTime,": submit",front.Name)

				//若job提交就设置创建时间，pod处于pending状态就有创建时间了
				for _, task := range front.Tasks {
					task.Pod.SetCreationTimestamp(schedulingapi.NowTime) //设置pod创建时间，1e9为1秒
				}
			}
		}

		//发现同时创建的pod越多，则所用时间越长，因此有以下两段

		//遍历task，把binding task的container创建倒计时减小
		for _, node := range cluster.Nodes {
			for _, task := range node.Tasks {
				if task.Status != schedulingapi.Binding {
					continue
				}
				task.CtnCreationCountDown -= 1
			}
		}

		//对于每个node，每隔interval，遍历task，找出最早创建的binding task改为running task
		for _, node := range cluster.Nodes {
			if node.CtnCreationTimeInterval!=0 && cnt%node.CtnCreationTimeInterval != 0{
				continue
			}
			findFlag := false
			var selectTask *schedulingapi.TaskInfo
			for _, task := range node.Tasks {
				if task.Status != schedulingapi.Binding {
					continue
				}
				if task.CtnCreationCountDown>0 {
					continue
				}
				if !findFlag{
					selectTask = task
					findFlag = true
					continue
				}
				if task.Pod.CreationTimestamp.Before(&selectTask.Pod.CreationTimestamp){
					selectTask = task
				}
			}
			if findFlag{
				fmt.Println("create container in",selectTask.NodeName,":",selectTask.Name,schedulingapi.NowTime)
				//更改cluster中task状态，node中task为job中task之前的clone吗？
				selectTask.Pod.Status.Phase = v1.PodRunning
				cluster.Jobs[selectTask.Job].Tasks[selectTask.UID].Pod.Status.Phase = v1.PodRunning

				selectTask.Status = schedulingapi.Running
				cluster.Jobs[selectTask.Job].Tasks[selectTask.UID].Status = schedulingapi.Running

				selectTask.Pod.Status.StartTime =  schedulingapi.NowTime.DeepCopy()
				cluster.Jobs[selectTask.Job].Tasks[selectTask.UID].Pod.Status.StartTime  =  schedulingapi.NowTime.DeepCopy()
				//todo 还要改job.TaskStatusIndex
				//delete(cluster.Jobs[task.Job].TaskStatusIndex[schedulingapi.Binding], task.UID)
			}

		}

		//刚reset 或 够一个周期了，等待新的step（scheduler conf）
		if (cnt == 0) || (period!=-1 && cnt%period == 0)  {
			loadNewSchedulerConf = false
			fmt.Println("wait for conf...")
		}



		for !loadNewSchedulerConf{

			time.Sleep(time.Duration(1e9))
		}

		if restartFlag{
			continue
		}

		//调度
		ssn := framework.OpenSessionV2(cluster, tiers, cfg)
		for _, action := range acts {
			action.Execute(ssn)
			//fmt.Println(action.Name())
		}

		//framework.CloseSession(ssn) //会报错

		syncSimulationPodPhases()

		// 回合稳定：待提交队列为空且无仍处于 Binding（容器创建中）的任务
		notCompletion = clusterHasBindingTask() || !jobQueue.Empty()

		//任务完成则
		if !notCompletion{
			//打印运行信息
			fmt.Println(schedulingapi.NowTime,"all complete")
			fmt.Println("simulation time:",simulationTime )
			fmt.Println("---------------------\nNodes:")
			for _, node := range cluster.Nodes {
				//fmt.Println(node.Tasks)
				//for _,task := range node.Tasks{
				//	//fmt.Println(task.Pod.CreationTimestamp)
				//	fmt.Println(task.NodeName)
				//}
				fmt.Println(node.Name,":")
				fmt.Println("task num:",len(node.Tasks))
				//fmt.Println(node.Capability)
				//fmt.Println(node.Allocatable) //没减少
				fmt.Println("Idle:",node.Idle) //减少了
				fmt.Println("Used:",node.Used)
			}
			fmt.Println("---------------------\nJobs:")
			for _, job := range cluster.Jobs {
				//fmt.Println(ssn.JobReady(job))
				for _, task := range job.Tasks {
					fmt.Println(task.Name)
					fmt.Println(task.Status)
					fmt.Println(task.Pod.CreationTimestamp)
					fmt.Println("job-create:",job.CreationTimestamp)
					fmt.Println("sim-end:", task.SimEndTimestamp)
				}
			}
		}

		//时间++
		schedulingapi.NowTime = metav1.NewTime(schedulingapi.NowTime.Add(time.Duration(1e9))) //1e9表示1秒
		cnt += 1
		if cnt%1800 == 0{
			//fmt.Println(cluster.Nodes)
			fmt.Println(schedulingapi.NowTime)
		}

		//todo
		//if cnt%500 == 0{
		//	fmt.Print(simulationTime)
		//	simulationTime = simulationTime.Add(time.Now().Sub(startSimulate))
		//	fmt.Println("->",simulationTime)
		//	fmt.Println("last 500 second:",time.Now().Sub(startSimulate) )
		//
		//	//fmt.Println(cluster.Nodes)
		//	lastId := schedulingapi.JobID(-1)
		//	for id, job := range cluster.Jobs {
		//		if lastId != schedulingapi.JobID(-1){
		//			delete(cluster.Jobs,lastId)
		//			lastId = schedulingapi.JobID(-1)
		//		}
		//		job_finish := true
		//		for _, task := range job.Tasks {
		//			if task.Status != schedulingapi.Succeeded{
		//				job_finish = false
		//				break
		//			}
		//		}
		//		if job_finish{
		//			lastId = id
		//		}
		//
		//	}
		//
		//	jobNum := 0
		//	for _, job := range cluster.Jobs {
		//		for _, task := range job.Tasks {
		//			if task.Status != schedulingapi.Succeeded{
		//				jobNum += 1
		//			}
		//			break //只看一个task
		//		}
		//	}
		//	fmt.Println("all job:",len(cluster.Jobs))
		//	fmt.Println("not finish job:",jobNum)
		//	startSimulate = time.Now()
		//}
	}
}



//用于监听
func reset(w http.ResponseWriter, r *http.Request)  {
	if notCompletion{
		//设置flag并等待程序执行到开头循环处
		restartFlag = true
		loadNewSchedulerConf = true //若在等待加载conf处则让其跳出等待
		time.Sleep(time.Duration(1e9))

		//清空队列，不再提交job
		jobQueue = util.NewPriorityQueue(func(l interface{}, r interface{}) bool { //用来按时间提交jobInfo
			lv := l.(*schedulingapi.JobInfo)
			rv := r.(*schedulingapi.JobInfo)
			return lv.SubTimestamp.Time.Before(rv.SubTimestamp.Time)
		})
	}
	fmt.Println("reset...")

	//重置cluster的Nodes、Jobs、RevocableNodes
	cluster = &schedulingapi.ClusterInfo{ //创建cluster
		Nodes:          make(map[string]*schedulingapi.NodeInfo),
		Jobs:           make(map[schedulingapi.JobID]*schedulingapi.JobInfo),
		Queues:         make(map[schedulingapi.QueueID]*schedulingapi.QueueInfo),
		NamespaceInfo:  make(map[schedulingapi.NamespaceName]*schedulingapi.NamespaceInfo),
		RevocableNodes: make(map[string]*schedulingapi.NodeInfo),
	}

	queueInfo := schedulingapi.NewQueueInfo(defaultQueue)

	namespaceInfo := &schedulingapi.NamespaceInfo{
		Name:   schedulingapi.NamespaceName("default"),
		Weight: 1,
	}

	cluster.Queues[queueInfo.UID] = queueInfo //将queue信息加入到cluster中
	cluster.NamespaceInfo[namespaceInfo.Name] = namespaceInfo //将namespace信息加入到cluster中


	//时间和循环次数设置为0
	cnt = 0
	schedulingapi.NowTime = metav1.NewTime(time.Time{})

	body, err := ioutil.ReadAll(r.Body) //转为字节[]byte
	if err != nil {
		panic(err)
	}

	var workload simulator.WorkloadType
	err = json.Unmarshal(body, &workload) //将字节[]byte读入struct中
	if err != nil {
		panic(err)
	}

	//加载parameters
	period_, err := strconv.Atoi(workload.Period)
	period = int64(period_)
	if err != nil{
		return
	}

	//加载节点信息
	err, nodes := simulator.Yaml2Nodes([]byte(workload.Nodes))
	if err != nil {
		fmt.Println("error:", err)
		return
	}

	for _, node := range nodes["cluster"] { //将集群node信息加入到cluster中
		nodeInfo := schedulingapi.NewNodeInfo(&node.Node)
		cluster.Nodes[nodeInfo.Name] = nodeInfo

		//从发过来的数据中读取，若无则会初始化为0
		if float64(node.CtnCreationTimeInterval)<0.1 && float64(node.CtnCreationExtraTime)<0.1 &&
			float64(node.CtnCreationTime)<0.1 { //default
			nodeInfo.CtnCreationTime = 2
			nodeInfo.CtnCreationExtraTime = 0.5
			nodeInfo.CtnCreationTimeInterval = 1
		}else{
			nodeInfo.CtnCreationTime = node.CtnCreationTime
			nodeInfo.CtnCreationExtraTime = node.CtnCreationExtraTime
			nodeInfo.CtnCreationTimeInterval = node.CtnCreationTimeInterval
		}

		if node.CalculationSpeed < 0.1  { //default
			nodeInfo.CalculationSpeed = 1
		}else{
			nodeInfo.CalculationSpeed = node.CalculationSpeed
		}

		if node.MinimumSpeed < 0.1  { //default
			nodeInfo.MinimumSpeed = -1
		}else{
			nodeInfo.MinimumSpeed = node.MinimumSpeed
		}

		if node.SlowSpeedThreshold < 0.1  { //default
			nodeInfo.SlowSpeedThreshold = -1
		}else{
			nodeInfo.SlowSpeedThreshold = node.SlowSpeedThreshold
		}
	}

	for _,node := range cluster.Nodes{
		fmt.Println(node.Name,":")
		fmt.Println("Allocatable:",node.Allocatable)
		fmt.Println("Capability:",node.Capability)
		fmt.Println("Idle:",node.Idle)
		fmt.Println("Used:",node.Used)
		fmt.Println("Taints:",node.Node.Spec.Taints)
	}

	cluster.NodeList = make([]string, len(cluster.Nodes))
	for _, ni := range cluster.Nodes {
		cluster.NodeList = append(cluster.NodeList, ni.Name)
	}


	//加载job信息
	err, jobs := simulator.Yaml2Jobs([]byte(workload.Workload))
	if err != nil {
		fmt.Println("error:", err)
	}
	for _, job := range jobs["jobs"] { //将job转化为jobInfo，并将jobInfo加入到jobQueue中
		jobInfo := schedulingapi.NewJobInfoV2(job)
		//设置job提交时间和创建时间
		if subTime, found := job.Labels["sub-time"]; found {
			if timestamp,err := strconv.Atoi(subTime); err == nil {
				jobInfo.SubTimestamp = metav1.NewTime(time.Time{}.Add(time.Duration(timestamp*1e9)))
				jobInfo.CreationTimestamp = metav1.NewTime(time.Time{}.Add(time.Duration(timestamp*1e9)))
			}
		}
		//若没有该标签则提交时间默认为0
		jobQueue.Push(jobInfo)
	}
	//fmt.Println(jobs)

	notCompletion = true

	fmt.Println("reset done")

	var v1NodeList []*v1.Node
	for _, node := range cluster.Nodes {
		//修改此处要把stepResult中的一块更改
		//Capacity表示实际使用量
		v1Node := util.BuildNode(node.Name, util.BuildResourceListWithGPU("0", "0Gi", "0"), node.Node.Labels)
		//Allocatable表示实际容量
		v1Node.Status.Allocatable = node.Node.Status.Allocatable
		v1NodeList = append(v1NodeList, v1Node)
	}

	info := simulator.Info{ Done: !notCompletion, V1Nodes: v1NodeList, Clock: schedulingapi.NowTime.Local().String()}
	resp, _ := json.Marshal(info)
	//fmt.Println(string(resp))

	//restart完成
	restartFlag = false
	w.Write(resp)
}


//用于监听
func step(w http.ResponseWriter, r *http.Request)  {
	body, err := ioutil.ReadAll(r.Body) //转为字节[]byte
	if err != nil {
		panic(err)
	}

	var scheduler_conf simulator.ConfType
	err = json.Unmarshal(body, &scheduler_conf) //将字节[]byte读入struct中
	if err != nil {
		panic(err)
	}

	if loadNewSchedulerConf{
		time.Sleep(time.Duration(0.4*1e9))
		fmt.Println("wait to load new conf")
	}

	acts, tiers, cfg, err = scheduler.UnmarshalSchedulerConfV2(scheduler_conf.Conf) //tiers里由存储argument的map数据结构
	if err != nil {
		fmt.Println("error:", err)
		return
	}

	fmt.Println("load conf:")
	fmt.Println(scheduler_conf.Conf)

	loadNewSchedulerConf = true

	startSimulate = time.Now()
	simulationTime = time.Time{}

	w.Write([]byte(`1`))

}

//用于监听
func stepResult(w http.ResponseWriter, r *http.Request)  {
	if loadNewSchedulerConf && notCompletion{ //这一周期未运行完 且 job未完成，不返回当前状态
		w.Write([]byte(`0`))
		return
	}

	var v1NodeList []*v1.Node
	for _, node := range cluster.Nodes {
		cpu := strconv.Itoa(int(node.Used.MilliCPU))
		mem := strconv.Itoa(int(node.Used.Memory))
		//修改此处要把stepResult中的一块更改
		//Capacity表示实际使用量
		v1Node := util.BuildNode(node.Name, util.BuildResourceListWithGPU(cpu, mem, "0"), node.Node.Labels)
		//Allocatable表示实际容量
		v1Node.Status.Allocatable = node.Node.Status.Allocatable
		v1NodeList = append(v1NodeList, v1Node)
	}

	var PodList []*v1.Pod

	for _, job := range cluster.Jobs {
		for _, task := range job.Tasks {
			PodList = append(PodList, task.Pod)
		}
	}

	info := simulator.Info{ NotCompletion: notCompletion,
		Nodes: cluster.Nodes,
		Jobs: cluster.Jobs,

		Done: !notCompletion,
		V1Nodes: v1NodeList,
		Pods: PodList,
		Clock: schedulingapi.NowTime.Local().String()}

	//info := Info{ NotCompletion: notCompletion, Nodes: cluster.Nodes, Jobs: cluster.Jobs } //原

	resp, _ := json.Marshal(info)
	//fmt.Println(string(resp))

	w.Write(resp)
}

func stepResultAnyway(w http.ResponseWriter, r *http.Request)  {
	info := simulator.Info{ NotCompletion: notCompletion, Nodes: cluster.Nodes, Jobs: cluster.Jobs }
	resp, _ := json.Marshal(info)
	//fmt.Println(string(resp))

	w.Write(resp)
}

func server()  {
	//if len(os.Args) < 2{
	//	//未附带参数则默认8002
	//	fmt.Println("\nport",port)
	//} else{
	//	port = ":" + os.Args[1]
	//	fmt.Println("\nport",port)
	//}
	// 处理reset请求
	http.HandleFunc("/reset", reset)
	// 处理step请求
	http.HandleFunc("/step", step)
	// 处理stepResult请求
	http.HandleFunc("/stepResult", stepResult)
	// 处理stepResult请求
	http.HandleFunc("/stepResultAnyway", stepResultAnyway)
	// 设置监听端口，等待响应
	http.ListenAndServe(":8006", nil)
}















