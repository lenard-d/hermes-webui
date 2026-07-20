"""Public interface for WebUI extension configuration and lifecycle."""

from .configuration import (
    EXTENSION_ROUTE_PREFIX,
    get_extension_config,
    get_extension_status,
    set_extension_user_enabled,
)
from .errors import ExtensionInstallError, ExtensionSidecarProxyError, ExtensionToggleError
from .gallery import get_extension_registry, install_extension, uninstall_extension
from .security import inject_extension_tags, serve_extension_static
from .sidecars import (
    resolve_extension_sidecar_proxy_target,
    set_extension_sidecar_proxy_consent,
)

__all__ = [
    "EXTENSION_ROUTE_PREFIX",
    "ExtensionInstallError",
    "ExtensionSidecarProxyError",
    "ExtensionToggleError",
    "get_extension_config",
    "get_extension_registry",
    "get_extension_status",
    "inject_extension_tags",
    "install_extension",
    "resolve_extension_sidecar_proxy_target",
    "serve_extension_static",
    "set_extension_sidecar_proxy_consent",
    "set_extension_user_enabled",
    "uninstall_extension",
]
