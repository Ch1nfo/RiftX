package manager

import "context"

type Provider interface {
	Create(context.Context, Record) (string, error)
	Interrupt(context.Context, Record) error
	Kill(context.Context, Record) error
	Delete(context.Context, Record) error
	ExportArtifact(context.Context, Record, string) (ExportedArtifact, error)
}
