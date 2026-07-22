package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/riftx-dev/riftx/services/sandbox-managerd/internal/docker"
	"github.com/riftx-dev/riftx/services/sandbox-managerd/internal/httpapi"
	"github.com/riftx-dev/riftx/services/sandbox-managerd/internal/manager"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	socket := flag.String("socket", ".riftx/managerd.sock", "Unix socket path")
	artifactRoot := flag.String("artifact-root", ".riftx/artifacts", "artifact export root")
	credentialRoot := flag.String("credential-root", ".riftx/credentials", "ephemeral credential root")
	managementNet := flag.String("management-network", "riftx-management", "isolated Docker management network")
	flag.Parse()
	if err := os.MkdirAll(filepath.Dir(*socket), 0o700); err != nil {
		return err
	}
	if info, err := os.Lstat(*socket); err == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return fmt.Errorf("refusing to replace non-socket path %s", *socket)
		}
		if err := os.Remove(*socket); err != nil {
			return err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	listener, err := net.Listen("unix", *socket)
	if err != nil {
		return err
	}
	defer listener.Close()
	defer os.Remove(*socket)
	if err := os.Chmod(*socket, 0o600); err != nil {
		return err
	}
	provider := docker.Provider{
		Runner: docker.CommandRunner{}, DockerBinary: "docker", NSenterBinary: "nsenter", NftBinary: "nft",
		ManagementNet: *managementNet, ArtifactRoot: *artifactRoot, CredentialRoot: *credentialRoot,
	}
	reconcileContext, cancelReconcile := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancelReconcile()
	if err := provider.EnsureManagementNetwork(reconcileContext); err != nil {
		return fmt.Errorf("prepare management network: %w", err)
	}
	if err := provider.Reconcile(reconcileContext); err != nil {
		return fmt.Errorf("reconcile stale sandboxes: %w", err)
	}
	service := manager.NewService(provider, 5*time.Minute)
	server := &http.Server{
		Handler:           httpapi.New(service).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-stop
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(ctx)
	}()
	if err := server.Serve(listener); !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}
