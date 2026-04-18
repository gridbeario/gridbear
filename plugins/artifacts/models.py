"""ORM model for Artifact records."""

from core.orm import Model, fields


class Artifact(Model):
    """Standalone HTML artifact produced by an agent.

    The ``id`` is declared as a Text field (UUID string) rather than a SERIAL
    — the ORM's migrate step detects the explicit id and applies PRIMARY KEY
    to it instead of adding a SERIAL. The UUID is the value embedded in
    capability URLs.

    Note on indexes: the ORM migrate engine supports simple single-column
    ``index=True`` declarations and multi-column ``_indexes`` via the
    ``(name, column, method)`` tuple format. Partial indexes (e.g.
    ``WHERE pinned = false``) and composite-column expressions are not
    supported by the migrator and would be a schema-only concern; we rely
    on single-column btree indexes for now.
    """

    _schema = "app"
    _name = "artifacts"

    id = fields.Text(required=True)
    title = fields.Text(required=True)
    agent_id = fields.Text(required=True, index=True)
    owner_user_id = fields.Text(required=True, index=True)
    conversation_id = fields.Text()
    file_path = fields.Text(required=True)
    size_bytes = fields.Integer(required=True)
    content_hash = fields.Text(required=True)
    pinned = fields.Boolean(default=False)
    share_token = fields.Text()
    expires_at = fields.DateTime(required=True, index=True)
    revoked_at = fields.DateTime()
    created_at = fields.DateTime(auto_now_add=True)
    updated_at = fields.DateTime(auto_now_add=True, auto_now=True)

    _constraints = [
        ("chk_artifact_size", "CHECK (size_bytes <= 10485760)"),
    ]
