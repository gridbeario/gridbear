"""ORM models for WebChat tables (chat schema).

These models map to existing tables created by chat_api._ensure_db().
The ORM auto-migration will only add missing columns (e.g. pinned_at),
never drop existing ones.
"""

from core.orm import Model, fields


class WebchatConversation(Model):
    _schema = "chat"
    _name = "webchat_conversations"
    _primary_key = "id"

    id = fields.Text(required=True)
    unified_id = fields.Text(required=True)
    agent_name = fields.Text(default="")
    title = fields.Text(default="")
    type = fields.Text(default="private")
    context_prompt = fields.Text()
    created_at = fields.DateTime(auto_now_add=True)
    updated_at = fields.DateTime(auto_now_add=True, auto_now=True)


class WebchatParticipant(Model):
    """Participant in a conversation.

    NOTE: The DB table uses a composite PK (conversation_id, unified_id).
    The ORM default 'id' PK is not used for this table since it already
    exists with a composite PK. Use domain-based methods (search,
    write_multi, delete_multi, create_or_update with _conflict_fields).
    """

    _schema = "chat"
    _name = "webchat_participants"

    conversation_id = fields.Text(required=True)
    unified_id = fields.Text(required=True)
    role = fields.Text(default="member")
    joined_at = fields.DateTime(auto_now_add=True)
    pinned_at = fields.DateTime()


class WebchatMessage(Model):
    _schema = "chat"
    _name = "webchat_messages"

    conversation_id = fields.Text(required=True)
    role = fields.Text(required=True)
    content = fields.Text(default="")
    metadata_json = fields.Text()
    sender_id = fields.Text()
    created_at = fields.DateTime(auto_now_add=True)


class WebchatDocument(Model):
    _schema = "chat"
    _name = "webchat_documents"
    _primary_key = "id"

    id = fields.Text(required=True)
    conversation_id = fields.Text(required=True)
    filename = fields.Text(required=True)
    original_filename = fields.Text(required=True)
    file_path = fields.Text(required=True)
    file_size = fields.Integer(required=True)
    mime_type = fields.Text()
    content_text = fields.Text()
    uploaded_by = fields.Text(required=True)
    uploaded_at = fields.DateTime(auto_now_add=True)
