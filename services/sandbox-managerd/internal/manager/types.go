package manager

import "time"

type Resources struct {
	CPULimit  uint16 `json:"cpuLimit"`
	MemoryMiB uint32 `json:"memoryMib"`
	PIDsLimit uint32 `json:"pidsLimit"`
}

type Scope struct {
	CIDRs         []string `json:"cidrs"`
	Domains       []string `json:"domains"`
	Ports         []uint16 `json:"ports"`
	DeniedCIDRs   []string `json:"deniedCidrs"`
	DeniedDomains []string `json:"deniedDomains"`
}

type CreateRequest struct {
	EngagementID   string    `json:"engagementId"`
	Image          string    `json:"image"`
	Profile        string    `json:"profile"`
	PolicyRevision string    `json:"policyRevision"`
	Resources      Resources `json:"resources"`
	Scope          Scope     `json:"scope"`
}

type Status string

const (
	StatusCreating    Status = "creating"
	StatusReady       Status = "ready"
	StatusInterrupted Status = "interrupted"
	StatusStopped     Status = "stopped"
	StatusFailed      Status = "failed"
)

type Sandbox struct {
	ID             string  `json:"id"`
	EngagementID   string  `json:"engagementId"`
	Status         Status  `json:"status"`
	EnvironmentID  string  `json:"environmentId"`
	ExecServerURL  string  `json:"execServerUrl"`
	BootstrapToken *string `json:"bootstrapToken"`
	PolicyRevision string  `json:"policyRevision"`
	CreatedAt      int64   `json:"createdAt"`
}

type Event struct {
	Cursor    string  `json:"cursor"`
	SandboxID string  `json:"sandboxId"`
	Kind      string  `json:"kind"`
	Timestamp int64   `json:"timestamp"`
	Detail    *string `json:"detail"`
}

type EventsResponse struct {
	Events     []Event `json:"events"`
	NextCursor *string `json:"nextCursor"`
}

type ExportArtifactRequest struct {
	Path string `json:"path"`
}

type ExportedArtifact struct {
	Path      string `json:"path"`
	MediaType string `json:"mediaType"`
	SHA256    string `json:"sha256"`
	SizeBytes uint64 `json:"sizeBytes"`
}

type Record struct {
	Sandbox   Sandbox
	Request   CreateRequest
	TokenHash [32]byte
	ExpiresAt time.Time
}
