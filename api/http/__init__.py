"""Public HTTP transport interface for Hermes WebUI."""

from importlib import import_module

__all__ = ("handle_delete", "handle_get", "handle_patch", "handle_post", "handle_put")


def __getattr__(name: str):
    """Load the composition router only when a public dispatcher is requested."""
    if name not in __all__:
        raise AttributeError(name)
    value = getattr(import_module(".router", __name__), name)
    globals()[name] = value
    return value
