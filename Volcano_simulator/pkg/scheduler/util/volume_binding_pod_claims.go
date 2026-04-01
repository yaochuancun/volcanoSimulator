package util

import (
	"reflect"

	volumescheduling "k8s.io/kubernetes/pkg/scheduler/framework/plugins/volumebinding"
)

// PodVolumeClaimsUnboundImmediateLen returns len(podVolumeClaims.unboundClaimsImmediate); fields are unexported upstream.
func PodVolumeClaimsUnboundImmediateLen(claims *volumescheduling.PodVolumeClaims) int {
	if claims == nil {
		return 0
	}
	v := reflect.ValueOf(claims).Elem()
	f := v.FieldByName("unboundClaimsImmediate")
	if !f.IsValid() {
		return 0
	}
	return f.Len()
}
