import argparse
import os

import psycopg
from fastapi.testclient import TestClient

from tenant_lab.api import create_app
from tenant_lab.auth import issue_token
from tenant_lab.db import migrate

BLUE_DOC = "00000000-0000-0000-0000-000000000001"
RED_DOC = "00000000-0000-0000-0000-000000000002"


def seed(dsn):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO tenants VALUES ('blue', 'Blue Studio'), ('red', 'Red Studio') "
            "ON CONFLICT DO NOTHING"
        )
        conn.execute("INSERT INTO users VALUES ('alice'), ('bob'), ('eve') ON CONFLICT DO NOTHING")
        conn.execute(
            """INSERT INTO memberships(tenant_id, user_id, role)
               VALUES ('blue', 'alice', 'editor'), ('blue', 'bob', 'viewer'),
                      ('red', 'eve', 'admin') ON CONFLICT DO NOTHING"""
        )
        conn.execute(
            """INSERT INTO documents(tenant_id, id, title, body, created_by)
               VALUES ('blue', %s, 'Blue plan', 'Synthetic internal document', 'alice'),
                      ('red', %s, 'Red plan', 'Another synthetic document', 'eve')
               ON CONFLICT DO NOTHING""",
            (BLUE_DOC, RED_DOC),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Two workspaces, explicit access boundaries")
    parser.add_argument("command", choices=["migrate", "seed", "demo", "token"])
    parser.add_argument("--user", default="alice")
    args = parser.parse_args()
    dsn = os.environ["DATABASE_URL"]
    if args.command == "token":
        print(issue_token(os.environ["JWT_SECRET"], args.user))
        return
    migrate(dsn)
    if args.command == "migrate":
        print("Schema is current.")
        return
    seed(dsn)
    if args.command == "seed":
        print("Blue / Red workspaces seeded.")
        return
    secret = os.environ["JWT_SECRET"]
    with TestClient(create_app(dsn, secret)) as client:
        for user, tenant, doc in [
            ("alice", "blue", BLUE_DOC),
            ("alice", "blue", RED_DOC),
            ("alice", "red", RED_DOC),
        ]:
            r = client.get(
                f"/tenants/{tenant}/documents/{doc}",
                headers={"Authorization": "Bearer " + issue_token(secret, user)},
            )
            print(user, "→", tenant, doc, r.status_code, r.json())
        r = client.post(
            "/tenants/blue/documents",
            json={"title": "Viewer cannot write", "body": ""},
            headers={"Authorization": "Bearer " + issue_token(secret, "bob")},
        )
        print("viewer write →", r.status_code, r.json())


if __name__ == "__main__":
    main()
