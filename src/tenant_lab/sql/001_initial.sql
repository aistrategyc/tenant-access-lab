CREATE TABLE tenants (
    id text PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE users (
    id text PRIMARY KEY
);
CREATE TABLE memberships (
    tenant_id text NOT NULL REFERENCES tenants(id),
    user_id text NOT NULL REFERENCES users(id),
    role text NOT NULL CHECK (role IN ('viewer', 'editor', 'admin')),
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id, user_id)
);
CREATE TABLE documents (
    tenant_id text NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    body text NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by text NOT NULL,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, created_by) REFERENCES memberships(tenant_id, user_id)
);
CREATE TABLE notes (
    tenant_id text NOT NULL,
    id uuid NOT NULL,
    document_id uuid NOT NULL,
    body text NOT NULL,
    created_by text NOT NULL,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents(tenant_id, id),
    FOREIGN KEY (tenant_id, created_by) REFERENCES memberships(tenant_id, user_id)
);
CREATE TABLE audit_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id text NOT NULL,
    document_id uuid NOT NULL,
    actor_id text NOT NULL,
    action text NOT NULL CHECK (action IN ('created', 'updated', 'note_added')),
    document_version integer NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents(tenant_id, id),
    FOREIGN KEY (tenant_id, actor_id) REFERENCES memberships(tenant_id, user_id)
);
CREATE INDEX audit_document ON audit_events(tenant_id, document_id, occurred_at);
