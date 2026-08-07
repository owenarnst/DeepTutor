from __future__ import annotations

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
        "alice-token": TokenPayload(username="alice", role="user", user_id="u_alice"),
        "bob-token": TokenPayload(username="bob", role="user", user_id="u_bob"),
    }
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_router, "decode_token", tokens.get)
    monkeypatch.setattr(multi_user_paths, "USERS_ROOT", tmp_path / "data" / "users")
    monkeypatch.setattr(multi_user_paths, "_path_services", {})

    app = FastAPI()
    app.include_router(
        courses_router.router,
        prefix="/api/v1/courses",
        dependencies=[Depends(auth_router.require_auth)],
    )
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
    assert listed.json() == {"courses": [course]}

    reopened = course_client.get(f"/api/v1/courses/{course['id']}", headers=_auth())
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

    assert course_client.get("/api/v1/courses", headers=_auth("bob")).json() == {"courses": []}
