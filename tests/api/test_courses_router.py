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

        response = course_client.post(
            "/api/v1/courses",
            headers={**_auth(actor), "Idempotency-Key": f"private-{actor}"},
            json={"title": f"Private {actor} course"},
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
