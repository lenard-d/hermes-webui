"""Kanban domain interface.

HTTP callers use the four verb handlers. Other domains may use the backend and
board metadata helpers without reaching into Kanban implementation modules.
"""

from .boards import _board_meta_dict as serialize_board_metadata
from .http import (
    handle_kanban_delete,
    handle_kanban_get,
    handle_kanban_patch,
    handle_kanban_post,
)
from .integration import _kb as get_backend

__all__ = [
    "get_backend",
    "handle_kanban_delete",
    "handle_kanban_get",
    "handle_kanban_patch",
    "handle_kanban_post",
    "serialize_board_metadata",
]
