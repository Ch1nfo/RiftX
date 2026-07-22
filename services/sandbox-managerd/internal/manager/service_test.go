package manager

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"
)

type fakeResolver map[string][]net.IP

func (r fakeResolver) LookupIP(_ context.Context, _ string, host string) ([]net.IP, error) {
	return r[host], nil
}

type fakeProvider struct{}

func (fakeProvider) Create(context.Context, Record) (string, error) {
	return "ws://10.0.0.2:9800", nil
}
func (fakeProvider) Interrupt(context.Context, Record) error { return nil }
func (fakeProvider) Kill(context.Context, Record) error      { return nil }
func (fakeProvider) Delete(context.Context, Record) error    { return nil }
func (fakeProvider) ExportArtifact(context.Context, Record, string) (ExportedArtifact, error) {
	return ExportedArtifact{}, nil
}

func validRequest() CreateRequest {
	return CreateRequest{
		EngagementID: "eng-1", Image: "riftx/sandbox:test", Profile: "recon", PolicyRevision: "rev-1",
		Resources: Resources{CPULimit: 2, MemoryMiB: 1024, PIDsLimit: 256},
		Scope:     Scope{CIDRs: []string{"10.10.0.0/24"}, Ports: []uint16{80}},
	}
}

func TestBootstrapTokenIsReturnedOnceAndOnlyHashIsStored(t *testing.T) {
	service := NewService(fakeProvider{}, time.Minute)
	sandbox, err := service.Create(context.Background(), validRequest())
	if err != nil {
		t.Fatal(err)
	}
	if sandbox.BootstrapToken == nil || len(*sandbox.BootstrapToken) < 40 {
		t.Fatal("create must return a high-entropy bootstrap token")
	}
	stored, err := service.Get(sandbox.ID)
	if err != nil {
		t.Fatal(err)
	}
	if stored.BootstrapToken != nil {
		t.Fatal("stored sandbox must not expose bootstrap token")
	}
	record, err := service.record(sandbox.ID)
	if err != nil {
		t.Fatal(err)
	}
	if record.TokenHash == [32]byte{} {
		t.Fatal("stored bootstrap token hash must not be empty")
	}
}

func TestScopeAlwaysIncludesBuiltInDenies(t *testing.T) {
	scope, err := normalizeScope(validRequest().Scope)
	if err != nil {
		t.Fatal(err)
	}
	foundMetadata := false
	for _, cidr := range scope.DeniedCIDRs {
		if cidr == "169.254.0.0/16" {
			foundMetadata = true
		}
	}
	if !foundMetadata {
		t.Fatal("metadata CIDR deny is missing")
	}
	policy := RenderNftablesPolicy("sandbox-1", scope)
	if policy == "" || policy[len(policy)-2:] != "}\n" {
		t.Fatalf("unexpected nftables policy: %q", policy)
	}
}

func TestDomainScopeIsResolvedAndPinnedAtCreation(t *testing.T) {
	resolver := fakeResolver{"target.example": {net.ParseIP("203.0.113.10")}}
	service := NewServiceWithResolver(fakeProvider{}, time.Minute, resolver)
	request := validRequest()
	request.Scope.Domains = []string{"Target.Example."}
	sandbox, err := service.Create(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	record, err := service.record(sandbox.ID)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, cidr := range record.Request.Scope.CIDRs {
		if cidr == "203.0.113.10/32" {
			found = true
		}
	}
	if !found {
		t.Fatalf("resolved scope = %#v", record.Request.Scope.CIDRs)
	}
}

func TestAllowedDomainCannotOverrideDeniedParent(t *testing.T) {
	request := validRequest()
	request.Scope.Domains = []string{"api.internal.example"}
	request.Scope.DeniedDomains = []string{"internal.example"}
	service := NewService(fakeProvider{}, time.Minute)
	if _, err := service.Create(context.Background(), request); !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("create error = %v", err)
	}
}
