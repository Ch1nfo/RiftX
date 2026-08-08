"""Publish bounded code-source reads into immutable Run Artifacts."""

from __future__ import annotations

import base64
import hashlib
import json

from pydantic import ValidationError

from riftx.application.errors import ApplicationConflictError
from riftx.code import CodePatchReceipt
from riftx.code.patch import (
    PatchFileState,
    parse_code_patch,
    prepare_code_patch,
    validate_patch_receipt_content,
)
from riftx.domain import ArtifactAccessClass, ArtifactContentTrust

from .artifacts import ArtifactApplicationService, RegisterArtifactContent


class ArtifactCodePublisher:
    def __init__(self, artifacts: ArtifactApplicationService) -> None:
        self._artifacts = artifacts

    async def publish(
        self,
        run_id: str,
        *,
        path: str,
        content: bytes,
        source_digest: str | None,
    ) -> str:
        path_digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]
        description = f"Immutable full code source for {path}"
        if source_digest is not None:
            description += f" from Snapshot {source_digest}"
        command = RegisterArtifactContent(
            content=content,
            name=f"code-source-{path_digest}.bin",
            mime_type="application/octet-stream",
            description=description,
            content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
        )
        artifact = await self._artifacts.register_content(run_id, command)
        return artifact.id

    async def publish_patch_receipt(
        self,
        run_id: str,
        receipt: CodePatchReceipt,
    ) -> str:
        content = json.dumps(
            receipt.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        artifact = await self._artifacts.register_content(
            run_id,
            RegisterArtifactContent(
                content=content,
                name=f"code-patch-receipt-{receipt.patch_sha256[:24]}.json",
                mime_type="application/vnd.riftx.code-patch-receipt+json",
                description=f"Immutable revert receipt for {receipt.path}",
                content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
            ),
        )
        return artifact.id

    async def load_patch_receipt(
        self,
        run_id: str,
        artifact_id: str,
    ) -> CodePatchReceipt:
        content = await self._artifacts.read_content_slice(
            artifact_id,
            expected_run_id=run_id,
            max_bytes=2 * 1024 * 1024,
        )
        if (
            not content.eof
            or content.artifact.access_class is not ArtifactAccessClass.PUBLIC_EXPORT
            or content.artifact.mime_type
            != "application/vnd.riftx.code-patch-receipt+json"
        ):
            raise _invalid_receipt()
        try:
            receipt = CodePatchReceipt.model_validate_json(content.data)
            original = (
                base64.b64decode(receipt.original_content_base64, validate=True)
                if receipt.original_content_base64 is not None
                else None
            )
        except (ValueError, ValidationError):
            raise _invalid_receipt() from None
        if receipt.run_id != run_id:
            raise _invalid_receipt()
        if hashlib.sha256(receipt.patch.encode("utf-8")).hexdigest() != receipt.patch_sha256:
            raise _invalid_receipt()
        if receipt.operation == "add":
            valid = (
                original is None
                and receipt.original_sha256 is None
                and receipt.original_mode is None
                and receipt.result_sha256 is not None
            )
        elif receipt.operation == "delete":
            valid = (
                original is not None
                and receipt.original_sha256 is not None
                and receipt.original_mode is not None
                and receipt.result_sha256 is None
            )
        else:
            valid = (
                original is not None
                and receipt.original_sha256 is not None
                and receipt.original_mode is not None
                and receipt.result_sha256 is not None
            )
        if not valid or (
            original is not None
            and hashlib.sha256(original).hexdigest() != receipt.original_sha256
        ):
            raise _invalid_receipt()
        try:
            if original is not None:
                validate_patch_receipt_content(
                    content=original,
                    expected_sha256=receipt.original_sha256,
                )
            parsed = parse_code_patch(receipt.patch)
            prepared = prepare_code_patch(
                parsed,
                expected_sha256=receipt.original_sha256,
                original=(
                    PatchFileState(
                        content=original,
                        sha256=receipt.original_sha256,
                        mode=receipt.original_mode,
                    )
                    if original is not None
                    and receipt.original_sha256 is not None
                    and receipt.original_mode is not None
                    else None
                ),
            )
        except ApplicationConflictError:
            raise _invalid_receipt() from None
        if (
            prepared.operation != receipt.operation
            or prepared.path != receipt.path
            or prepared.patch_sha256 != receipt.patch_sha256
            or prepared.result_sha256 != receipt.result_sha256
        ):
            raise _invalid_receipt()
        return receipt


def _invalid_receipt() -> ApplicationConflictError:
    return ApplicationConflictError(
        "code_patch_receipt_invalid",
        "Patch receipt failed immutable owner or content validation",
    )


__all__ = ["ArtifactCodePublisher"]
