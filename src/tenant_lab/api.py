import os
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from tenant_lab.access import audit, authorized, document
from tenant_lab.auth import identity


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=20_000)


class DocumentUpdate(DocumentInput):
    expected_version: int = Field(ge=1, strict=True)


class NoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=2_000)


def create_app(dsn: str, secret: str) -> FastAPI:
    if len(secret.encode()) < 32:
        raise ValueError("Use a signing key of at least 32 bytes")
    app = FastAPI(title="Tenant Access Lab", version="0.1.0")

    def current_user(authorization: Annotated[str | None, Header()] = None):
        return identity(secret, authorization)

    User = Annotated[str, Depends(current_user)]

    @app.get("/tenants/{tenant_id}/documents")
    def list_documents(
        tenant_id: str,
        user: User,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        after: UUID | None = None,
    ):
        with authorized(dsn, user, tenant_id) as conn:
            rows = conn.execute(
                """SELECT id, title, version FROM documents
                   WHERE tenant_id = %s AND (%s::uuid IS NULL OR id > %s)
                   ORDER BY id LIMIT %s""",
                (tenant_id, after, after, limit + 1),
            ).fetchall()
            return {
                "items": rows[:limit],
                "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }

    @app.post("/tenants/{tenant_id}/documents", status_code=201)
    def create_document(tenant_id: str, payload: DocumentInput, user: User):
        document_id = uuid4()
        with authorized(dsn, user, tenant_id, write=True) as conn:
            conn.execute(
                """INSERT INTO documents(tenant_id, id, title, body, created_by)
                   VALUES (%s, %s, %s, %s, %s)""",
                (tenant_id, document_id, payload.title, payload.body, user),
            )
            audit(conn, tenant_id, document_id, user, "created", 1)
            return document(conn, tenant_id, document_id)

    @app.get("/tenants/{tenant_id}/documents/{document_id}")
    def get_document(tenant_id: str, document_id: UUID, user: User):
        with authorized(dsn, user, tenant_id) as conn:
            return document(conn, tenant_id, document_id)

    @app.patch("/tenants/{tenant_id}/documents/{document_id}")
    def update_document(tenant_id: str, document_id: UUID, payload: DocumentUpdate, user: User):
        with authorized(dsn, user, tenant_id, write=True) as conn:
            document(conn, tenant_id, document_id)
            row = conn.execute(
                """UPDATE documents SET title = %s, body = %s, version = version + 1
                   WHERE tenant_id = %s AND id = %s AND version = %s
                   RETURNING id, title, body, version""",
                (payload.title, payload.body, tenant_id, document_id, payload.expected_version),
            ).fetchone()
            if not row:
                raise HTTPException(409, "Document changed; reload before saving")
            audit(conn, tenant_id, document_id, user, "updated", row["version"])
            return row

    @app.post("/tenants/{tenant_id}/documents/{document_id}/notes", status_code=201)
    def add_note(tenant_id: str, document_id: UUID, payload: NoteInput, user: User):
        with authorized(dsn, user, tenant_id, write=True) as conn:
            parent = document(conn, tenant_id, document_id)
            note_id = uuid4()
            conn.execute(
                """INSERT INTO notes(tenant_id, id, document_id, body, created_by)
                   VALUES (%s, %s, %s, %s, %s)""",
                (tenant_id, note_id, document_id, payload.body, user),
            )
            audit(conn, tenant_id, document_id, user, "note_added", parent["version"])
            return {"id": note_id, "document_id": document_id}

    return app


def from_env() -> FastAPI:
    return create_app(os.environ["DATABASE_URL"], os.environ["JWT_SECRET"])
