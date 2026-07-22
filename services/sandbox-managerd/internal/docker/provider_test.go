package docker

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/riftx-dev/riftx/services/sandbox-managerd/internal/manager"
)

type recordingRunner struct {
	calls    [][]string
	inputs   []string
	psOutput []byte
}

type failingInspectRunner struct {
	recordingRunner
}

func (r *failingInspectRunner) Run(ctx context.Context, input []byte, name string, args ...string) ([]byte, error) {
	if strings.Join(args, " ") == "network inspect riftx-management" {
		r.calls = append(r.calls, append([]string{name}, args...))
		r.inputs = append(r.inputs, string(input))
		return nil, errors.New("network missing")
	}
	return r.recordingRunner.Run(ctx, input, name, args...)
}

func (r *recordingRunner) Run(_ context.Context, input []byte, name string, args ...string) ([]byte, error) {
	call := append([]string{name}, args...)
	r.calls = append(r.calls, call)
	r.inputs = append(r.inputs, string(input))
	joined := strings.Join(call, " ")
	if strings.Contains(joined, "ps -aq --filter label=riftx.engagement") {
		return r.psOutput, nil
	}
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

func TestReconcileRemovesOnlyRiftXLabeledContainers(t *testing.T) {
	credentialRoot := t.TempDir()
	if err := os.WriteFile(filepath.Join(credentialRoot, "stale.json"), []byte("secret"), 0o600); err != nil {
		t.Fatal(err)
	}
	runner := &recordingRunner{psOutput: []byte("container-a\ncontainer-b\n")}
	provider := Provider{Runner: runner, DockerBinary: "docker", CredentialRoot: credentialRoot}
	if err := provider.Reconcile(context.Background()); err != nil {
		t.Fatal(err)
	}
	expected := [][]string{
		{"docker", "ps", "-aq", "--filter", "label=riftx.engagement"},
		{"docker", "rm", "-f", "container-a"},
		{"docker", "rm", "-f", "container-b"},
	}
	if !reflect.DeepEqual(runner.calls, expected) {
		t.Fatalf("unexpected calls: %#v", runner.calls)
	}
	if _, err := os.Stat(filepath.Join(credentialRoot, "stale.json")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("stale credential was not removed: %v", err)
	}
}

func TestEnsureManagementNetworkDisablesInterContainerCommunication(t *testing.T) {
	runner := &failingInspectRunner{}
	provider := Provider{Runner: runner, DockerBinary: "docker", ManagementNet: "riftx-management"}
	if err := provider.EnsureManagementNetwork(context.Background()); err != nil {
		t.Fatal(err)
	}
	joined := make([]string, 0, len(runner.calls))
	for _, call := range runner.calls {
		joined = append(joined, strings.Join(call, " "))
	}
	if !strings.Contains(strings.Join(joined, "\n"), "network create --driver bridge --opt com.docker.network.bridge.enable_icc=false") {
		t.Fatalf("management network was not hardened: %v", runner.calls)
	}
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
