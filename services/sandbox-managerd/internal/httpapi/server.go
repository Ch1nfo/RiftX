package httpapi

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/riftx-dev/riftx/services/sandbox-managerd/internal/manager"
)

type Server struct {
	manager *manager.Service
}

func New(managerService *manager.Service) *Server {
	return &Server{manager: managerService}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/sandboxes", s.createSandbox)
	mux.HandleFunc("GET /v1/events", s.events)
	mux.HandleFunc("/v1/sandboxes/", s.sandbox)
	return mux
}

func (s *Server) createSandbox(response http.ResponseWriter, request *http.Request) {
	var params manager.CreateRequest
	if err := decodeJSON(request, &params); err != nil {
		writeError(response, http.StatusBadRequest, err)
		return
	}
	sandbox, err := s.manager.Create(request.Context(), params)
	if err != nil {
		writeManagerError(response, err)
		return
	}
	writeJSON(response, http.StatusCreated, sandbox)
}

func (s *Server) sandbox(response http.ResponseWriter, request *http.Request) {
	path := strings.TrimPrefix(request.URL.Path, "/v1/sandboxes/")
	parts := strings.Split(path, "/")
	if len(parts) == 0 || parts[0] == "" {
		writeError(response, http.StatusNotFound, manager.ErrNotFound)
		return
	}
	id := parts[0]
	if len(parts) == 1 {
		s.sandboxRoot(response, request, id)
		return
	}
	if request.Method != http.MethodPost {
		writeError(response, http.StatusMethodNotAllowed, errors.New("method not allowed"))
		return
	}
	switch strings.Join(parts[1:], "/") {
	case "interrupt":
		sandbox, err := s.manager.Interrupt(request.Context(), id)
		s.writeSandboxResult(response, sandbox, err)
	case "kill":
		sandbox, err := s.manager.Kill(request.Context(), id)
		s.writeSandboxResult(response, sandbox, err)
	case "artifacts/export":
		var params manager.ExportArtifactRequest
		if err := decodeJSON(request, &params); err != nil {
			writeError(response, http.StatusBadRequest, err)
			return
		}
		artifact, err := s.manager.ExportArtifact(request.Context(), id, params.Path)
		if err != nil {
			writeManagerError(response, err)
			return
		}
		writeJSON(response, http.StatusOK, artifact)
	default:
		writeError(response, http.StatusNotFound, manager.ErrNotFound)
	}
}

func (s *Server) sandboxRoot(response http.ResponseWriter, request *http.Request, id string) {
	switch request.Method {
	case http.MethodGet:
		sandbox, err := s.manager.Get(id)
		s.writeSandboxResult(response, sandbox, err)
	case http.MethodDelete:
		if err := s.manager.Delete(request.Context(), id); err != nil {
			writeManagerError(response, err)
			return
		}
		response.WriteHeader(http.StatusNoContent)
	default:
		writeError(response, http.StatusMethodNotAllowed, errors.New("method not allowed"))
	}
}

func (s *Server) events(response http.ResponseWriter, request *http.Request) {
	events, err := s.manager.Events(request.URL.Query().Get("cursor"))
	if err != nil {
		writeManagerError(response, err)
		return
	}
	writeJSON(response, http.StatusOK, events)
}

func (s *Server) writeSandboxResult(response http.ResponseWriter, sandbox manager.Sandbox, err error) {
	if err != nil {
		writeManagerError(response, err)
		return
	}
	writeJSON(response, http.StatusOK, sandbox)
}

func decodeJSON(request *http.Request, value any) error {
	defer request.Body.Close()
	decoder := json.NewDecoder(io.LimitReader(request.Body, 1<<20))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		return fmt.Errorf("invalid JSON: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("request body must contain one JSON value")
	}
	return nil
}

func writeManagerError(response http.ResponseWriter, err error) {
	status := http.StatusInternalServerError
	switch {
	case errors.Is(err, manager.ErrNotFound):
		status = http.StatusNotFound
	case errors.Is(err, manager.ErrInvalidRequest):
		status = http.StatusBadRequest
	}
	writeError(response, status, err)
}

func writeError(response http.ResponseWriter, status int, err error) {
	writeJSON(response, status, map[string]string{"error": err.Error()})
}

func writeJSON(response http.ResponseWriter, status int, value any) {
	response.Header().Set("Content-Type", "application/json")
	response.WriteHeader(status)
	_ = json.NewEncoder(response).Encode(value)
}
