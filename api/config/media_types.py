"""File-size and media-type policy shared by HTTP and workspace adapters."""

from api.config.paths import _env_mb_bytes


MAX_FILE_BYTES = 400_000
MAX_UPLOAD_BYTES = _env_mb_bytes("HERMES_WEBUI_MAX_UPLOAD_MB", 20)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp"}
MD_EXTS = {".md", ".markdown", ".mdown"}
CODE_EXTS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".css",
    ".html",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".bash",
    ".txt",
    ".log",
    ".env",
    ".csv",
    ".xml",
    ".sql",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
}
MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".bmp": "image/bmp",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
}


__all__ = [
    "CODE_EXTS",
    "IMAGE_EXTS",
    "MAX_FILE_BYTES",
    "MAX_UPLOAD_BYTES",
    "MD_EXTS",
    "MIME_MAP",
]
