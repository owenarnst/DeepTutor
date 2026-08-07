"""Authenticated Course Mode create/list/detail routes."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from deeptutor.course_mode.models import Course
from deeptutor.course_mode.repository import (
    DEFAULT_COURSE_LIST_LIMIT,
    MAX_COURSE_LIST_LIMIT,
    MAX_COURSE_LIST_OFFSET,
    CourseInput,
    CourseQuotaExceededError,
    CourseRepository,
    IdempotencyConflictError,
    InvalidCourseIdentifierError,
    InvalidCourseInputError,
    InvalidRequestKeyError,
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
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    unit_title: str = Field(default="Unit 1", min_length=1, max_length=200)


class CourseResponse(BaseModel):
    course: Course


class CreateCourseResponse(CourseResponse):
    created: bool


class CourseListResponse(BaseModel):
    courses: list[Course]
    total: int
    has_more: bool
    next_offset: int | None


def get_course_repository() -> CourseRepository:
    user = get_current_user()
    max_courses = load_system_settings()["course_max_per_owner"]
    return CourseRepository(
        get_current_course_path_service(),
        owner_scope=user.id,
        max_courses=max_courses,
    )


@router.post("", response_model=CreateCourseResponse)
async def create_course(
    body: CreateCourseRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
) -> CreateCourseResponse:
    repository = get_course_repository()
    try:
        result = await asyncio.to_thread(
            repository.create_draft,
            idempotency_key,
            CourseInput(
                title=body.title,
                description=body.description,
                unit_title=body.unit_title,
            ),
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (InvalidCourseInputError, InvalidRequestKeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except CourseQuotaExceededError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return CreateCourseResponse(course=result.course, created=result.created)


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


@router.get("/{course_id}", response_model=CourseResponse)
async def get_course(course_id: str) -> CourseResponse:
    repository = get_course_repository()
    try:
        course = await asyncio.to_thread(repository.get, course_id)
    except InvalidCourseIdentifierError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid course id"
        ) from exc
    if course is None:
        # Missing and foreign courses intentionally share one response.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")
    return CourseResponse(course=course)
