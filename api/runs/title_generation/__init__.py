"""Session-title generation interface.

Callers that only need on-demand generation use this package Interface.  Local
run lifecycle code imports the explicit policy and lifecycle owners directly.
"""

from .lifecycle import generate_session_title_for_session

__all__ = ("generate_session_title_for_session",)
