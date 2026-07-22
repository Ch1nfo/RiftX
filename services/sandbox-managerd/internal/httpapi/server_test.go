package httpapi

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/riftx-dev/riftx/services/sandbox-managerd/internal/manager"
)

type fakeProvider struct{}

func (fakeProvider) Create(context.Context, manager.Record) (string, error) {
	return "ws://10.0.0.2:9800", nil
}
func (fakeProvider) Interrupt(context.Context, manager.Record) error { return nil }
func (fakeProvider) Kill(context.Context, manager.Record) error      { return nil }
func (fakeProvider) Delete(context.Context, manager.Record) error    { return nil }
func (fakeProvider) ExportArtifact(context.Context, manager.Record, string) (manager.ExportedArtifact, error) {
	return manager.ExportedArtifact{}, nil
}

func TestCreateRejectsUnknownJSONFields(t *testing.T) {
	service := manager.NewService(fakeProvider{}, time.Minute)
	request := httptest.NewRequest(http.MethodPost, "/v1/sandboxes", bytes.NewBufferString(`{"unknown":true}`))
	response := httptest.NewRecorder()
	New(service).Handler().ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusBadRequest)
	}
}
