package main

import (
	"encoding/json"
	"fmt"
	"io/ioutil"
	"net/http"
	"strconv"
	"time"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
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

// port: HTTP listen address for the simulator (matches default SimRun URL port in Submit_volcano_workloads).
var port = ":8006"

var (
	loadNewSchedulerConf = true      // set when a new scheduler conf has been received
	notCompletion        = false     // true while simulation still has work (jobs not fully settled)
	restartFlag          = true      // reset in progress
	cnt                  = int64(0)  // loop iteration count
	period               = int64(-1) // reload scheduler conf every N loop seconds; -1 = only at startup
	acts                 []framework.Action
	tiers                []conf.Tier
	cfg                  []conf.Configuration
	cluster              = &schedulingapi.ClusterInfo{ // create cluster
		Nodes:          make(map[string]*schedulingapi.NodeInfo),
		Jobs:           make(map[schedulingapi.JobID]*schedulingapi.JobInfo),
		Queues:         make(map[schedulingapi.QueueID]*schedulingapi.QueueInfo),
		NamespaceInfo:  make(map[schedulingapi.NamespaceName]*schedulingapi.NamespaceInfo),
		RevocableNodes: make(map[string]*schedulingapi.NodeInfo),
	}
	jobQueue = util.NewPriorityQueue(func(l interface{}, r interface{}) bool { // JobInfo by submit time; not a k8s Queue object
		lv := l.(*schedulingapi.JobInfo)
		rv := r.(*schedulingapi.JobInfo)
		return lv.SubTimestamp.Time.Before(rv.SubTimestamp.Time)
	})
	defaultQueue *scheduling.Queue // k8s Queue object

	simulationTime time.Time
)

/*
Notes:
1. Unscheduled and Binding (bound, container creating) pods are exposed as Pod Phase Pending; after a Task becomes Running it stays Running and does not move to Succeeded when work completes.
2. Binding means scheduled to a node and container is being created; Running means the container is ready.
3. Simulation ends when the submit queue is empty and no Task is still Binding (container creation queue drained).
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
		MinNodesToFind:             100, // related to CalculateNumOfFeasibleNodesToFind in pkg/scheduler/utils/scheduler_helper.go
		MinPercentageOfNodesToFind: 5,
		PercentageOfNodesToFind:    100,
	}
	opts.RegisterOptions() // register options as package globals

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

	actions.InitV2() // register plugins; required for UnmarshalSchedulerConfV2

	cluster.Queues[queueInfo.UID] = queueInfo                 // add queue to cluster
	cluster.NamespaceInfo[namespaceInfo.Name] = namespaceInfo // add namespace to cluster

	go server() // HTTP server; start after init above

	fmt.Print("simulator start...")

	// one outer loop tick = 1 second
	for true {

		for !notCompletion || restartFlag { // idle or waiting for reset; sits here at startup; conf load takes time - do not step right after reset
			time.Sleep(time.Duration(0.2 * 1e9))
		}

		//fmt.Println(schedulingapi.NowTime)

		// submit jobs whose submit time has passed
		for !jobQueue.Empty() {
			front := jobQueue.Pop().(*schedulingapi.JobInfo)
			if schedulingapi.NowTime.Time.Before(front.SubTimestamp.Time) { // sim time still before job submit time
				jobQueue.Push(front)
				break
			} else {
				cluster.Jobs[front.UID] = front // commit JobInfo to cluster
				//fmt.Println(schedulingapi.NowTime,": submit",front.Name)

				// on submit, set creation time so pending pods have a timestamp
				for _, task := range front.Tasks {
					task.Pod.SetCreationTimestamp(schedulingapi.NowTime) // pod creation time; 1e9 ns = 1 s
				}
			}
		}

		// more concurrent pod creates take longer; split into two phases

		// decrement container-create countdown for Binding tasks
		for _, node := range cluster.Nodes {
			for _, task := range node.Tasks {
				if task.Status != schedulingapi.Binding {
					continue
				}
				task.CtnCreationCountDown -= 1
			}
		}

		// per node, every interval ticks, promote earliest Binding task (countdown done) to Running
		for _, node := range cluster.Nodes {
			if node.CtnCreationTimeInterval != 0 && cnt%node.CtnCreationTimeInterval != 0 {
				continue
			}
			findFlag := false
			var selectTask *schedulingapi.TaskInfo
			for _, task := range node.Tasks {
				if task.Status != schedulingapi.Binding {
					continue
				}
				if task.CtnCreationCountDown > 0 {
					continue
				}
				if !findFlag {
					selectTask = task
					findFlag = true
					continue
				}
				if task.Pod.CreationTimestamp.Before(&selectTask.Pod.CreationTimestamp) {
					selectTask = task
				}
			}
			if findFlag {
				fmt.Println("create container in", selectTask.NodeName, ":", selectTask.Name, schedulingapi.NowTime)
				// sync task state in cluster (node.Tasks vs job.Tasks)
				selectTask.Pod.Status.Phase = v1.PodRunning
				cluster.Jobs[selectTask.Job].Tasks[selectTask.UID].Pod.Status.Phase = v1.PodRunning

				selectTask.Status = schedulingapi.Running
				cluster.Jobs[selectTask.Job].Tasks[selectTask.UID].Status = schedulingapi.Running

				selectTask.Pod.Status.StartTime = schedulingapi.NowTime.DeepCopy()
				cluster.Jobs[selectTask.Job].Tasks[selectTask.UID].Pod.Status.StartTime = schedulingapi.NowTime.DeepCopy()
				// TODO: also update job.TaskStatusIndex
				//delete(cluster.Jobs[task.Job].TaskStatusIndex[schedulingapi.Binding], task.UID)
			}

		}

		// after reset or each period, wait for next step (scheduler conf)
		if (cnt == 0) || (period != -1 && cnt%period == 0) {
			loadNewSchedulerConf = false
			fmt.Println("wait for conf...")
		}

		for !loadNewSchedulerConf {

			time.Sleep(time.Duration(1e9))
		}

		if restartFlag {
			continue
		}

		// run scheduling
		ssn := framework.OpenSessionV2(cluster, tiers, cfg)
		for _, action := range acts {
			action.Execute(ssn)
			//fmt.Println(action.Name())
		}

		//framework.CloseSession(ssn) // panics

		syncSimulationPodPhases()

		// round settled: submit queue empty and no Binding (container-creating) tasks
		notCompletion = clusterHasBindingTask() || !jobQueue.Empty()

		// when all work is done
		if !notCompletion {
			// print run summary
			fmt.Println(schedulingapi.NowTime, "all complete")
			fmt.Println("simulation time:", simulationTime)
			fmt.Println("---------------------\nNodes:")
			for _, node := range cluster.Nodes {
				//fmt.Println(node.Tasks)
				//for _,task := range node.Tasks{
				//	//fmt.Println(task.Pod.CreationTimestamp)
				//	fmt.Println(task.NodeName)
				//}
				fmt.Println(node.Name, ":")
				fmt.Println("task num:", len(node.Tasks))
				//fmt.Println(node.Capability)
				//fmt.Println(node.Allocatable) // unchanged
				fmt.Println("Idle:", node.Idle) // reflects usage
				fmt.Println("Used:", node.Used)
			}
			fmt.Println("---------------------\nJobs:")
			for _, job := range cluster.Jobs {
				//fmt.Println(ssn.JobReady(job))
				for _, task := range job.Tasks {
					fmt.Println(task.Name)
					fmt.Println(task.Status)
					fmt.Println(task.Pod.CreationTimestamp)
					fmt.Println("job-create:", job.CreationTimestamp)
					fmt.Println("sim-end:", task.SimEndTimestamp)
				}
			}
		}

		// advance time by 1 s
		schedulingapi.NowTime = metav1.NewTime(schedulingapi.NowTime.Add(time.Duration(1e9))) // 1e9 ns = 1 s
		cnt += 1
		if cnt%1800 == 0 {
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
		//			break // only inspect one task
		//		}
		//	}
		//	fmt.Println("all job:",len(cluster.Jobs))
		//	fmt.Println("not finish job:",jobNum)
		//	startSimulate = time.Now()
		//}
	}
}

// HTTP handler: reset workload / cluster state
func reset(w http.ResponseWriter, r *http.Request) {
	if notCompletion {
		// set flags and let main loop return to idle wait
		restartFlag = true
		loadNewSchedulerConf = true // unblock if stuck waiting for conf
		time.Sleep(time.Duration(1e9))

		// clear submit queue
		jobQueue = util.NewPriorityQueue(func(l interface{}, r interface{}) bool { // JobInfo by submit time
			lv := l.(*schedulingapi.JobInfo)
			rv := r.(*schedulingapi.JobInfo)
			return lv.SubTimestamp.Time.Before(rv.SubTimestamp.Time)
		})
	}
	fmt.Println("reset...")

	// reset cluster Nodes, Jobs, RevocableNodes
	cluster = &schedulingapi.ClusterInfo{ // create cluster
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

	cluster.Queues[queueInfo.UID] = queueInfo                 // add queue to cluster
	cluster.NamespaceInfo[namespaceInfo.Name] = namespaceInfo // add namespace to cluster

	// reset time and loop counter
	cnt = 0
	schedulingapi.NowTime = metav1.NewTime(time.Time{})

	body, err := ioutil.ReadAll(r.Body) // read body to []byte
	if err != nil {
		panic(err)
	}

	var workload simulator.WorkloadType
	err = json.Unmarshal(body, &workload) // decode JSON into struct
	if err != nil {
		panic(err)
	}

	// load period parameter
	period_, err := strconv.Atoi(workload.Period)
	period = int64(period_)
	if err != nil {
		return
	}

	// load nodes
	err, nodes := simulator.Yaml2Nodes([]byte(workload.Nodes))
	if err != nil {
		fmt.Println("error:", err)
		return
	}

	for _, node := range nodes["cluster"] { // add cluster nodes
		nodeInfo := schedulingapi.NewNodeInfo(&node.Node)
		cluster.Nodes[nodeInfo.Name] = nodeInfo

		// from payload; missing values default to 0
		if float64(node.CtnCreationTimeInterval) < 0.1 && float64(node.CtnCreationExtraTime) < 0.1 &&
			float64(node.CtnCreationTime) < 0.1 { //default
			nodeInfo.CtnCreationTime = 2
			nodeInfo.CtnCreationExtraTime = 0.5
			nodeInfo.CtnCreationTimeInterval = 1
		} else {
			nodeInfo.CtnCreationTime = node.CtnCreationTime
			nodeInfo.CtnCreationExtraTime = node.CtnCreationExtraTime
			nodeInfo.CtnCreationTimeInterval = node.CtnCreationTimeInterval
		}

		if node.CalculationSpeed < 0.1 { //default
			nodeInfo.CalculationSpeed = 1
		} else {
			nodeInfo.CalculationSpeed = node.CalculationSpeed
		}

		if node.MinimumSpeed < 0.1 { //default
			nodeInfo.MinimumSpeed = -1
		} else {
			nodeInfo.MinimumSpeed = node.MinimumSpeed
		}

		if node.SlowSpeedThreshold < 0.1 { //default
			nodeInfo.SlowSpeedThreshold = -1
		} else {
			nodeInfo.SlowSpeedThreshold = node.SlowSpeedThreshold
		}
	}

	for _, node := range cluster.Nodes {
		fmt.Println(node.Name, ":")
		fmt.Println("Allocatable:", node.Allocatable)
		fmt.Println("Capability:", node.Capability)
		fmt.Println("Idle:", node.Idle)
		fmt.Println("Used:", node.Used)
		fmt.Println("Taints:", node.Node.Spec.Taints)
	}

	cluster.NodeList = make([]string, len(cluster.Nodes))
	for _, ni := range cluster.Nodes {
		cluster.NodeList = append(cluster.NodeList, ni.Name)
	}

	// load jobs
	err, jobs := simulator.Yaml2Jobs([]byte(workload.Workload))
	if err != nil {
		fmt.Println("error:", err)
	}
	for _, job := range jobs["jobs"] { // Job -> JobInfo, enqueue by submit time
		jobInfo := schedulingapi.NewJobInfoV2(job)
		// job submit and creation timestamps
		if subTime, found := job.Labels["sub-time"]; found {
			if timestamp, err := strconv.Atoi(subTime); err == nil {
				jobInfo.SubTimestamp = metav1.NewTime(time.Time{}.Add(time.Duration(timestamp * 1e9)))
				jobInfo.CreationTimestamp = metav1.NewTime(time.Time{}.Add(time.Duration(timestamp * 1e9)))
			}
		}
		// without label, submit time defaults to 0
		jobQueue.Push(jobInfo)
	}
	//fmt.Println(jobs)

	notCompletion = true

	fmt.Println("reset done")

	var v1NodeList []*v1.Node
	for _, node := range cluster.Nodes {
		// keep in sync with stepResult node serialization
		// Capacity: reported used resources
		v1Node := util.BuildNode(node.Name, util.BuildResourceListWithGPU("0", "0Gi", "0"), node.Node.Labels)
		// Allocatable: capacity
		v1Node.Status.Allocatable = node.Node.Status.Allocatable
		v1NodeList = append(v1NodeList, v1Node)
	}

	info := simulator.Info{Done: !notCompletion, V1Nodes: v1NodeList, Clock: schedulingapi.NowTime.Local().String()}
	resp, _ := json.Marshal(info)
	//fmt.Println(string(resp))

	// reset finished
	restartFlag = false
	w.Write(resp)
}

// HTTP handler: load scheduler conf (step)
func step(w http.ResponseWriter, r *http.Request) {
	body, err := ioutil.ReadAll(r.Body) // read body to []byte
	if err != nil {
		panic(err)
	}

	var scheduler_conf simulator.ConfType
	err = json.Unmarshal(body, &scheduler_conf) // decode JSON into struct
	if err != nil {
		panic(err)
	}

	if loadNewSchedulerConf {
		time.Sleep(time.Duration(0.4 * 1e9))
		fmt.Println("wait to load new conf")
	}

	acts, tiers, cfg, err = scheduler.UnmarshalSchedulerConfV2(scheduler_conf.Conf) // tiers hold plugin argument maps
	if err != nil {
		fmt.Println("error:", err)
		return
	}

	fmt.Println("load conf:")
	fmt.Println(scheduler_conf.Conf)

	loadNewSchedulerConf = true

	simulationTime = time.Time{}

	w.Write([]byte(`1`))

}

// HTTP handler: snapshot state (stepResult)
func stepResult(w http.ResponseWriter, r *http.Request) {
	if loadNewSchedulerConf && notCompletion { // round not finished and jobs active: defer snapshot
		w.Write([]byte(`0`))
		return
	}

	var v1NodeList []*v1.Node
	for _, node := range cluster.Nodes {
		cpu := strconv.Itoa(int(node.Used.MilliCPU))
		mem := strconv.Itoa(int(node.Used.Memory))
		// keep in sync with stepResult node serialization
		// Capacity: reported used resources
		v1Node := util.BuildNode(node.Name, util.BuildResourceListWithGPU(cpu, mem, "0"), node.Node.Labels)
		// Allocatable: capacity
		v1Node.Status.Allocatable = node.Node.Status.Allocatable
		v1NodeList = append(v1NodeList, v1Node)
	}

	var PodList []*v1.Pod

	for _, job := range cluster.Jobs {
		for _, task := range job.Tasks {
			PodList = append(PodList, task.Pod)
		}
	}

	info := simulator.Info{NotCompletion: notCompletion,
		Nodes: cluster.Nodes,
		Jobs:  cluster.Jobs,

		Done:    !notCompletion,
		V1Nodes: v1NodeList,
		Pods:    PodList,
		Clock:   schedulingapi.NowTime.Local().String()}

	//info := Info{ NotCompletion: notCompletion, Nodes: cluster.Nodes, Jobs: cluster.Jobs } // legacy shape

	resp, _ := json.Marshal(info)
	//fmt.Println(string(resp))

	w.Write(resp)
}

func stepResultAnyway(w http.ResponseWriter, r *http.Request) {
	info := simulator.Info{NotCompletion: notCompletion, Nodes: cluster.Nodes, Jobs: cluster.Jobs}
	resp, _ := json.Marshal(info)
	//fmt.Println(string(resp))

	w.Write(resp)
}

func server() {
	//if len(os.Args) < 2{
	//	// default port 8002 if no arg
	//	fmt.Println("\nport",port)
	//} else{
	//	port = ":" + os.Args[1]
	//	fmt.Println("\nport",port)
	//}
	// /reset
	http.HandleFunc("/reset", reset)
	// /step
	http.HandleFunc("/step", step)
	// /stepResult
	http.HandleFunc("/stepResult", stepResult)
	// /stepResultAnyway
	http.HandleFunc("/stepResultAnyway", stepResultAnyway)
	// listen and serve
	http.ListenAndServe(port, nil)
}
