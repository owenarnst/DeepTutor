from __future__ import annotations

import asyncio
from pathlib import Path
import time

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def course_client(tmp_path, monkeypatch):
    from deeptutor.api.routers import auth as auth_router
    from deeptutor.api.routers import courses as courses_router
    from deeptutor.multi_user import paths as multi_user_paths
    from deeptutor.services.auth import TokenPayload

    tokens = {
        "admin-token": TokenPayload(username="local", role="admin", user_id="local-admin"),
        "alice-token": TokenPayload(username="alice", role="user", user_id="u_alice"),
        "bob-token": TokenPayload(username="bob", role="user", user_id="u_bob"),
    }
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_router, "decode_token", tokens.get)
    data_root = tmp_path / "data"
    monkeypatch.setattr(multi_user_paths, "ADMIN_WORKSPACE_ROOT", data_root)
    monkeypatch.setattr(multi_user_paths, "USERS_ROOT", data_root / "users")
    monkeypatch.setattr(multi_user_paths, "SYSTEM_ROOT", data_root / "system")
    monkeypatch.setattr(multi_user_paths, "_path_services", {})

    app = FastAPI()
    app.include_router(
        courses_router.router,
        prefix="/api/v1/courses",
        dependencies=[Depends(auth_router.require_auth)],
    )
    app.state.test_data_root = data_root
    return TestClient(app)


def _auth(user: str = "alice") -> dict[str, str]:
    return {"Authorization": f"Bearer {user}-token"}


def _create_course(
    client: TestClient,
    *,
    title: str,
    key: str,
    actor: str = "alice",
    content: bytes = b"Cell biology lecture notes\nMitochondria produce ATP.",
    filename: str = "lecture-notes.txt",
    **overrides: object,
):
    """Submit the ratified multipart Course import contract."""
    data: dict[str, str] = {
        "title": title,
        "description": "Learn the cell",
        "unit_title": "The cell",
        "desired_outcome": "Explain the core concepts and solve representative problems.",
        "weekly_minutes": "120",
        "ocw_url": "https://ocw.mit.edu/courses/7-01sc-fundamentals-of-biology-fall-2011/",
    }
    data.update({name: str(value) for name, value in overrides.items()})
    return client.post(
        "/api/v1/courses",
        headers={**_auth(actor), "Idempotency-Key": key},
        data=data,
        files={"files": (filename, content, "text/plain")},
    )


def _plant_runner_visible_course_storage(
    data_root: Path,
    *,
    workspace_root: Path,
    legacy_user_root: Path,
    owner_scope: str,
    seed_name: str,
) -> dict:
    """Plant a valid victim-owned Course aggregate where the runner can write."""
    from deeptutor.course_mode.repository import CourseInput, CourseRepository
    from deeptutor.services.path_service import PathService

    seed_root = data_root / f"seed-{seed_name}"
    seed_paths = PathService(
        workspace_root=workspace_root,
        course_storage_root=seed_root,
        course_storage_anchor=data_root,
    )
    planted = (
        CourseRepository(seed_paths, owner_scope=owner_scope)
        .create_draft(
            f"planted-{seed_name}",
            CourseInput(title=f"Attacker planted {seed_name}"),
        )
        .course
    )

    legacy_user_root.mkdir(parents=True, exist_ok=True)
    seed_paths.get_course_mode_db().rename(legacy_user_root / "course_mode.db")
    legacy_workspace = legacy_user_root / "workspace" / "course-mode"
    legacy_workspace.parent.mkdir(parents=True, exist_ok=True)
    seed_paths.get_course_mode_workspace_root().rename(legacy_workspace)
    seed_root.rmdir()
    return planted.model_dump(mode="json")


def _multipart_payload(
    files: list[tuple[str, bytes]],
    *,
    boundary: str = "owe7-streaming-boundary",
) -> tuple[bytes, str]:
    chunks: list[bytes] = []
    fields = {
        "title": "Streaming limits",
        "description": "A bounded request",
        "unit_title": "Unit 1",
        "desired_outcome": "Read the source",
        "weekly_minutes": "30",
        "ocw_url": "https://ocw.mit.edu/courses/18-06/",
    }
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    for filename, content in files:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="files"; '
                    f'filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n'
                ).encode(),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _streaming_request(body: bytes, content_type: str, *, chunk_size: int = 5):
    from starlette.requests import Request

    chunks = [body[index : index + chunk_size] for index in range(0, len(body), chunk_size)]
    cursor = 0

    async def receive() -> dict[str, object]:
        nonlocal cursor
        if cursor >= len(chunks):
            return {"type": "http.request", "body": b"", "more_body": False}
        chunk = chunks[cursor]
        cursor += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": cursor < len(chunks),
        }

    class StreamingRequest(Request):
        async def form(self):  # type: ignore[no-untyped-def]
            raise AssertionError("the route must enforce limits during streaming parse")

    return StreamingRequest(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/courses",
            "headers": [(b"content-type", content_type.encode())],
        },
        receive,
    )


def test_create_list_and_reopen_draft(course_client: TestClient) -> None:
    response = _create_course(
        course_client,
        title="Cell Biology",
        key="browser-attempt-1",
    )

    assert response.status_code == 201
    course = response.json()["course"]
    assert course["status"] == "draft"
    assert len(course["units"]) == 1
    assert course["units"][0]["course_id"] == course["id"]

    replay = _create_course(
        course_client,
        title=" Cell   Biology ",
        key="browser-attempt-1",
    )
    assert replay.status_code == 200
    assert replay.json()["course"] == course
    assert replay.json()["created"] is False
    assert replay.json()["processing_job"] == response.json()["processing_job"]

    listed = course_client.get("/api/v1/courses", headers=_auth())
    assert listed.status_code == 200
    assert listed.json() == {
        "courses": [course],
        "total": 1,
        "has_more": False,
        "next_offset": None,
    }

    reopened = course_client.get(f"/api/v1/courses/{course['id']}", headers=_auth())
    assert reopened.status_code == 200
    assert reopened.json() == {"course": course}


def test_actual_course_route_uses_server_private_storage_without_user_workspace_prelude(
    course_client: TestClient,
) -> None:
    from deeptutor.multi_user import paths as multi_user_paths

    data_root = course_client.app.state.test_data_root
    user_root = data_root / "users" / "u_alice"
    assert not user_root.exists()

    created: dict[str, dict] = {}
    for actor in ("alice", "bob", "admin"):
        response = _create_course(
            course_client,
            actor=actor,
            key=f"private-storage-{actor}",
            title=f"{actor.title()} private course",
        )
        assert response.status_code == 201
        created[actor] = response.json()["course"]

    assert not user_root.exists()
    private_root = data_root / "system" / "course-mode" / "scopes"
    databases = list(private_root.rglob("course_mode.db"))
    assert len(databases) == 3
    assert len({database.parent for database in databases}) == 3
    for course in created.values():
        workspaces = list(private_root.rglob(course["id"]))
        assert len(workspaces) == 1
        assert workspaces[0].is_relative_to(data_root / "system")
        assert not workspaces[0].is_relative_to(data_root / "users")
    for database in databases:
        assert database.is_relative_to(data_root / "system")
        assert not database.is_relative_to(data_root / "users")

    # Recreate the real per-scope services, as a backend process restart would,
    # then prove each authenticated route reopens only its own persisted Course.
    multi_user_paths._path_services.clear()
    for actor, course in created.items():
        reopened = course_client.get(f"/api/v1/courses/{course['id']}", headers=_auth(actor))
        assert reopened.status_code == 200
        assert reopened.json() == {"course": course}


def test_actual_course_route_ignores_runner_visible_course_storage(
    course_client: TestClient,
) -> None:
    """Runner-writable legacy names never become authoritative Course storage."""
    from deeptutor.multi_user import paths as multi_user_paths

    data_root = course_client.app.state.test_data_root
    actors = {
        "admin": {
            "owner_scope": "local-admin",
            "workspace_root": data_root,
            "legacy_user_root": data_root / "user",
        },
        "alice": {
            "owner_scope": "u_alice",
            "workspace_root": data_root / "users" / "u_alice",
            "legacy_user_root": data_root / "users" / "u_alice" / "user",
        },
    }
    planted: dict[str, dict] = {}
    for actor, setup in actors.items():
        planted[actor] = _plant_runner_visible_course_storage(
            data_root,
            workspace_root=setup["workspace_root"],
            legacy_user_root=setup["legacy_user_root"],
            owner_scope=setup["owner_scope"],
            seed_name=actor,
        )

    created: dict[str, dict] = {}
    for actor, setup in actors.items():
        legacy_user_root = setup["legacy_user_root"]
        legacy_database = legacy_user_root / "course_mode.db"
        legacy_workspace = legacy_user_root / "workspace" / "course-mode"

        empty = course_client.get("/api/v1/courses", headers=_auth(actor))
        assert empty.status_code == 200
        assert empty.json()["courses"] == []
        assert (
            course_client.get(
                f"/api/v1/courses/{planted[actor]['id']}",
                headers=_auth(actor),
            ).status_code
            == 404
        )

        response = _create_course(
            course_client,
            actor=actor,
            key=f"private-{actor}",
            title=f"Private {actor} course",
        )
        assert response.status_code == 201
        created[actor] = response.json()["course"]
        assert legacy_database.is_file()
        assert (legacy_workspace / "courses" / planted[actor]["id"]).is_dir()

    # A recreated service encounters both the private DB and conflicting legacy
    # names. The untrusted entries must remain inert and cannot deny reopen.
    multi_user_paths._path_services.clear()
    for actor, setup in actors.items():
        reopened = course_client.get(
            f"/api/v1/courses/{created[actor]['id']}",
            headers=_auth(actor),
        )
        assert reopened.status_code == 200
        assert reopened.json() == {"course": created[actor]}
        listed = course_client.get("/api/v1/courses", headers=_auth(actor))
        assert [course["id"] for course in listed.json()["courses"]] == [created[actor]["id"]]
        assert (setup["legacy_user_root"] / "course_mode.db").is_file()

    private_databases = list(
        (data_root / "system" / "course-mode" / "scopes").rglob("course_mode.db")
    )
    assert len(private_databases) == 2


def test_create_requires_auth_and_a_stable_request_key(course_client: TestClient) -> None:
    assert course_client.get("/api/v1/courses").status_code == 401
    missing_key = course_client.post("/api/v1/courses", headers=_auth(), json={"title": "Physics"})
    assert missing_key.status_code == 422

    invalid_key = course_client.post(
        "/api/v1/courses",
        headers={**_auth(), "Idempotency-Key": "../escape"},
        json={"title": "Physics"},
    )
    assert invalid_key.status_code == 422


def test_create_rejects_client_ownership_and_key_reuse_conflict(
    course_client: TestClient,
) -> None:
    client_owned = course_client.post(
        "/api/v1/courses",
        headers={**_auth(), "Idempotency-Key": "ownership-test"},
        data={
            "title": "Physics",
            "unit_title": "Unit",
            "desired_outcome": "Outcome",
            "weekly_minutes": "60",
            "ocw_url": "https://ocw.mit.edu/courses/7-01sc-fundamentals-of-biology-fall-2011/",
            "user_id": "u_bob",
        },
        files={"files": ("notes.txt", b"notes", "text/plain")},
    )
    assert client_owned.status_code == 422

    assert (
        _create_course(
            course_client,
            key="ownership-test",
            title="Physics",
        ).status_code
        == 201
    )
    conflict = _create_course(
        course_client,
        key="ownership-test",
        title="Chemistry",
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == (
        "Idempotency key was already used with different course input"
    )


def test_missing_foreign_and_traversal_ids_do_not_leak(course_client: TestClient) -> None:
    created = _create_course(
        course_client,
        actor="alice",
        key="alice-course",
        title="Alice only",
    ).json()["course"]

    assert (
        course_client.get(f"/api/v1/courses/{created['id']}", headers=_auth("bob")).status_code
        == 404
    )
    assert (
        course_client.get(
            "/api/v1/courses/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            headers=_auth("alice"),
        ).status_code
        == 404
    )
    traversal = course_client.get("/api/v1/courses/%2E%2E%5Cforeign", headers=_auth("alice"))
    assert traversal.status_code in {400, 404}

    assert course_client.get("/api/v1/courses", headers=_auth("bob")).json() == {
        "courses": [],
        "total": 0,
        "has_more": False,
        "next_offset": None,
    }


def test_course_list_is_paginated_and_bounded(course_client: TestClient) -> None:
    for index in range(3):
        response = _create_course(
            course_client,
            key=f"page-{index}",
            title=f"Course {index}",
        )
        assert response.status_code == 201

    page = course_client.get(
        "/api/v1/courses?limit=1&offset=1",
        headers=_auth(),
    )
    assert page.status_code == 200
    assert len(page.json()["courses"]) == 1
    assert page.json()["total"] == 3
    assert page.json()["has_more"] is True
    assert page.json()["next_offset"] == 2
    assert course_client.get("/api/v1/courses?limit=101", headers=_auth()).status_code == 422
    assert course_client.get("/api/v1/courses?offset=10001", headers=_auth()).status_code == 422


def test_quota_rejects_new_course_but_allows_replay_at_limit(
    course_client: TestClient,
    monkeypatch,
) -> None:
    from deeptutor.api.routers import courses as courses_router

    monkeypatch.setattr(
        courses_router,
        "load_system_settings",
        lambda: {"course_max_per_owner": 2},
    )
    first_headers = {**_auth(), "Idempotency-Key": "quota-first"}
    first = _create_course(course_client, key="quota-first", title="First")
    second = _create_course(
        course_client,
        key="quota-second",
        title="Second",
    )
    assert first.status_code == 201
    assert second.status_code == 201

    exhausted = _create_course(
        course_client,
        key="quota-third",
        title="Third",
    )
    assert exhausted.status_code == 409
    assert exhausted.json()["detail"] == "Course limit reached (2 per owner)"

    replay = _create_course(course_client, key="quota-first", title="First")
    assert replay.status_code == 200
    assert replay.json()["created"] is False
    assert replay.json()["course"]["id"] == first.json()["course"]["id"]


@pytest.mark.asyncio
async def test_list_moves_blocking_repository_work_off_event_loop(monkeypatch) -> None:
    from deeptutor.api.routers import courses as courses_router
    from deeptutor.course_mode.repository import CoursePage

    order: list[str] = []

    class SlowRepository:
        def list_page(self, *, limit: int, offset: int):
            assert (limit, offset) == (50, 0)
            time.sleep(0.05)
            order.append("repository-finished")
            return CoursePage(courses=(), total=0, limit=limit, offset=offset)

    monkeypatch.setattr(courses_router, "get_course_repository", SlowRepository)

    async def heartbeat() -> None:
        await asyncio.sleep(0.005)
        order.append("heartbeat")

    list_task = asyncio.create_task(courses_router.list_courses(limit=50, offset=0))
    heartbeat_task = asyncio.create_task(heartbeat())
    await heartbeat_task
    assert order == ["heartbeat"]

    response = await list_task

    assert response.courses == []
    assert order == ["heartbeat", "repository-finished"]


def test_import_exposes_durable_processing_and_manifest_review(course_client: TestClient) -> None:
    response = _create_course(
        course_client,
        key="import-contract",
        title="Import contract",
        filename="week-1-solution.md",
        content=b"Answer key content",
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["course"]["ocw_url"].startswith("https://ocw.mit.edu/courses/")
    job = payload["processing_job"]
    assert job["status"] == "awaiting_manifest_review"
    assert job["stage"] == "awaiting_manifest_review"

    fetched_job = course_client.get(f"/api/v1/courses/jobs/{job['id']}", headers=_auth())
    assert fetched_job.status_code == 200
    assert fetched_job.json()["job"]["id"] == job["id"]

    manifest_response = course_client.get(
        f"/api/v1/courses/{payload['course']['id']}/manifest", headers=_auth()
    )
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert len(manifest["entries"]) == 1
    assert manifest["entries"][0]["role"] == "solution"
    assert manifest["entries"][0]["visibility"] == "instructor_only"
    assert manifest["blockers"] == ["suspected_solution_confirmation"]

    blocked = course_client.post(
        f"/api/v1/courses/{payload['course']['id']}/manifest/approve",
        headers=_auth(),
        json={"revision": manifest["revision"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "manifest_review_incomplete"

    entry = manifest["entries"][0]
    corrected = course_client.patch(
        f"/api/v1/courses/{payload['course']['id']}/manifest",
        headers=_auth(),
        json={
            "revision": manifest["revision"],
            "entries": [
                {
                    "id": entry["id"],
                    "role": "solution",
                    "visibility": "instructor_only",
                    "role_confirmed": True,
                    "visibility_confirmed": True,
                }
            ],
        },
    )
    assert corrected.status_code == 200
    assert corrected.json()["blockers"] == []

    approved = course_client.post(
        f"/api/v1/courses/{payload['course']['id']}/manifest/approve",
        headers=_auth(),
        json={"revision": corrected.json()["revision"]},
    )
    assert approved.status_code == 200
    assert approved.json()["eligible_for_planning"] is True
    assert approved.json()["course_id"] == payload["course"]["id"]


def test_manifest_endpoint_rejects_immutable_entry_fields(
    course_client: TestClient,
) -> None:
    created = _create_course(course_client, key="manifest-schema", title="Manifest schema")
    assert created.status_code == 201
    payload = created.json()
    manifest = course_client.get(
        f"/api/v1/courses/{payload['course']['id']}/manifest", headers=_auth()
    ).json()
    entry = manifest["entries"][0]

    response = course_client.patch(
        f"/api/v1/courses/{payload['course']['id']}/manifest",
        headers=_auth(),
        json={
            "revision": manifest["revision"],
            "entries": [
                {
                    "id": entry["id"],
                    "course_id": entry["course_id"],
                    "role": entry["role"],
                    "visibility": entry["visibility"],
                    "role_confirmed": entry["role_confirmed"],
                    "visibility_confirmed": entry["visibility_confirmed"],
                }
            ],
        },
    )

    assert response.status_code == 422


def test_public_retry_recovers_a_queued_job_after_create_process_loss(
    course_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.course_mode.repository import CourseRepository

    original_process_import = CourseRepository.process_import

    def process_lost_after_create(self: CourseRepository, job_id: str):
        job = self.get_processing_job_by_id(job_id)
        assert job is not None
        return job

    monkeypatch.setattr(CourseRepository, "process_import", process_lost_after_create)
    created = _create_course(
        course_client,
        key="queued-process-loss",
        title="Queued recovery",
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["processing_job"]["status"] == "queued"

    monkeypatch.setattr(CourseRepository, "process_import", original_process_import)
    retry = course_client.post(
        f"/api/v1/courses/jobs/{payload['processing_job']['id']}/retry",
        headers=_auth(),
    )

    assert retry.status_code == 200
    assert retry.json()["job"]["status"] == "awaiting_manifest_review"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("files", "file_limit", "total_limit", "count_limit", "message"),
    [
        (
            [
                ("large.md", b"x" * 17),
            ],
            16,
            64,
            4,
            "per-file",
        ),
        (
            [
                ("first.md", b"x" * 13),
                ("second.md", b"y" * 13),
            ],
            16,
            24,
            4,
            "batch size",
        ),
        (
            [
                ("one.md", b"one"),
                ("two.md", b"two"),
                ("three.md", b"three"),
            ],
            16,
            64,
            2,
            "file count",
        ),
    ],
)
async def test_chunked_multipart_limits_are_enforced_before_form_spooling(
    monkeypatch: pytest.MonkeyPatch,
    files: list[tuple[str, bytes]],
    file_limit: int,
    total_limit: int,
    count_limit: int,
    message: str,
) -> None:
    from deeptutor.api.routers import courses as courses_router

    monkeypatch.setattr(courses_router, "COURSE_MAX_FILE_BYTES", file_limit)
    monkeypatch.setattr(courses_router, "COURSE_MAX_TOTAL_BYTES", total_limit)
    monkeypatch.setattr(courses_router, "COURSE_MAX_UPLOAD_COUNT", count_limit)
    body, content_type = _multipart_payload(files)
    request = _streaming_request(body, content_type, chunk_size=3)

    with pytest.raises(courses_router.InvalidCourseSourceError, match=message):
        await courses_router._parse_multipart_course(request)


@pytest.mark.asyncio
async def test_multipart_handoff_keeps_accepted_uploads_as_rewindable_streams() -> None:
    from deeptutor.api.routers import courses as courses_router
    from deeptutor.course_mode.source_processing import CourseUpload

    body, content_type = _multipart_payload(
        [
            ("first.md", b"first source text"),
            ("second.md", b"second source text"),
        ]
    )
    request = _streaming_request(body, content_type, chunk_size=7)

    _course_input, uploads = await courses_router._parse_multipart_course(request)
    try:
        assert all(isinstance(upload, CourseUpload) for upload in uploads)
        assert all(upload.stream.seekable() for upload in uploads)
        assert all(not isinstance(upload.stream, bytes) for upload in uploads)
        assert [upload.stream.read() for upload in uploads] == [
            b"first source text",
            b"second source text",
        ]
    finally:
        for upload in uploads:
            upload.close()


def test_chunked_multipart_endpoint_rejects_oversized_source_before_course_creation(
    course_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.api.routers import courses as courses_router

    monkeypatch.setattr(courses_router, "COURSE_MAX_FILE_BYTES", 16)
    body, content_type = _multipart_payload([("large.md", b"x" * 17)])
    chunks = (body[index : index + 3] for index in range(0, len(body), 3))
    response = course_client.post(
        "/api/v1/courses",
        headers={
            **_auth(),
            "Idempotency-Key": "chunked-oversize",
            "Content-Type": content_type,
        },
        content=chunks,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Uploaded source exceeds the per-file size limit"


def test_import_rejects_source_less_json_and_unsafe_or_unsupported_uploads(
    course_client: TestClient,
) -> None:
    source_less = course_client.post(
        "/api/v1/courses",
        headers={**_auth(), "Idempotency-Key": "source-less"},
        json={"title": "No source"},
    )
    assert source_less.status_code == 422
    assert source_less.json()["detail"] == (
        "Course creation requires at least one uploaded source file"
    )

    common = {
        "title": "Unsafe source",
        "unit_title": "Unit 1",
        "desired_outcome": "Read the source",
        "weekly_minutes": "30",
        "ocw_url": "https://ocw.mit.edu/courses/18-06/",
    }
    traversal = course_client.post(
        "/api/v1/courses",
        headers={**_auth(), "Idempotency-Key": "traversal-source"},
        data=common,
        files={"files": ("../notes.md", b"notes", "text/markdown")},
    )
    assert traversal.status_code == 422
    assert "relative basename" in traversal.json()["detail"]

    image = course_client.post(
        "/api/v1/courses",
        headers={**_auth(), "Idempotency-Key": "image-source"},
        data=common,
        files={"files": ("diagram.svg", b"<svg></svg>", "image/svg+xml")},
    )
    assert image.status_code == 422
    assert "Unsupported Course source format" in image.json()["detail"]
