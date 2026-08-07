from __future__ import annotations

import asyncio
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


def test_create_list_and_reopen_draft(course_client: TestClient) -> None:
    headers = {**_auth(), "Idempotency-Key": "browser-attempt-1"}
    response = course_client.post(
        "/api/v1/courses",
        headers=headers,
        json={
            "title": "Cell Biology",
            "description": "Learn the cell",
            "unit_title": "The cell",
        },
    )

    assert response.status_code == 201
    course = response.json()["course"]
    assert course["status"] == "draft"
    assert len(course["units"]) == 1
    assert course["units"][0]["course_id"] == course["id"]

    replay = course_client.post(
        "/api/v1/courses",
        headers=headers,
        json={
            "title": " Cell   Biology ",
            "description": "Learn the cell",
            "unit_title": "The cell",
        },
    )
    assert replay.status_code == 200
    assert replay.json() == {"course": course, "created": False}

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
        response = course_client.post(
            "/api/v1/courses",
            headers={**_auth(actor), "Idempotency-Key": f"private-storage-{actor}"},
            json={"title": f"{actor.title()} private course"},
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
    headers = {**_auth(), "Idempotency-Key": "ownership-test"}
    client_owned = course_client.post(
        "/api/v1/courses",
        headers=headers,
        json={"title": "Physics", "user_id": "u_bob"},
    )
    assert client_owned.status_code == 422

    assert (
        course_client.post(
            "/api/v1/courses", headers=headers, json={"title": "Physics"}
        ).status_code
        == 201
    )
    conflict = course_client.post("/api/v1/courses", headers=headers, json={"title": "Chemistry"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == (
        "Idempotency key was already used with different course input"
    )


def test_missing_foreign_and_traversal_ids_do_not_leak(course_client: TestClient) -> None:
    created = course_client.post(
        "/api/v1/courses",
        headers={**_auth("alice"), "Idempotency-Key": "alice-course"},
        json={"title": "Alice only"},
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
        response = course_client.post(
            "/api/v1/courses",
            headers={**_auth(), "Idempotency-Key": f"page-{index}"},
            json={"title": f"Course {index}"},
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
    first = course_client.post("/api/v1/courses", headers=first_headers, json={"title": "First"})
    second = course_client.post(
        "/api/v1/courses",
        headers={**_auth(), "Idempotency-Key": "quota-second"},
        json={"title": "Second"},
    )
    assert first.status_code == 201
    assert second.status_code == 201

    exhausted = course_client.post(
        "/api/v1/courses",
        headers={**_auth(), "Idempotency-Key": "quota-third"},
        json={"title": "Third"},
    )
    assert exhausted.status_code == 409
    assert exhausted.json()["detail"] == "Course limit reached (2 per owner)"

    replay = course_client.post("/api/v1/courses", headers=first_headers, json={"title": "First"})
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
