/*
Copyright 2020 The Volcano Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package k8s

import (
	"context"

	v1 "k8s.io/api/core/v1"
	resourceapi "k8s.io/api/resource/v1beta1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/client-go/informers"
	clientset "k8s.io/client-go/kubernetes"
	restclient "k8s.io/client-go/rest"
	"k8s.io/client-go/tools/events"
	"k8s.io/dynamic-resource-allocation/structured"
	"k8s.io/klog/v2"
	"k8s.io/kubernetes/pkg/scheduler/framework"
	"k8s.io/kubernetes/pkg/scheduler/framework/parallelize"
)

// Framework is a minimal framework.Handle for in-process K8s scheduler plugins (predicates, etc.).
type Framework struct {
	snapshot        framework.SharedLister
	kubeClient      clientset.Interface
	informerFactory informers.SharedInformerFactory
}

var _ framework.Handle = (*Framework)(nil)

func (f *Framework) SnapshotSharedLister() framework.SharedLister {
	return f.snapshot
}

func (f *Framework) IterateOverWaitingPods(callback func(framework.WaitingPod)) {
	panic("not implemented")
}

func (f *Framework) GetWaitingPod(uid types.UID) framework.WaitingPod {
	panic("not implemented")
}

func (f *Framework) RejectWaitingPod(uid types.UID) bool {
	panic("not implemented")
}

func (f *Framework) ClientSet() clientset.Interface {
	return f.kubeClient
}

func (f *Framework) KubeConfig() *restclient.Config {
	panic("not implemented")
}

func (f *Framework) EventRecorder() events.EventRecorder {
	return nil
}

func (f *Framework) SharedInformerFactory() informers.SharedInformerFactory {
	return f.informerFactory
}

func (f *Framework) SharedDRAManager() framework.SharedDRAManager {
	return noopSharedDRAManager{}
}

func (f *Framework) RunFilterPluginsWithNominatedPods(ctx context.Context, state *framework.CycleState, pod *v1.Pod, info *framework.NodeInfo) *framework.Status {
	panic("not implemented")
}

func (f *Framework) Extenders() []framework.Extender {
	panic("not implemented")
}

func (f *Framework) Parallelizer() parallelize.Parallelizer {
	return parallelize.NewParallelizer(16)
}

// PodNominator
func (f *Framework) AddNominatedPod(logger klog.Logger, pod *framework.PodInfo, nominatingInfo *framework.NominatingInfo) {
	panic("not implemented")
}

func (f *Framework) DeleteNominatedPodIfExists(pod *v1.Pod) {
	panic("not implemented")
}

func (f *Framework) UpdateNominatedPod(logger klog.Logger, oldPod *v1.Pod, newPodInfo *framework.PodInfo) {
	panic("not implemented")
}

func (f *Framework) NominatedPodsForNode(nodeName string) []*framework.PodInfo {
	panic("not implemented")
}

// PluginsRunner
func (f *Framework) RunPreScorePlugins(ctx context.Context, state *framework.CycleState, pod *v1.Pod, nodes []*framework.NodeInfo) *framework.Status {
	panic("not implemented")
}

func (f *Framework) RunScorePlugins(ctx context.Context, state *framework.CycleState, pod *v1.Pod, nodes []*framework.NodeInfo) ([]framework.NodePluginScores, *framework.Status) {
	panic("not implemented")
}

func (f *Framework) RunFilterPlugins(ctx context.Context, state *framework.CycleState, pod *v1.Pod, info *framework.NodeInfo) *framework.Status {
	panic("not implemented")
}

func (f *Framework) RunPreFilterExtensionAddPod(ctx context.Context, state *framework.CycleState, podToSchedule *v1.Pod, podInfoToAdd *framework.PodInfo, nodeInfo *framework.NodeInfo) *framework.Status {
	panic("not implemented")
}

func (f *Framework) RunPreFilterExtensionRemovePod(ctx context.Context, state *framework.CycleState, podToSchedule *v1.Pod, podInfoToRemove *framework.PodInfo, nodeInfo *framework.NodeInfo) *framework.Status {
	panic("not implemented")
}

// PodActivator
func (f *Framework) Activate(logger klog.Logger, pods map[string]*v1.Pod) {}

type noopSharedDRAManager struct{}

type noopClaimTracker struct{}

func (noopClaimTracker) List() ([]*resourceapi.ResourceClaim, error) { return nil, nil }
func (noopClaimTracker) Get(string, string) (*resourceapi.ResourceClaim, error) {
	return nil, nil
}
func (noopClaimTracker) ListAllAllocatedDevices() (sets.Set[structured.DeviceID], error) {
	return sets.New[structured.DeviceID](), nil
}
func (noopClaimTracker) SignalClaimPendingAllocation(types.UID, *resourceapi.ResourceClaim) error {
	return nil
}
func (noopClaimTracker) ClaimHasPendingAllocation(types.UID) bool { return false }
func (noopClaimTracker) RemoveClaimPendingAllocation(types.UID) (bool) { return false }
func (noopClaimTracker) AssumeClaimAfterAPICall(*resourceapi.ResourceClaim) error { return nil }
func (noopClaimTracker) AssumedClaimRestore(string, string) {}

type noopResourceSliceLister struct{}

func (noopResourceSliceLister) List() ([]*resourceapi.ResourceSlice, error) { return nil, nil }

type noopDeviceClassLister struct{}

func (noopDeviceClassLister) List() ([]*resourceapi.DeviceClass, error) { return nil, nil }
func (noopDeviceClassLister) Get(string) (*resourceapi.DeviceClass, error) { return nil, nil }

func (noopSharedDRAManager) ResourceClaims() framework.ResourceClaimTracker { return noopClaimTracker{} }
func (noopSharedDRAManager) ResourceSlices() framework.ResourceSliceLister { return noopResourceSliceLister{} }
func (noopSharedDRAManager) DeviceClasses() framework.DeviceClassLister    { return noopDeviceClassLister{} }

// NewFrameworkHandle creates a framework.Handle for K8s in-tree plugins used by Volcano.
func NewFrameworkHandle(nodeMap map[string]*framework.NodeInfo, client clientset.Interface, informerFactory informers.SharedInformerFactory) framework.Handle {
	snapshot := NewSnapshot(nodeMap)
	return &Framework{
		snapshot:        snapshot,
		kubeClient:      client,
		informerFactory: informerFactory,
	}
}
