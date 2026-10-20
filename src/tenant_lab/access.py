from contextlib import contextmanager

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row


@contextmanager
def authorized(dsn: str, user_id: str, tenant_id: str, *, write: bool = False):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        # Keep membership stable until the business transaction commits.
        member = conn.execute(
            """SELECT role FROM memberships
               WHERE tenant_id = %s AND user_id = %s AND active FOR SHARE""",
            (tenant_id, user_id),
        ).fetchone()
        if not member:
            raise HTTPException(404, "Workspace or document not found")
        if write and member["role"] not in {"editor", "admin"}:
            raise HTTPException(403, "This role cannot change documents")
        yield conn


def document(conn, tenant_id, document_id):
    row = conn.execute(
        """SELECT id, title, body, version FROM documents
           WHERE tenant_id = %s AND id = %s""",
        (tenant_id, document_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Workspace or document not found")
    return row


def audit(conn, tenant_id, document_id, user_id, action, version):
    conn.execute(
        """INSERT INTO audit_events
           (tenant_id, document_id, actor_id, action, document_version)
           VALUES (%s, %s, %s, %s, %s)""",
        (tenant_id, document_id, user_id, action, version),
    )
