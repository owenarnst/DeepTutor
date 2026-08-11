"""Authenticated Course Mode import, processing, and manifest routes."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile as StarletteUploadFile

from deeptutor.course_mode.models import (
    Course,
    CourseJobStatus,
    CourseManifest,
    CourseProcessingJob,
    ManifestRole,
    ManifestVisibility,
)
from deeptutor.course_mode.repository import (
    DEFAULT_COURSE_LIST_LIMIT,
    MAX_COURSE_LIST_LIMIT,
    MAX_COURSE_LIST_OFFSET,
    CourseImportResult,
    CourseInput,
    CourseQuotaExceededError,
    CourseRepository,
    IdempotencyConflictError,
    InvalidCourseIdentifierError,
    InvalidCourseInputError,
    InvalidJobRetryError,
    InvalidManifestError,
    InvalidManifestStateError,
    InvalidRequestKeyError,
    ManifestApprovalBlockedError,
    ManifestRevisionConflictError,
)
from deeptutor.course_mode.source_processing import (
    COURSE_MAX_FILE_BYTES,
    COURSE_MAX_TOTAL_BYTES,
    COURSE_MAX_UPLOAD_COUNT,
    InvalidCourseSourceError,
)
from deeptutor.multi_user.context import get_current_user
from deeptutor.multi_user.paths import get_current_course_path_service
from deeptutor.services.config.runtime_settings import load_system_settings

router = APIRouter()

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]


class CreateCourseRequest(BaseModel):
    """Document the strict multipart creation fields for OpenAPI consumers."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    unit_title: str = Field(min_length=1, max_length=200)
    desired_outcome: str = Field(min_length=1, max_length=2000)
    weekly_minutes: int = Field(ge=1, le=10_080)
    ocw_url: str
    scheduling: str | None = Field(default=None, max_length=2000)
    difficulty: str | None = Field(default=None, max_length=2000)
    accessibility: str | None = Field(default=None, max_length=2000)


class CourseResponse(BaseModel):
    course: Course


class ProcessingResponse(BaseModel):
    job: CourseProcessingJob


class CreateCourseResponse(CourseResponse):
    created: bool
    processing_job: CourseProcessingJob


class CourseListResponse(BaseModel):
    courses: list[Course]
    total: int
    has_more: bool
    next_offset: int | None


class ManifestEntryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    role: ManifestRole | None = None
    visibility: ManifestVisibility | None = None
    role_confirmed: bool | None = None
    visibility_confirmed: bool | None = None


class ManifestUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    entries: list[ManifestEntryUpdate] = Field(min_length=1)


class ManifestApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)


def get_course_repository() -> CourseRepository:
    user = get_current_user()
    max_courses = load_system_settings()["course_max_per_owner"]
    return CourseRepository(
        get_current_course_path_service(),
        owner_scope=user.id,
        max_courses=max_courses,
    )


def _safe_error(exc: Exception, *, default: str) -> HTTPException:
    if isinstance(exc, IdempotencyConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, CourseQuotaExceededError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, InvalidCourseIdentifierError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")
    if isinstance(
        exc,
        (
            InvalidCourseInputError,
            InvalidCourseSourceError,
            InvalidRequestKeyError,
            InvalidManifestError,
        ),
    ):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    if isinstance(exc, InvalidManifestStateError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, ManifestRevisionConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, ManifestApprovalBlockedError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "manifest_review_incomplete", "blockers": list(exc.blockers)},
        )
    if isinstance(exc, InvalidJobRetryError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=default)


def _form_value(form: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = form.get(name)
        if value is not None:
            return value
    return default


def _required_form_text(value: Any, label: str, *, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise InvalidCourseInputError(f"{label} must be text")
    return value


def _optional_form_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise InvalidCourseInputError("Optional Course input is invalid") from exc


async def _parse_multipart_course(request: Request) -> tuple[CourseInput, list[tuple[str, bytes]]]:
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        # The source-less JSON pilot contract is intentionally retired. Keep
        # this explicit so clients cannot accidentally create an unprocessable
        # Course while believing an upload was accepted.
        raise InvalidCourseSourceError("Course creation requires at least one uploaded source file")
    form = await request.form()
    if form.get("user_id") is not None or form.get("owner_scope") is not None:
        raise InvalidCourseInputError("Course ownership is assigned by authentication")
    title = _form_value(form, "title")
    unit_title = _form_value(form, "unit_title", "unitTitle")
    desired_outcome = _form_value(form, "desired_outcome", "desiredOutcome")
    weekly_minutes_raw = _form_value(form, "weekly_minutes", "weeklyMinutes")
    ocw_url = _form_value(form, "ocw_url", "ocwUrl", "source_url")
    title_text = _required_form_text(title, "Title")
    unit_title_text = _required_form_text(unit_title, "Unit title")
    desired_outcome_text = _required_form_text(desired_outcome, "Desired outcome")
    ocw_url_text = _required_form_text(ocw_url, "OCW URL")
    try:
        weekly_minutes = int(weekly_minutes_raw)
    except (TypeError, ValueError) as exc:
        raise InvalidCourseInputError("Weekly minutes must be an integer") from exc
    uploads: list[tuple[str, bytes]] = []
    total_upload_bytes = 0
    upload_count = 0
    for field_name in ("files", "uploads", "source_files", "file"):
        for item in form.getlist(field_name):
            # Request.form() yields Starlette's base UploadFile even though
            # FastAPI exposes its subclass for endpoint annotations.
            if isinstance(item, StarletteUploadFile):
                upload_count += 1
                if upload_count > COURSE_MAX_UPLOAD_COUNT:
                    raise InvalidCourseSourceError(
                        "Course source file count exceeds the request limit"
                    )
                filename = item.filename or ""
                content = await item.read(COURSE_MAX_FILE_BYTES + 1)
                if len(content) > COURSE_MAX_FILE_BYTES:
                    raise InvalidCourseSourceError(
                        "Uploaded source exceeds the per-file size limit"
                    )
                total_upload_bytes += len(content)
                if total_upload_bytes > COURSE_MAX_TOTAL_BYTES:
                    raise InvalidCourseSourceError(
                        "Uploaded Course sources exceed the batch size limit"
                    )
                uploads.append((filename, content))
        if uploads:
            break
    course_input = CourseInput(
        title=title_text,
        description=_required_form_text(
            _form_value(form, "description", default=""), "Description"
        ),
        unit_title=unit_title_text,
        desired_outcome=desired_outcome_text,
        weekly_minutes=weekly_minutes,
        ocw_url=ocw_url_text,
        scheduling=_optional_form_text(_form_value(form, "scheduling", "schedule")),
        difficulty=_optional_form_text(_form_value(form, "difficulty")),
        accessibility=_optional_form_text(_form_value(form, "accessibility")),
    )
    return course_input, uploads


def _course_response(result: CourseImportResult) -> CreateCourseResponse:
    return CreateCourseResponse(
        course=result.course,
        created=result.created,
        processing_job=result.processing_job,
    )


@router.post("", response_model=CreateCourseResponse)
async def create_course(
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKey,
) -> CreateCourseResponse:
    try:
        course_input, uploads = await _parse_multipart_course(request)
        repository = get_course_repository()
        result = await asyncio.to_thread(
            repository.create_import,
            idempotency_key,
            course_input,
            uploads,
        )
        if result.processing_job.status is CourseJobStatus.QUEUED:
            job = await asyncio.to_thread(repository.process_import, result.processing_job.id)
            result = CourseImportResult(
                course=repository.get(result.course.id) or result.course,
                processing_job=job,
                created=result.created,
            )
    except Exception as exc:
        raise _safe_error(exc, default="Course import could not be created") from exc
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return _course_response(result)


@router.get("/jobs/{job_id}", response_model=ProcessingResponse)
async def get_processing_job(job_id: str) -> ProcessingResponse:
    repository = get_course_repository()
    try:
        job = await asyncio.to_thread(repository.get_processing_job_by_id, job_id)
    except InvalidCourseIdentifierError as exc:
        raise HTTPException(status_code=400, detail="Processing job not found") from exc
    if job is None:
        raise HTTPException(status_code=404, detail="Processing job not found")
    return ProcessingResponse(job=job)


@router.post("/jobs/{job_id}/retry", response_model=ProcessingResponse)
async def retry_processing_job(job_id: str) -> ProcessingResponse:
    repository = get_course_repository()
    try:
        job = await asyncio.to_thread(repository.retry_import, job_id)
    except Exception as exc:
        raise _safe_error(exc, default="Processing job could not be retried") from exc
    return ProcessingResponse(job=job)


@router.post("/{course_id}/processing/retry", response_model=ProcessingResponse)
@router.post("/{course_id}/retry", response_model=ProcessingResponse)
async def retry_course_processing(course_id: str) -> ProcessingResponse:
    repository = get_course_repository()
    try:
        current = await asyncio.to_thread(repository.get_processing_job, course_id)
        if current is None:
            raise InvalidCourseIdentifierError("Processing job not found")
        job = await asyncio.to_thread(repository.retry_import, current.id)
    except Exception as exc:
        raise _safe_error(exc, default="Processing job could not be retried") from exc
    return ProcessingResponse(job=job)


@router.get("", response_model=CourseListResponse)
async def list_courses(
    limit: Annotated[int, Query(ge=1, le=MAX_COURSE_LIST_LIMIT)] = (DEFAULT_COURSE_LIST_LIMIT),
    offset: Annotated[int, Query(ge=0, le=MAX_COURSE_LIST_OFFSET)] = 0,
) -> CourseListResponse:
    repository = get_course_repository()
    page = await asyncio.to_thread(repository.list_page, limit=limit, offset=offset)
    return CourseListResponse(
        courses=list(page.courses),
        total=page.total,
        has_more=page.has_more,
        next_offset=page.next_offset,
    )


@router.get("/{course_id}/processing", response_model=ProcessingResponse)
async def get_course_processing(course_id: str) -> ProcessingResponse:
    repository = get_course_repository()
    try:
        job = await asyncio.to_thread(repository.get_processing_job, course_id)
    except InvalidCourseIdentifierError as exc:
        raise HTTPException(status_code=400, detail="Invalid course id") from exc
    if job is None:
        raise HTTPException(status_code=404, detail="Processing job not found")
    return ProcessingResponse(job=job)


@router.get("/{course_id}/manifest", response_model=CourseManifest)
async def get_course_manifest(course_id: str) -> CourseManifest:
    repository = get_course_repository()
    try:
        return await asyncio.to_thread(repository.get_manifest, course_id)
    except InvalidCourseIdentifierError as exc:
        raise HTTPException(status_code=404, detail="Course manifest not found") from exc


@router.patch("/{course_id}/manifest", response_model=CourseManifest)
@router.put("/{course_id}/manifest", response_model=CourseManifest)
async def update_course_manifest(
    course_id: str,
    body: ManifestUpdateRequest,
) -> CourseManifest:
    repository = get_course_repository()
    try:
        updates = [entry.model_dump(exclude_none=True) for entry in body.entries]
        return await asyncio.to_thread(
            repository.update_manifest, course_id, body.revision, updates
        )
    except Exception as exc:
        raise _safe_error(exc, default="Manifest could not be updated") from exc


@router.post("/{course_id}/manifest/approve", response_model=CourseManifest)
async def approve_course_manifest(
    course_id: str,
    body: ManifestApprovalRequest,
) -> CourseManifest:
    repository = get_course_repository()
    try:
        return await asyncio.to_thread(repository.approve_manifest, course_id, body.revision)
    except Exception as exc:
        raise _safe_error(exc, default="Manifest could not be approved") from exc


@router.get("/{course_id}", response_model=CourseResponse)
async def get_course(course_id: str) -> CourseResponse:
    repository = get_course_repository()
    try:
        course = await asyncio.to_thread(repository.get, course_id)
    except InvalidCourseIdentifierError as exc:
        raise HTTPException(status_code=400, detail="Invalid course id") from exc
    if course is None:
        # Missing and foreign courses intentionally share one response.
        raise HTTPException(status_code=404, detail="Course not found")
    return CourseResponse(course=course)


__all__ = ["router", "get_course_repository"]
