// Package options provides minimal scheduler CLI options for tests and sim builds
// that do not ship the full scheduler binary in this repo.
package options

import (
	"github.com/spf13/pflag"
)

// ServerOpts is the global server options used by scheduler tests.
var ServerOpts *ServerOption

// ServerOption holds scheduler-related tuning used by pkg/scheduler.
type ServerOption struct {
	MinNodesToFind             int32
	MinPercentageOfNodesToFind int32
	PercentageOfNodesToFind    int32
	EnablePriorityClass        bool
	EnableCSIStorage           bool
}

// NewServerOption returns default server options for tests.
func NewServerOption() *ServerOption {
	return &ServerOption{
		MinNodesToFind:             100,
		MinPercentageOfNodesToFind: 5,
		PercentageOfNodesToFind:    100,
		EnablePriorityClass:        true,
		EnableCSIStorage:           false,
	}
}

// RegisterOptions registers flags (no-op stub for tests that call it).
func (s *ServerOption) RegisterOptions() {
	fs := pflag.NewFlagSet("scheduler-test", pflag.ContinueOnError)
	fs.Int32Var(&s.MinNodesToFind, "min-nodes-to-find", s.MinNodesToFind, "")
	fs.Int32Var(&s.MinPercentageOfNodesToFind, "min-percentage-of-nodes-to-find", s.MinPercentageOfNodesToFind, "")
	fs.Int32Var(&s.PercentageOfNodesToFind, "percentage-nodes-to-find", s.PercentageOfNodesToFind, "")
}
