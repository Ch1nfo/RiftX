"""Model-profile configuration endpoints."""

from fastapi import APIRouter

from ..dependencies import ModelProfileAdminDependency, ModelProfileServiceDependency
from ..schemas import (
    ErrorResponse,
    ModelProfileListResponse,
    ModelProfileResponse,
    ModelProfileSummaryListResponse,
    ModelProfileUpdateRequest,
    SetDefaultModelProfileRequest,
)

router = APIRouter(prefix="/model-profiles", tags=["model-profiles"])

_ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@router.get("", response_model=ModelProfileSummaryListResponse, responses=_ERROR_RESPONSES)
async def list_model_profiles(
    service: ModelProfileServiceDependency,
) -> ModelProfileSummaryListResponse:
    return ModelProfileSummaryListResponse.from_view(await service.list_profiles())


@router.get("/admin", response_model=ModelProfileListResponse, responses=_ERROR_RESPONSES)
async def list_model_profiles_for_admin(
    service: ModelProfileServiceDependency,
    _authorized: ModelProfileAdminDependency,
) -> ModelProfileListResponse:
    return ModelProfileListResponse.from_view(await service.list_profiles())


@router.put("/default", response_model=ModelProfileListResponse, responses=_ERROR_RESPONSES)
async def set_default_model_profile(
    request: SetDefaultModelProfileRequest,
    service: ModelProfileServiceDependency,
    _authorized: ModelProfileAdminDependency,
) -> ModelProfileListResponse:
    return ModelProfileListResponse.from_view(await service.set_default(request.profile))


@router.get("/{profile_name}", response_model=ModelProfileResponse, responses=_ERROR_RESPONSES)
async def get_model_profile(
    profile_name: str,
    service: ModelProfileServiceDependency,
    _authorized: ModelProfileAdminDependency,
) -> ModelProfileResponse:
    return ModelProfileResponse.from_view(await service.get_profile(profile_name))


@router.put("/{profile_name}", response_model=ModelProfileResponse, responses=_ERROR_RESPONSES)
async def upsert_model_profile(
    profile_name: str,
    request: ModelProfileUpdateRequest,
    service: ModelProfileServiceDependency,
    _authorized: ModelProfileAdminDependency,
) -> ModelProfileResponse:
    api_key = request.api_key.get_secret_value() if request.api_key is not None else None
    return ModelProfileResponse.from_view(
        await service.upsert_profile(
            profile_name,
            request.to_profile(),
            api_key=api_key,
            clear_api_key=request.clear_stored_api_key,
        )
    )


@router.delete(
    "/{profile_name}",
    response_model=ModelProfileListResponse,
    responses=_ERROR_RESPONSES,
)
async def delete_model_profile(
    profile_name: str,
    service: ModelProfileServiceDependency,
    _authorized: ModelProfileAdminDependency,
) -> ModelProfileListResponse:
    return ModelProfileListResponse.from_view(await service.delete_profile(profile_name))
