"""Knowledge-source discovery, search, and read interfaces.

HTTP callers should use this package interface.  The implementation keeps
profile-scoped configuration, external I/O, source classification, and wiki
filesystem safety out of the route facade.
"""

from .adapters import (
    JoplinHTTPAdapter,
    active_config_snapshot,
    active_hermes_home,
    joplin_connection,
)
from .joplin import get_joplin_note, recent_ai_notes, search_joplin_notes
from .parsing import (
    note_snippet,
    parse_joplin_recall_refs,
    script_path_from_config_value,
)
from .sources import (
    configured_note_tool_hints,
    discover_note_sources,
    external_notes_sources_enabled,
    looks_like_notes_source,
    note_source_label,
)
from .wiki_index import (
    WikiIndex,
    build_wiki_status,
    clear_wiki_page_cache,
    resolve_wiki_path,
)
from .wiki_read import browse_wiki_pages, read_wiki_page

__all__ = (
    "JoplinHTTPAdapter",
    "WikiIndex",
    "active_config_snapshot",
    "active_hermes_home",
    "browse_wiki_pages",
    "build_wiki_status",
    "clear_wiki_page_cache",
    "configured_note_tool_hints",
    "discover_note_sources",
    "external_notes_sources_enabled",
    "get_joplin_note",
    "joplin_connection",
    "looks_like_notes_source",
    "note_snippet",
    "note_source_label",
    "parse_joplin_recall_refs",
    "read_wiki_page",
    "recent_ai_notes",
    "resolve_wiki_path",
    "script_path_from_config_value",
    "search_joplin_notes",
)
