package manager

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

var (
	ErrNotFound       = errors.New("sandbox not found")
	ErrInvalidRequest = errors.New("invalid request")
)

type Service struct {
	mu       sync.RWMutex
	provider Provider
	records  map[string]Record
	events   []Event
	tokenTTL time.Duration
	resolver Resolver
}

func NewService(provider Provider, tokenTTL time.Duration) *Service {
	return NewServiceWithResolver(provider, tokenTTL, systemResolver{})
}

func NewServiceWithResolver(provider Provider, tokenTTL time.Duration, resolver Resolver) *Service {
	return &Service{
		provider: provider,
		records:  make(map[string]Record),
		tokenTTL: tokenTTL,
		resolver: resolver,
	}
}

func (s *Service) Create(ctx context.Context, request CreateRequest) (Sandbox, error) {
	if strings.TrimSpace(request.EngagementID) == "" || strings.TrimSpace(request.Image) == "" ||
		strings.TrimSpace(request.Profile) == "" || strings.TrimSpace(request.PolicyRevision) == "" {
		return Sandbox{}, fmt.Errorf("%w: required field is empty", ErrInvalidRequest)
	}
	if request.Resources.CPULimit == 0 || request.Resources.MemoryMiB == 0 || request.Resources.PIDsLimit == 0 {
		return Sandbox{}, fmt.Errorf("%w: resource limits must be non-zero", ErrInvalidRequest)
	}
	scope, err := normalizeScope(request.Scope)
	if err != nil {
		return Sandbox{}, fmt.Errorf("%w: %v", ErrInvalidRequest, err)
	}
	scope, err = resolveScope(ctx, s.resolver, scope)
	if err != nil {
		return Sandbox{}, fmt.Errorf("%w: %v", ErrInvalidRequest, err)
	}
	request.Scope = scope
	id, err := randomID()
	if err != nil {
		return Sandbox{}, err
	}
	token, tokenHash, err := newBootstrapToken()
	if err != nil {
		return Sandbox{}, err
	}
	now := time.Now().UTC()
	record := Record{
		Sandbox: Sandbox{
			ID:             id,
			EngagementID:   request.EngagementID,
			Status:         StatusCreating,
			EnvironmentID:  "sandbox-" + id,
			PolicyRevision: request.PolicyRevision,
			CreatedAt:      now.Unix(),
		},
		Request:   request,
		TokenHash: tokenHash,
		ExpiresAt: now.Add(s.tokenTTL),
	}
	execServerURL, err := s.provider.Create(ctx, record)
	if err != nil {
		return Sandbox{}, err
	}
	record.Sandbox.Status = StatusReady
	record.Sandbox.ExecServerURL = execServerURL
	s.mu.Lock()
	s.records[id] = record
	s.appendEventLocked(id, "sandboxReady", nil)
	s.mu.Unlock()
	response := record.Sandbox
	response.BootstrapToken = &token
	return response, nil
}

func (s *Service) Get(id string) (Sandbox, error) {
	s.mu.RLock()
	record, ok := s.records[id]
	s.mu.RUnlock()
	if !ok {
		return Sandbox{}, ErrNotFound
	}
	record.Sandbox.BootstrapToken = nil
	return record.Sandbox, nil
}

func (s *Service) Interrupt(ctx context.Context, id string) (Sandbox, error) {
	return s.changeStatus(ctx, id, StatusInterrupted, s.provider.Interrupt, "sandboxInterrupted")
}

func (s *Service) Kill(ctx context.Context, id string) (Sandbox, error) {
	return s.changeStatus(ctx, id, StatusStopped, s.provider.Kill, "sandboxKilled")
}

func (s *Service) Delete(ctx context.Context, id string) error {
	record, err := s.record(id)
	if err != nil {
		return err
	}
	if err := s.provider.Delete(ctx, record); err != nil {
		return err
	}
	s.mu.Lock()
	delete(s.records, id)
	s.appendEventLocked(id, "sandboxDeleted", nil)
	s.mu.Unlock()
	return nil
}

func (s *Service) ExportArtifact(ctx context.Context, id, path string) (ExportedArtifact, error) {
	clean := filepath.Clean(path)
	if filepath.IsAbs(clean) || clean == "." || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return ExportedArtifact{}, fmt.Errorf("%w: artifact path must be relative", ErrInvalidRequest)
	}
	record, err := s.record(id)
	if err != nil {
		return ExportedArtifact{}, err
	}
	return s.provider.ExportArtifact(ctx, record, clean)
}

func (s *Service) Events(cursor string) (EventsResponse, error) {
	start := 0
	if cursor != "" {
		parsed, err := strconv.Atoi(cursor)
		if err != nil || parsed < 0 {
			return EventsResponse{}, fmt.Errorf("%w: invalid cursor", ErrInvalidRequest)
		}
		start = parsed
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	if start > len(s.events) {
		start = len(s.events)
	}
	events := append([]Event(nil), s.events[start:]...)
	next := strconv.Itoa(len(s.events))
	return EventsResponse{Events: events, NextCursor: &next}, nil
}

func (s *Service) changeStatus(ctx context.Context, id string, status Status, action func(context.Context, Record) error, event string) (Sandbox, error) {
	record, err := s.record(id)
	if err != nil {
		return Sandbox{}, err
	}
	if err := action(ctx, record); err != nil {
		return Sandbox{}, err
	}
	record.Sandbox.Status = status
	record.TokenHash = [32]byte{}
	s.mu.Lock()
	s.records[id] = record
	s.appendEventLocked(id, event, nil)
	s.mu.Unlock()
	return record.Sandbox, nil
}

func (s *Service) record(id string) (Record, error) {
	s.mu.RLock()
	record, ok := s.records[id]
	s.mu.RUnlock()
	if !ok {
		return Record{}, ErrNotFound
	}
	return record, nil
}

func (s *Service) appendEventLocked(sandboxID, kind string, detail *string) {
	cursor := strconv.Itoa(len(s.events) + 1)
	s.events = append(s.events, Event{
		Cursor: cursor, SandboxID: sandboxID, Kind: kind, Timestamp: time.Now().UTC().Unix(), Detail: detail,
	})
}

func randomID() (string, error) {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}
