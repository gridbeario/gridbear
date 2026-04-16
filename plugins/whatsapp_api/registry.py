"""Global channel registry for WhatsApp Meta API.

Separate module to avoid class-identity issues when the adapter
is imported from different Python module paths (main.py vs ui.app).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapter import WhatsAppMetaChannel

_channels: dict[str, WhatsAppMetaChannel] = {}


def register(phone_number_id: str, channel: WhatsAppMetaChannel) -> None:
    _channels[phone_number_id] = channel


def unregister(phone_number_id: str) -> None:
    _channels.pop(phone_number_id, None)


def get_channel(phone_number_id: str):
    return _channels.get(phone_number_id)


def get_all_keys() -> list[str]:
    return list(_channels.keys())
