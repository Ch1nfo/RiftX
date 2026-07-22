//go:build integration

package docker

import (
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/riftx-dev/riftx/services/sandbox-managerd/internal/manager"
)

func TestLinuxNetworkSecurityBoundary(t *testing.T) {
	suffix := fmt.Sprintf("%d", os.Getpid())
	managementNetwork := "riftx-security-mgmt-" + suffix
	targetNetwork := "riftx-security-target-" + suffix
	sandbox := "riftx-security-sandbox-" + suffix
	allowedTarget := "riftx-security-allowed-" + suffix
	deniedTarget := "riftx-security-denied-" + suffix

	runDocker(t, "network", "create", managementNetwork)
	t.Cleanup(func() { cleanupDocker(managementNetwork, targetNetwork, sandbox, allowedTarget, deniedTarget) })
	runDocker(t, "network", "create", targetNetwork)
	runDocker(t, "run", "-d", "--name", allowedTarget, "--network", targetNetwork,
		"alpine:3.21", "sh", "-c", httpServerCommand())
	runDocker(t, "run", "-d", "--name", deniedTarget, "--network", managementNetwork,
		"alpine:3.21", "sh", "-c", httpServerCommand())
	runDocker(t, "run", "-d", "--name", sandbox, "--network", managementNetwork,
		"--cap-drop", "ALL", "--security-opt", "no-new-privileges", "alpine:3.21", "sleep", "300")
	runDocker(t, "network", "connect", targetNetwork, sandbox)

	allowedIP := inspectContainerIP(t, allowedTarget, targetNetwork)
	deniedIP := inspectContainerIP(t, deniedTarget, managementNetwork)
	managementCIDR := strings.TrimSpace(runDocker(
		t,
		"network",
		"inspect",
		"--format",
		"{{range .IPAM.Config}}{{.Subnet}}{{end}}",
		managementNetwork,
	))
	pid := strings.TrimSpace(runDocker(t, "inspect", "--format", "{{.State.Pid}}", sandbox))
	policy := manager.RenderNftablesPolicy(sandbox, manager.Scope{
		CIDRs:       []string{allowedIP + "/32"},
		Ports:       []uint16{8080},
		DeniedCIDRs: []string{"127.0.0.0/8", "169.254.0.0/16", managementCIDR},
	})
	command := exec.Command("sudo", "nsenter", "-t", pid, "-n", "nft", "-f", "-")
	command.Stdin = bytes.NewBufferString(policy)
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("apply nftables policy: %v: %s", err, output)
	}

	waitForAllowedTarget(t, sandbox, allowedIP)
	assertDockerCommandFails(t, "exec", sandbox, "wget", "-T", "1", "-qO-", "http://"+deniedIP+":8080")
	assertDockerCommandFails(t, "exec", sandbox, "wget", "-T", "1", "-qO-", "http://169.254.169.254/")
	assertDockerCommandFails(t, "exec", sandbox, "nslookup", "example.com")
	assertDockerCommandFails(t, "exec", sandbox, "ping", "-c", "1", allowedIP)
}

func httpServerCommand() string {
	return `while true; do printf 'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok' | nc -l -p 8080; done`
}

func waitForAllowedTarget(t *testing.T, sandbox, targetIP string) {
	t.Helper()
	var lastOutput string
	for range 20 {
		command := exec.Command("docker", "exec", sandbox, "wget", "-T", "1", "-qO-", "http://"+targetIP+":8080")
		output, err := command.CombinedOutput()
		lastOutput = string(output)
		if err == nil && strings.TrimSpace(lastOutput) == "ok" {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("allowed target was unreachable: %s", lastOutput)
}

func inspectContainerIP(t *testing.T, container, network string) string {
	t.Helper()
	template := fmt.Sprintf("{{with index .NetworkSettings.Networks %q}}{{.IPAddress}}{{end}}", network)
	address := strings.TrimSpace(runDocker(t, "inspect", "--format", template, container))
	if address == "" {
		t.Fatalf("container %s has no address on %s", container, network)
	}
	return address
}

func runDocker(t *testing.T, args ...string) string {
	t.Helper()
	output, err := exec.Command("docker", args...).CombinedOutput()
	if err != nil {
		t.Fatalf("docker %s: %v: %s", strings.Join(args, " "), err, output)
	}
	return string(output)
}

func assertDockerCommandFails(t *testing.T, args ...string) {
	t.Helper()
	if output, err := exec.Command("docker", args...).CombinedOutput(); err == nil {
		t.Fatalf("docker %s unexpectedly succeeded: %s", strings.Join(args, " "), output)
	}
}

func cleanupDocker(networksAndContainers ...string) {
	for _, name := range networksAndContainers[2:] {
		_ = exec.Command("docker", "rm", "-f", name).Run()
	}
	for _, name := range networksAndContainers[:2] {
		_ = exec.Command("docker", "network", "rm", name).Run()
	}
}
