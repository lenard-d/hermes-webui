"""Attachment validation, image routing, and multimodal message assembly."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path


_NATIVE_IMAGE_MAX_BYTES = 20 * 1024 * 1024
_IMAGE_MAGIC: dict[bytes | None, frozenset[str]] = {
    b"\x89PNG\r\n\x1a\n": frozenset({"image/png"}),
    b"\xff\xd8\xff": frozenset({"image/jpeg"}),
    b"GIF87a": frozenset({"image/gif"}),
    b"GIF89a": frozenset({"image/gif"}),
    b"RIFF": frozenset({"image/webp"}),
    b"BM": frozenset({"image/bmp"}),
    None: frozenset({"image/svg+xml"}),
}


def _attachment_name(att) -> str:
    if isinstance(att, dict):
        return str(att.get('name') or att.get('filename') or att.get('path') or '').strip()
    return str(att or '').strip()


def _is_valid_image(path: Path, mime: str) -> bool:
    """Check that the file's first bytes match the expected image MIME type.

    Uses simple magic-number detection (no external dependency). SVG is
    allowed through because it is text-based and has no binary signature.
    """
    if not mime.startswith('image/'):
        return False
    mime_base = mime.split(';', 1)[0]
    if mime_base == 'image/svg+xml':
        return True
    try:
        with path.open('rb') as fh:
            head = fh.read(16)
    except OSError:
        return False
    for magic, mimes in _IMAGE_MAGIC.items():
        if magic is not None and head.startswith(magic) and mime_base in mimes:
            return True
    return False


def _explicit_text_signal(cfg: dict) -> bool:
    """True when the user has explicitly opted into the text (vision_analyze)
    image pipeline.

    Two explicit signals, either of which means "the user chose text on
    purpose" and we must honour it rather than forwarding images natively:

      * ``agent.image_input_mode: text`` — a direct mode override.
      * a configured ``auxiliary.vision`` backend (provider not ``auto``/empty,
        or an explicit model / base_url) — the user is paying for a dedicated
        vision model and wants the text pipeline regardless of the main model.

    This mirrors the explicit-signal portion of
    ``agent/image_routing.py:decide_image_input_mode`` and is used both to
    interpret *why* the canonical router returned ``"text"`` (so the
    unknown-model carve-out only fires when there's no explicit user choice)
    and as the fallback decision when the agent package is unavailable.
    """
    if not isinstance(cfg, dict):
        return False
    agent_cfg = cfg.get("agent") or {}
    if isinstance(agent_cfg, dict):
        mode = str(agent_cfg.get("image_input_mode", "auto") or "auto").strip().lower()
        if mode == "text":
            return True
    aux = cfg.get("auxiliary") or {}
    vision = (aux.get("vision") or {}) if isinstance(aux, dict) else {}
    if not isinstance(vision, dict):
        return False
    provider = str(vision.get("provider") or "").strip().lower()
    model_name = str(vision.get("model") or "").strip()
    base_url = str(vision.get("base_url") or "").strip()
    return provider not in ("", "auto") or bool(model_name) or bool(base_url)


def _resolve_image_input_mode(cfg: dict) -> str:
    """Return ``"native"`` or ``"text"`` for current-turn image uploads.

    Delegates the routing decision to ``agent/image_routing.py:
    decide_image_input_mode`` — the single source of truth — instead of the
    local re-implementation that previously lived here. That copy had DIVERGED
    from the canonical function: it returned ``"text"`` (dropping the image) in
    cases the canonical router would have forwarded natively, because it never
    consulted the active model's vision capability and instead hard-coded a
    handful of config heuristics.

    The WebUI keeps one deliberate carve-out on top of the canonical decision:
    for UNKNOWN / custom models (no models.dev capability data) the canonical
    router conservatively returns ``"text"``, but the WebUI historically
    forwards images NATIVELY and relies on the agent's strip-and-retry guard
    (``run_agent._try_shrink_image_parts_in_messages`` /
    ``_strip_images_from_messages``) to downgrade on a provider rejection. We
    preserve that behaviour here: a canonical ``"text"`` verdict is only
    honoured when there is a real signal — an explicit user choice
    (``image_input_mode: text`` or a configured ``auxiliary.vision`` backend)
    or a model KNOWN to lack vision. Otherwise we forward native.

    When the agent package is unavailable (e.g. the WebUI standalone test
    environment, where ``import agent`` fails), we fall back to the historical
    WebUI behaviour: honour an explicit text signal, otherwise native.
    """
    if not isinstance(cfg, dict):
        cfg = {}

    try:
        from agent.image_routing import decide_image_input_mode, _lookup_supports_vision
        from agent.auxiliary_client import _read_main_provider, _read_main_model

        provider = (_read_main_provider() or "").strip()
        model = (_read_main_model() or "").strip()

        mode = decide_image_input_mode(provider, model, cfg)
        if mode == "native":
            return "native"

        # Canonical returned "text". Honour it only when it reflects a genuine
        # signal; otherwise apply the WebUI unknown-model native carve-out.
        if _explicit_text_signal(cfg):
            return "text"
        if _lookup_supports_vision(provider, model, cfg) is False:
            # Model is KNOWN to be text-only — respect the canonical verdict.
            return "text"
        # Unknown / custom model (capability is None): WebUI forwards native
        # and lets the agent's strip-and-retry guard downgrade on rejection.
        return "native"
    except Exception:
        # Agent package unavailable or import error — preserve historical WebUI
        # behaviour: explicit text signal wins, otherwise native.
        pass

    if _explicit_text_signal(cfg):
        return "text"
    return "native"


def _build_native_multimodal_message(workspace_ctx: str, msg_text: str, attachments, workspace: str, *, cfg: dict = None):
    """Build native multimodal content parts for current-turn image uploads.

    WebUI uploads files into the active workspace. For image files, pass the
    bytes to Hermes as OpenAI-style image_url data URLs so vision-capable main
    models can consume them in the same request. Non-image files intentionally
    stay as text path attachments so the agent can inspect them with file tools.

    When *cfg* is provided, respects ``agent.image_input_mode`` — if the resolved
    mode is ``"text"``, returns a plain string (attachments are not embedded) so
    the agent's text-mode pipeline (``vision_analyze``) handles images.
    """
    if not attachments:
        return workspace_ctx + msg_text

    # ── Check image_input_mode before embedding anything ──
    if cfg is not None and _resolve_image_input_mode(cfg) == "text":
        return workspace_ctx + msg_text

    parts = [{'type': 'text', 'text': workspace_ctx + msg_text}]
    workspace_root = Path(workspace).expanduser().resolve()
    # Stage-361 maintainer fix (Opus SHOULD-FIX): chat uploads from #2319 now
    # land in ~/.hermes/webui/attachments/<sid>/ (outside workspace_root by
    # design). The pre-existing `path.relative_to(workspace_root)` guard would
    # silently reject every image upload for vision-capable models. Allow the
    # configured attachment root in addition to workspace_root so native
    # multimodal embeds still build the base64 image_url part. The
    # _attachment_root() helper applies expanduser+resolve and is also reused
    # by _upload_destination — single source of truth for the inbox root.
    try:
        from api.media.uploads import attachment_root as resolve_attachment_root
        attachment_root = resolve_attachment_root()
        _allowed_roots = (workspace_root, attachment_root)
    except Exception:
        _allowed_roots = (workspace_root,)
    image_count = 0

    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        raw_path = str(att.get('path') or '').strip()
        if not raw_path:
            continue
        try:
            path = Path(raw_path).expanduser().resolve()
            # Uploads should live inside the selected workspace OR the
            # session attachment inbox (#2319). Do not read arbitrary paths
            # from client-provided attachment metadata.
            if not any(path.is_relative_to(r) for r in _allowed_roots):
                continue
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size <= 0 or size > _NATIVE_IMAGE_MAX_BYTES:
                continue
            mime = str(att.get('mime') or '').strip() or (mimetypes.guess_type(path.name)[0] or '')
            if not mime.startswith('image/') or not _is_valid_image(path, mime):
                continue
            data = base64.b64encode(path.read_bytes()).decode('ascii')
        except Exception:
            continue
        parts.append({
            'type': 'image_url',
            'image_url': {'url': f'data:{mime};base64,{data}'},
        })
        image_count += 1

    return parts if image_count else workspace_ctx + msg_text
