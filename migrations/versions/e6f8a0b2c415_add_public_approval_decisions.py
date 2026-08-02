"""add authoritative public Approval decisions

Revision ID: e6f8a0b2c415
Revises: d5e7f9a1b304
Create Date: 2026-08-01 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f8a0b2c415"
down_revision: str | None = "d5e7f9a1b304"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("decision", sa.String(32), nullable=True),
    )
    op.add_column(
        "approvals",
        sa.Column("decision_feedback", sa.Text(), nullable=True),
    )

    # A same-ID terminal Runtime Approval is the strongest surviving evidence
    # after a historical split write. Only self-consistent status/decision pairs
    # are authoritative; malformed rows remain visible for fail-closed handling.
    op.execute(
        sa.text(
            """
            UPDATE approvals
            SET decision = (
                    SELECT runtime_approval_requests.decision
                    FROM runtime_approval_requests
                    WHERE runtime_approval_requests.id = approvals.id
                      AND runtime_approval_requests.run_id = approvals.run_id
                      AND (
                          (
                              runtime_approval_requests.status = 'approved'
                              AND runtime_approval_requests.decision IN (
                                  'approve_once', 'approve_tool_for_run'
                              )
                          )
                          OR (
                              runtime_approval_requests.status = 'rejected'
                              AND runtime_approval_requests.decision IN (
                                  'reject', 'reject_with_feedback'
                              )
                          )
                      )
                ),
                decision_feedback = (
                    SELECT runtime_approval_requests.feedback
                    FROM runtime_approval_requests
                    WHERE runtime_approval_requests.id = approvals.id
                      AND runtime_approval_requests.run_id = approvals.run_id
                      AND (
                          (
                              runtime_approval_requests.status = 'approved'
                              AND runtime_approval_requests.decision IN (
                                  'approve_once', 'approve_tool_for_run'
                              )
                          )
                          OR (
                              runtime_approval_requests.status = 'rejected'
                              AND runtime_approval_requests.decision IN (
                                  'reject', 'reject_with_feedback'
                              )
                          )
                      )
                )
            WHERE approvals.decision IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM runtime_approval_requests
                  WHERE runtime_approval_requests.id = approvals.id
                    AND runtime_approval_requests.run_id = approvals.run_id
                    AND (
                        (
                            runtime_approval_requests.status = 'approved'
                            AND runtime_approval_requests.decision IN (
                                'approve_once', 'approve_tool_for_run'
                            )
                        )
                        OR (
                            runtime_approval_requests.status = 'rejected'
                            AND runtime_approval_requests.decision IN (
                                'reject', 'reject_with_feedback'
                            )
                        )
                    )
              )
            """
        )
    )

    # A Run-wide grant has no source Approval ID.  It may have been created by
    # a later Approval for the same Run/Tool, so it cannot prove this row's
    # original scope.  Preserve the historical fail-closed recovery contract:
    # without a same-ID terminal Runtime decision, an approval is one-shot.
    op.execute(
        sa.text(
            """
            UPDATE approvals
            SET decision = 'approve_once',
                decision_feedback = NULL
            WHERE approvals.decision IS NULL
              AND approvals.status = 'approved'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE approvals
            SET decision = CASE
                    WHEN TRIM(COALESCE(approvals.reason, '')) <> ''
                    THEN 'reject_with_feedback'
                    ELSE 'reject'
                END,
                decision_feedback = CASE
                    WHEN TRIM(COALESCE(approvals.reason, '')) <> ''
                    THEN approvals.reason
                    ELSE NULL
                END
            WHERE approvals.decision IS NULL
              AND approvals.status = 'rejected'
            """
        )
    )


def downgrade() -> None:
    op.drop_column("approvals", "decision_feedback")
    op.drop_column("approvals", "decision")
