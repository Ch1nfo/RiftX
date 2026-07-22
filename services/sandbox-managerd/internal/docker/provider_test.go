package docker

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/riftx-dev/riftx/services/sandbox-managerd/internal/manager"
)

type recordingRunner struct {
	calls  [][]string
	inputs []string
}

func (r *recordingRunner) Run(_ context.Context, input []byte, name string, args ...string) ([]byte, error) {
	call := append([]string{name}, args...)
	r.calls = append(r.calls, call)
	r.inputs = append(r.inputs, string(input))
	joined := strings.Join(call, " ")
	if strings.Contains(joined, "network inspect --format {{json .IPAM.Config}}") {
		return []byte(`[{"Subnet":"172.28.0.0/16","Gateway":"172.28.0.1"}]`), nil
	}
	if strings.Contains(joined, "inspect --format {{.State.Pid}}") {
		return []byte("123\n"), nil
	}
	if strings.Contains(joined, "inspect --format {{range .NetworkSettings.Networks}}") {
		return []byte("10.0.0.2\n"), nil
	}
	return nil, nil
}

func TestCreateAppliesContainerAndNetworkSecurityBaseline(t *testing.T) {
	runner := &recordingRunner{}
	provider := Provider{
		Runner: runner, DockerBinary: "docker", NSenterBinary: "nsenter", NftBinary: "nft",
		ManagementNet: "riftx-management", ArtifactRoot: t.TempDir(), CredentialRoot: t.TempDir(),
	}
	record := manager.Record{
		Sandbox:   manager.Sandbox{ID: "sb-1", EngagementID: "eng-1", PolicyRevision: "rev-1"},
		TokenHash: [32]byte{1, 2, 3},
		ExpiresAt: time.Now().Add(time.Minute),
		Request: manager.CreateRequest{
			Image:     "riftx/sandbox:test",
			Resources: manager.Resources{CPULimit: 2, MemoryMiB: 1024, PIDsLimit: 256},
			Scope:     manager.Scope{CIDRs: []string{"10.10.0.0/24"}, DeniedCIDRs: []string{"169.254.0.0/16"}},
		},
	}
	url, err := provider.Create(context.Background(), record)
	if err != nil {
		t.Fatal(err)
	}
	if url != "ws://10.0.0.2:9800" {
		t.Fatalf("URL = %q", url)
	}
	allCalls := make([]string, 0, len(runner.calls))
	for _, call := range runner.calls {
		allCalls = append(allCalls, strings.Join(call, " "))
	}
	joined := strings.Join(allCalls, "\n")
	for _, required := range []string{"--read-only", "--cap-drop ALL", "no-new-privileges", "--pids-limit 256", "--network riftx-management", "dst=/run/riftx/auth.json,readonly", "--riftx-auth-file /run/riftx/auth.json", "nsenter -t 123 -n nft -f -"} {
		if !strings.Contains(joined, required) {
			t.Errorf("missing %q in calls:\n%s", required, joined)
		}
	}
	joinedInputs := strings.Join(runner.inputs, "\n")
	for _, denied := range []string{"ip daddr 172.28.0.0/16 drop", "ip daddr 172.28.0.1/32 drop"} {
		if !strings.Contains(joinedInputs, denied) {
			t.Errorf("missing management deny %q in nft policy:\n%s", denied, joinedInputs)
		}
	}
}
