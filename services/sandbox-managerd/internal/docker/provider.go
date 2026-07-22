package docker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"mime"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/riftx-dev/riftx/services/sandbox-managerd/internal/manager"
)

type Runner interface {
	Run(context.Context, []byte, string, ...string) ([]byte, error)
}

type CommandRunner struct{}

func (CommandRunner) Run(ctx context.Context, input []byte, name string, args ...string) ([]byte, error) {
	command := exec.CommandContext(ctx, name, args...)
	command.Stdin = bytes.NewReader(input)
	output, err := command.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("%s failed: %w: %s", name, err, strings.TrimSpace(string(output)))
	}
	return output, nil
}

type Provider struct {
	Runner         Runner
	DockerBinary   string
	NSenterBinary  string
	NftBinary      string
	ManagementNet  string
	ArtifactRoot   string
	CredentialRoot string
}

func (p Provider) Create(ctx context.Context, record manager.Record) (string, error) {
	name := "riftx-" + record.Sandbox.ID
	managementDenies, err := p.managementDeniedCIDRs(ctx)
	if err != nil {
		return "", err
	}
	record.Request.Scope.DeniedCIDRs = append(record.Request.Scope.DeniedCIDRs, managementDenies...)
	credentialPath, err := p.writeCredential(record)
	if err != nil {
		return "", err
	}
	created := false
	containerCreated := false
	defer func() {
		if !created {
			if containerCreated {
				_, _ = p.Runner.Run(context.Background(), nil, p.DockerBinary, "rm", "-f", name)
			}
			_ = os.Remove(credentialPath)
		}
	}()
	args := []string{
		"create", "--name", name,
		"--label", "riftx.engagement=" + record.Sandbox.EngagementID,
		"--label", "riftx.policy-revision=" + record.Sandbox.PolicyRevision,
		"--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--pids-limit", strconv.FormatUint(uint64(record.Request.Resources.PIDsLimit), 10),
		"--memory", fmt.Sprintf("%dm", record.Request.Resources.MemoryMiB),
		"--cpus", strconv.FormatUint(uint64(record.Request.Resources.CPULimit), 10),
		"--network", p.ManagementNet,
		"--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
		"--tmpfs", "/workspace:rw,nosuid,size=1024m",
		"--mount", "type=bind,src=" + credentialPath + ",dst=/run/riftx/auth.json,readonly",
		record.Request.Image,
		"codex", "exec-server", "--listen", "ws://0.0.0.0:9800", "--riftx-auth-file", "/run/riftx/auth.json",
	}
	if _, err := p.Runner.Run(ctx, nil, p.DockerBinary, args...); err != nil {
		return "", err
	}
	containerCreated = true
	if _, err := p.Runner.Run(ctx, nil, p.DockerBinary, "start", name); err != nil {
		return "", err
	}
	pidOutput, err := p.Runner.Run(ctx, nil, p.DockerBinary, "inspect", "--format", "{{.State.Pid}}", name)
	if err != nil {
		return "", err
	}
	pid := strings.TrimSpace(string(pidOutput))
	policy := manager.RenderNftablesPolicy(record.Sandbox.ID, record.Request.Scope)
	if _, err := p.Runner.Run(ctx, []byte(policy), p.NSenterBinary, "-t", pid, "-n", p.NftBinary, "-f", "-"); err != nil {
		return "", err
	}
	ipOutput, err := p.Runner.Run(ctx, nil, p.DockerBinary, "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name)
	if err != nil {
		return "", err
	}
	ip := strings.TrimSpace(string(ipOutput))
	if ip == "" {
		return "", fmt.Errorf("container %s has no management IP", name)
	}
	created = true
	return "ws://" + ip + ":9800", nil
}

func (p Provider) managementDeniedCIDRs(ctx context.Context) ([]string, error) {
	output, err := p.Runner.Run(
		ctx,
		nil,
		p.DockerBinary,
		"network",
		"inspect",
		"--format",
		"{{json .IPAM.Config}}",
		p.ManagementNet,
	)
	if err != nil {
		return nil, err
	}
	var networks []struct {
		Subnet  string `json:"Subnet"`
		Gateway string `json:"Gateway"`
	}
	if err := json.Unmarshal(bytes.TrimSpace(output), &networks); err != nil {
		return nil, fmt.Errorf("parse Docker management network: %w", err)
	}
	if len(networks) == 0 {
		return nil, fmt.Errorf("Docker management network %q has no configured subnet", p.ManagementNet)
	}
	denied := make([]string, 0, len(networks)*2)
	for _, network := range networks {
		if network.Subnet != "" {
			denied = append(denied, network.Subnet)
		}
		if ip := net.ParseIP(network.Gateway); ip != nil {
			bits := 128
			if ip.To4() != nil {
				bits = 32
			}
			denied = append(denied, fmt.Sprintf("%s/%d", ip.String(), bits))
		}
	}
	return denied, nil
}

func (p Provider) Interrupt(ctx context.Context, record manager.Record) error {
	_, err := p.Runner.Run(ctx, nil, p.DockerBinary, "stop", "--time", "5", "riftx-"+record.Sandbox.ID)
	if err == nil {
		_ = os.Remove(p.credentialPath(record.Sandbox.ID))
	}
	return err
}

func (p Provider) Kill(ctx context.Context, record manager.Record) error {
	_, err := p.Runner.Run(ctx, nil, p.DockerBinary, "kill", "riftx-"+record.Sandbox.ID)
	if err == nil {
		_ = os.Remove(p.credentialPath(record.Sandbox.ID))
	}
	return err
}

func (p Provider) Delete(ctx context.Context, record manager.Record) error {
	_, err := p.Runner.Run(ctx, nil, p.DockerBinary, "rm", "-f", "riftx-"+record.Sandbox.ID)
	if err == nil {
		_ = os.Remove(p.credentialPath(record.Sandbox.ID))
	}
	return err
}

func (p Provider) writeCredential(record manager.Record) (string, error) {
	if err := os.MkdirAll(p.CredentialRoot, 0o700); err != nil {
		return "", err
	}
	path, err := filepath.Abs(p.credentialPath(record.Sandbox.ID))
	if err != nil {
		return "", err
	}
	payload, err := json.Marshal(struct {
		BootstrapSHA256 string `json:"bootstrapSha256"`
		ExpiresAt       int64  `json:"expiresAt"`
	}{
		BootstrapSHA256: hex.EncodeToString(record.TokenHash[:]),
		ExpiresAt:       record.ExpiresAt.Unix(),
	})
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(path, payload, 0o600); err != nil {
		return "", err
	}
	return path, nil
}

func (p Provider) credentialPath(sandboxID string) string {
	return filepath.Join(p.CredentialRoot, sandboxID+".json")
}

func (p Provider) ExportArtifact(ctx context.Context, record manager.Record, path string) (manager.ExportedArtifact, error) {
	directory := filepath.Join(p.ArtifactRoot, record.Sandbox.EngagementID, record.Sandbox.ID)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return manager.ExportedArtifact{}, err
	}
	destination := filepath.Join(directory, filepath.Base(path))
	source := "riftx-" + record.Sandbox.ID + ":/workspace/" + filepath.ToSlash(path)
	if _, err := p.Runner.Run(ctx, nil, p.DockerBinary, "cp", source, destination); err != nil {
		return manager.ExportedArtifact{}, err
	}
	file, err := os.Open(destination)
	if err != nil {
		return manager.ExportedArtifact{}, err
	}
	defer file.Close()
	hash := sha256.New()
	size, err := io.Copy(hash, file)
	if err != nil {
		return manager.ExportedArtifact{}, err
	}
	mediaType := mime.TypeByExtension(filepath.Ext(destination))
	if mediaType == "" {
		mediaType = "application/octet-stream"
	}
	return manager.ExportedArtifact{
		Path: destination, MediaType: mediaType, SHA256: hex.EncodeToString(hash.Sum(nil)), SizeBytes: uint64(size),
	}, nil
}
