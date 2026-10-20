import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

from tenant_lab.access import authorized
from tenant_lab.api import create_app
from tenant_lab.auth import AUDIENCE, ISSUER, issue_token
from tenant_lab.cli import BLUE_DOC, RED_DOC, seed

SECRET = "synthetic-test-key-at-least-32-bytes"


@pytest.fixture
def client(dsn):
    seed(dsn)
    with TestClient(create_app(dsn, SECRET)) as client:
        yield client


def headers(user="alice"):
    return {"Authorization": "Bearer " + issue_token(SECRET, user)}


def update_payload():
    return {"title": "Updated", "body": "Synthetic", "expected_version": 1}


def test_list_returns_only_current_tenant(client):
    response = client.get("/tenants/blue/documents", headers=headers())
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["items"]] == [BLUE_DOC]


@pytest.mark.parametrize("tenant", ["blue", "red", "unknown"])
@pytest.mark.parametrize("method", ["get", "patch"])
def test_foreign_document_is_not_accessible_by_id_or_tenant(client, tenant, method):
    kwargs = {"headers": headers()}
    if method == "patch":
        kwargs["json"] = update_payload()
    response = getattr(client, method)(f"/tenants/{tenant}/documents/{RED_DOC}", **kwargs)
    assert response.status_code == 404
    assert response.json() == {"detail": "Workspace or document not found"}


def test_foreign_document_cannot_receive_note(client, dsn):
    response = client.post(
        f"/tenants/blue/documents/{RED_DOC}/notes",
        headers=headers(),
        json={"body": "No cross-tenant link"},
    )
    assert response.status_code == 404
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0


def test_database_composite_fk_rejects_foreign_parent(client, dsn):
    with pytest.raises(psycopg.errors.ForeignKeyViolation), psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO notes VALUES ('blue', %s, %s, 'invalid', 'alice')", (uuid4(), RED_DOC)
        )


@pytest.mark.parametrize("operation", ["create", "update", "note"])
def test_viewer_can_read_but_cannot_write(client, operation):
    h = headers("bob")
    assert client.get(f"/tenants/blue/documents/{BLUE_DOC}", headers=h).status_code == 200
    if operation == "create":
        r = client.post("/tenants/blue/documents", headers=h, json={"title": "x", "body": ""})
    elif operation == "update":
        r = client.patch(f"/tenants/blue/documents/{BLUE_DOC}", headers=h, json=update_payload())
    else:
        r = client.post(f"/tenants/blue/documents/{BLUE_DOC}/notes", headers=h, json={"body": "x"})
    assert r.status_code == 403


def test_revocation_applies_even_when_token_is_still_valid(client, dsn):
    h = headers()
    path = f"/tenants/blue/documents/{BLUE_DOC}"
    assert client.get(path, headers=h).status_code == 200
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE memberships SET active = false WHERE user_id = 'alice'")
    assert client.get(path, headers=h).status_code == 404
    assert client.patch(path, headers=h, json=update_payload()).status_code == 404


def test_role_downgrade_is_applied_without_reissuing_token(client, dsn):
    h = headers()
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE memberships SET role = 'viewer' WHERE user_id = 'alice'")
    r = client.patch(f"/tenants/blue/documents/{BLUE_DOC}", headers=h, json=update_payload())
    assert r.status_code == 403


def test_revoke_cannot_commit_in_middle_of_authorized_transaction(client, dsn):
    with authorized(dsn, "alice", "blue", write=True):
        with pytest.raises(psycopg.errors.LockNotAvailable), psycopg.connect(dsn) as other:
            other.execute("SET LOCAL lock_timeout = '100ms'")
            other.execute("UPDATE memberships SET active = false WHERE user_id = 'alice'")
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE memberships SET active = false WHERE user_id = 'alice'")
    assert client.get("/tenants/blue/documents", headers=headers()).status_code == 404


def test_payload_cannot_assign_tenant_or_actor(client):
    r = client.post(
        "/tenants/blue/documents",
        headers=headers(),
        json={"title": "Test", "body": "", "tenant_id": "red", "created_by": "eve"},
    )
    assert r.status_code == 422


def test_write_and_audit_commit_together(client, dsn):
    r = client.patch(
        f"/tenants/blue/documents/{BLUE_DOC}", headers=headers(), json=update_payload()
    )
    assert r.status_code == 200 and r.json()["version"] == 2
    with psycopg.connect(dsn) as conn:
        assert conn.execute(
            "SELECT tenant_id, actor_id, action, document_version FROM audit_events"
        ).fetchall() == [("blue", "alice", "updated", 2)]


def test_audit_failure_rolls_back_business_change(client, dsn):
    with psycopg.connect(dsn) as conn:
        conn.execute("ALTER TABLE audit_events ADD CONSTRAINT simulated_failure CHECK (false)")
    with pytest.raises(psycopg.errors.CheckViolation):
        client.patch(
            f"/tenants/blue/documents/{BLUE_DOC}", headers=headers(), json=update_payload()
        )
    with psycopg.connect(dsn) as conn:
        assert conn.execute(
            "SELECT title, version FROM documents WHERE tenant_id = 'blue'"
        ).fetchone() == ("Blue plan", 1)


def test_concurrent_edits_do_not_silently_overwrite_each_other(client, dsn):
    barrier = threading.Barrier(2)

    def edit(_):
        with TestClient(create_app(dsn, SECRET)) as local:
            barrier.wait()
            return local.patch(
                f"/tenants/blue/documents/{BLUE_DOC}", headers=headers(), json=update_payload()
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(edit, range(2))) == [200, 409]
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 1


@pytest.mark.parametrize("case", ["missing", "expired", "wrong-audience", "wrong-issuer", "forged"])
def test_invalid_authentication(client, case):
    now = datetime.now(UTC)
    claims = {
        "sub": "alice",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=10),
    }
    if case == "expired":
        claims["exp"] = now - timedelta(minutes=1)
    elif case == "wrong-audience":
        claims["aud"] = "other"
    elif case == "wrong-issuer":
        claims["iss"] = "other"
    key = SECRET if case != "forged" else "another-test-key-at-least-32-bytes"
    token = jwt.encode(claims, key, algorithm="HS256")
    h = {} if case == "missing" else {"Authorization": "Bearer " + token}
    response = client.get("/tenants/blue/documents", headers=h)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_token_role_claim_is_ignored(client):
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "bob",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + timedelta(minutes=10),
            "role": "admin",
            "tenant_id": "red",
        },
        SECRET,
        algorithm="HS256",
    )
    r = client.patch(
        f"/tenants/blue/documents/{BLUE_DOC}",
        headers={"Authorization": "Bearer " + token},
        json=update_payload(),
    )
    assert r.status_code == 403


def test_member_of_two_tenants_still_cannot_mix_document_and_tenant(client, dsn):
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO memberships VALUES ('red', 'alice', 'editor', true)")
    assert client.get(f"/tenants/red/documents/{RED_DOC}", headers=headers()).status_code == 200
    assert client.get(f"/tenants/blue/documents/{RED_DOC}", headers=headers()).status_code == 404


def test_create_and_note_use_authenticated_actor(client, dsn):
    r = client.post(
        "/tenants/blue/documents", headers=headers(), json={"title": "New", "body": "Plan"}
    )
    assert r.status_code == 201
    doc_id = r.json()["id"]
    assert (
        client.post(
            f"/tenants/blue/documents/{doc_id}/notes", headers=headers(), json={"body": "Review"}
        ).status_code
        == 201
    )
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT created_by FROM notes").fetchone()[0] == "alice"


def test_keyset_pagination_has_no_duplicates_or_foreign_rows(client):
    for i in range(3):
        assert (
            client.post(
                "/tenants/blue/documents",
                headers=headers(),
                json={"title": f"Page {i}", "body": ""},
            ).status_code
            == 201
        )
    ids, cursor = [], None
    while True:
        params = {"limit": 2}
        if cursor:
            params["after"] = cursor
        page = client.get("/tenants/blue/documents", headers=headers(), params=params).json()
        ids.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(ids) == len(set(ids)) == 4
    assert RED_DOC not in ids
