from pathlib import Path
import re
from tests.i18n_split_loader import locale_block_source, source_shaped_i18n
from tests.test_issue2147_profile_concept_help import PROFILE_CONCEPT_KEYS


REPO = Path(__file__).resolve().parent.parent
PROFILE_CONCEPT_FALLBACK_KEYS = set(PROFILE_CONCEPT_KEYS)
KNOWN_ENGLISH_FALLBACK_KEYS = PROFILE_CONCEPT_FALLBACK_KEYS | {
    "bg_complete",
    "bg_failed",
    "bg_label",
    "bg_no_answer",
    "bg_running",
    "btw_asking",
    "btw_done",
    "btw_failed",
    "btw_label",
    "btw_no_answer",
    "cancel_unavailable",
    "cmd_voice",
    "cmd_voice_use_mic",
    "cmd_webui_only_session",
    "no_active_task",
    "retry_failed",
    "status_agent_running",
    "status_heading",
    "status_load_failed",
    "status_messages",
    "status_model",
    "status_no",
    "status_personality",
    "status_provider",
    "status_session_id",
    "status_title",
    "status_workspace",
    "status_yes",
    "stream_stopped",
    "title_change_hint",
    "title_current",
    "title_set",
    "undid_messages_suffix",
    "undid_n_messages",
    "undo_exchange",
    "undo_failed",
    "usage_default_model",
    "usage_estimated_cost",
    "usage_heading",
    "usage_input_tokens",
    "usage_load_failed",
    "usage_output_tokens",
    "usage_settings_tip",
    "usage_total",
    "usage_unknown",
}


def read(path: Path) -> str:
    if path == REPO / "static" / "i18n.js":
        return source_shaped_i18n()
    return path.read_text(encoding="utf-8")


def test_spanish_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    assert "\n  es: {" in src
    assert "_label: 'Español'" in src
    assert "_speech: 'es-ES'" in src


def test_spanish_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    expected = [
        "settings_title: 'Configuración'",
        "login_title: 'Iniciar sesión'",
        "approval_heading: 'Se requiere aprobación'",
        "tab_tasks: 'Tareas'",
        "tab_skills: 'Habilidades'",
        "tab_memory: 'Memoria'",
    ]
    for entry in expected:
        assert entry in src


def test_spanish_locale_covers_english_keys():
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    en_keys = set(key_pattern.findall(locale_block_source("en")))
    es_keys = set(key_pattern.findall(locale_block_source("es")))

    assert en_keys - es_keys == KNOWN_ENGLISH_FALLBACK_KEYS
