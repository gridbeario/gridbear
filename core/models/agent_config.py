"""ORM model for agent configuration (replaces YAML files)."""

from core.orm import Model, fields


class AgentConfigRecord(Model):
    _schema = "app"
    _name = "agent_configs"
    _primary_key = "id"

    id = fields.Text(required=True)
    name = fields.Text(required=True)
    description = fields.Text(default="")
    personality = fields.Text(default="")
    locale = fields.Text(default="en")
    timezone = fields.Text(default="UTC")
    runner = fields.Text(default="claude")
    model = fields.Text(default="")
    fallback_runner = fields.Text(default="")
    tool_loading = fields.Text(default="full")
    max_tools = fields.Integer(default=0)
    avatar = fields.Text(default="")
    is_active = fields.Boolean(default=True)
    channels = fields.Json(default={})
    services = fields.Json(default=[])
    voice = fields.Json(default={})
    image = fields.Json(default={})
    email = fields.Json(default={})
    mcp_permissions = fields.Json(default=[])
    plugins = fields.Json(default={})
    context_options = fields.Json(default={})
    created_at = fields.DateTime(auto_now_add=True)
    updated_at = fields.DateTime(auto_now=True)
