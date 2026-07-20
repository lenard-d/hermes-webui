import re
from tests.i18n_split_loader import locale_block_source


LOGS_FILTER_KEYS = {
    "ja": {
        "logs_severity": "重大度",
        "logs_severity_all": "すべて",
        "logs_severity_errors": "エラー",
        "logs_severity_warnings": "警告+",
        "logs_filter_active": "表示中（フィルター有効）",
    },
    "ru": {
        "logs_severity": "Уровень",
        "logs_severity_all": "Все",
        "logs_severity_errors": "Ошибки",
        "logs_severity_warnings": "Предупреждения+",
        "logs_filter_active": "показано (фильтр активен)",
    },
    "es": {
        "logs_severity": "Severidad",
        "logs_severity_all": "Todo",
        "logs_severity_errors": "Errores",
        "logs_severity_warnings": "Advertencias+",
        "logs_filter_active": "mostrados (filtro activo)",
    },
    "de": {
        "logs_severity": "Schweregrad",
        "logs_severity_all": "Alle",
        "logs_severity_errors": "Fehler",
        "logs_severity_warnings": "Warnungen+",
        "logs_filter_active": "angezeigt (Filter aktiv)",
    },
    "zh": {
        "logs_severity": "严重性",
        "logs_severity_all": "全部",
        "logs_severity_errors": "错误",
        "logs_severity_warnings": "警告+",
        "logs_filter_active": "已显示（筛选器已启用）",
    },
    "zh-Hant": {
        "logs_severity": "嚴重性",
        "logs_severity_all": "全部",
        "logs_severity_errors": "錯誤",
        "logs_severity_warnings": "警告+",
        "logs_filter_active": "已顯示（篩選器已啟用）",
    },
    "pt": {
        "logs_severity": "Severidade",
        "logs_severity_all": "Todos",
        "logs_severity_errors": "Erros",
        "logs_severity_warnings": "Avisos+",
        "logs_filter_active": "exibidos (filtro ativo)",
    },
    "ko": {
        "logs_severity": "심각도",
        "logs_severity_all": "전체",
        "logs_severity_errors": "오류",
        "logs_severity_warnings": "경고+",
        "logs_filter_active": "표시됨(필터 활성)",
    },
}


def _i18n_locale_block(locale: str) -> str:
    return locale_block_source(locale)


def _string_value(block: str, key: str) -> str:
    match = re.search(rf"^\s+{re.escape(key)}:\s+'([^']*)',(?P<tail>[^\n]*)$", block, re.M)
    assert match, f"{key} missing"
    assert "TODO: translate" not in match.group("tail")
    return match.group(1)


def test_logs_severity_filter_keys_are_translated_for_non_english_locales():
    for locale, expected in LOGS_FILTER_KEYS.items():
        block = _i18n_locale_block(locale)
        for key, value in expected.items():
            assert _string_value(block, key) == value
