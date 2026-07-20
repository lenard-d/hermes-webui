"""Sanitized errors exposed by the extension package interface."""


class ExtensionToggleError(Exception):
    """Extension toggle failure safe to return to the browser."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ExtensionInstallError(Exception):
    """Extension install or uninstall failure safe for browser responses."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ExtensionSidecarProxyError(Exception):
    """Sidecar proxy failure safe to return to the browser."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


__all__ = [
    "ExtensionInstallError",
    "ExtensionSidecarProxyError",
    "ExtensionToggleError",
]
