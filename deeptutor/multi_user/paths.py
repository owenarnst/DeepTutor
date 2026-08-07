"""Path resolution for admin-local and per-user workspaces.

Everything lives under ``<runtime-home>/data`` so a deployment has exactly
one tree to mount and back up:

* ``data/user``           — the admin workspace (admin scope root is ``data/``)
* ``data/users/<uid>``    — one workspace per non-admin user
* ``data/partners/<id>``  — partner (synthetic-user) workspaces
* ``data/system``         — deployment state: accounts, grants, audit, and
  tenant-isolated Course databases/workspaces. Never mounted into the sandbox
  runner — see ``docker-compose.yml``.

Deployments upgraded from the sibling ``multi-user/`` layout are migrated
in place by :func:`migrate_legacy_multi_user_tree`.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import logging
from pathlib import Path
import shutil
import threading
from typing import Iterator

from deeptutor.runtime.home import get_runtime_home
from deeptutor.services.path_service import PathService

from .models import LOCAL_ADMIN_ID, LOCAL_ADMIN_USERNAME, CurrentUser, UserScope

logger = logging.getLogger(__name__)

PROJECT_ROOT = get_runtime_home()
ADMIN_WORKSPACE_ROOT = PROJECT_ROOT / "data"
USERS_ROOT = ADMIN_WORKSPACE_ROOT / "users"
SYSTEM_ROOT = ADMIN_WORKSPACE_ROOT / "system"
LEGACY_MULTI_USER_ROOT = PROJECT_ROOT / "multi-user"

_path_services: dict[str, PathService] = {}

_legacy_migration_lock = threading.Lock()
_legacy_migration_done = False


def migrate_legacy_multi_user_tree() -> None:
    """One-time move of the pre-v1.5 sibling ``multi-user/`` tree into ``data/``.

    ``multi-user/_system`` becomes ``data/system``; every other child is a
    user id directory and becomes ``data/users/<uid>``. Existing targets are
    never overwritten — leftovers stay in place and are logged so an operator
    can reconcile by hand. Idempotent and cheap once migrated (one existence
    check), so callers on the auth/grants/workspace read paths can invoke it
    unconditionally.
    """
    global _legacy_migration_done
    if _legacy_migration_done:
        return
    with _legacy_migration_lock:
        if _legacy_migration_done:
            return
        _legacy_migration_done = True
        legacy = LEGACY_MULTI_USER_ROOT
        if not legacy.is_dir():
            return
        leftovers: list[str] = []
        for child in sorted(legacy.iterdir()):
            target = SYSTEM_ROOT if child.name == "_system" else USERS_ROOT / child.name
            if target.exists():
                leftovers.append(child.name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(child), str(target))
            logger.info("Migrated legacy multi-user path %s -> %s", child, target)
        if leftovers:
            logger.warning(
                "Legacy multi-user tree partially migrated; reconcile by hand: %s",
                ", ".join(str(legacy / name) for name in leftovers),
            )
            return
        try:
            legacy.rmdir()
        except OSError:
            logger.warning("Could not remove legacy multi-user root %s", legacy)


def admin_scope() -> UserScope:
    return UserScope(kind="admin", user_id=LOCAL_ADMIN_ID, root=ADMIN_WORKSPACE_ROOT.resolve())


def local_admin_user() -> CurrentUser:
    return CurrentUser(
        id=LOCAL_ADMIN_ID,
        username=LOCAL_ADMIN_USERNAME,
        role="admin",
        scope=admin_scope(),
    )


def scope_for_user(user_id: str, *, is_admin: bool) -> UserScope:
    if is_admin:
        return admin_scope()
    migrate_legacy_multi_user_tree()
    return UserScope(kind="user", user_id=user_id, root=(USERS_ROOT / user_id).resolve())


def ensure_user_workspace(user_id: str) -> Path:
    return ensure_scope_workspace(scope_for_user(user_id, is_admin=False))


def ensure_scope_workspace(scope: UserScope) -> Path:
    """Create the workspace tree for *scope* at its own root.

    Resolving from ``scope.root`` (instead of recomputing ``USERS_ROOT /
    user_id``) keeps this correct for synthetic scopes whose root lives
    elsewhere — e.g. partner workspaces under ``data/partners/<id>/workspace``.
    For regular users both paths are identical.
    """
    root = scope.root.resolve()
    PathService(workspace_root=root).ensure_all_directories()
    (root / "knowledge_bases").mkdir(parents=True, exist_ok=True)
    (root / "memory").mkdir(parents=True, exist_ok=True)
    return root


def ensure_system_dirs() -> None:
    migrate_legacy_multi_user_tree()
    for child in ("auth", "grants", "audit", "indexes"):
        (SYSTEM_ROOT / child).mkdir(parents=True, exist_ok=True)


def course_storage_root_for_scope(scope: UserScope) -> Path:
    """Return a stable, non-client-controlled private Course root for *scope*.

    The digest is derived only from the authenticated server-side scope kind
    and user id. It remains stable when a deployment moves its data directory,
    while avoiding any path interpretation of an identity string.
    """
    identity = f"{scope.kind}:{scope.user_id}".encode("utf-8")
    storage_key = hashlib.sha256(identity).hexdigest()
    return SYSTEM_ROOT / "course-mode" / "scopes" / storage_key


def get_path_service_for_scope(scope: UserScope) -> PathService:
    key = scope.cache_key
    service = _path_services.get(key)
    if service is None:
        service = PathService(
            workspace_root=scope.root,
            course_storage_root=course_storage_root_for_scope(scope),
            course_storage_anchor=SYSTEM_ROOT.parent,
        )
        _path_services[key] = service
    return service


def get_admin_path_service() -> PathService:
    return get_path_service_for_scope(admin_scope())


def _get_current_scoped_path_service(*, create_user_workspace: bool) -> PathService:
    from .context import get_current_user_or_none

    user = get_current_user_or_none()
    if user is None:
        return PathService.get_instance()
    if create_user_workspace and user.scope.kind == "user":
        ensure_scope_workspace(user.scope)
    return get_path_service_for_scope(user.scope)


def get_current_path_service() -> PathService:
    return _get_current_scoped_path_service(create_user_workspace=True)


def get_current_course_path_service() -> PathService:
    """Resolve Course storage without creating the runner-visible workspace.

    Course routes use this dependency instead of :func:`get_current_path_service`.
    Authentication already installed the current user, and Course data lives
    solely under the server-private ``data/system`` tree.
    """
    return _get_current_scoped_path_service(create_user_workspace=False)


@contextmanager
def user_context(user: CurrentUser) -> Iterator[None]:
    from .context import reset_current_user, set_current_user

    token = set_current_user(user)
    try:
        yield
    finally:
        reset_current_user(token)
