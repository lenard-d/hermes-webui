from __future__ import annotations

import json
import shutil
import subprocess
import textwrap

import pytest

from tests.i18n_split_loader import (
    LOCALES,
    PARTS_DIR,
    REPO_ROOT,
    i18n_script_paths,
)


EXPECTED_LOCALE_FINGERPRINT = (
    "89fb7cc8a465855f4d49aa31c4187cb10a38322d2199737f7ccedb3a5773d689"
)


def _node() -> str:
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("node is required for i18n JavaScript verification")
    return executable


def _run_scripts_expression(expression: str) -> object:
    paths = [str(path) for path in i18n_script_paths()]
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const storage = {{}};
        const context = {{
          localStorage: {{
            getItem: (key) => storage[key] ?? null,
            setItem: (key, value) => {{ storage[key] = String(value); }},
          }},
          document: {{ documentElement: {{ lang: '' }}, querySelectorAll: () => [] }},
        }};
        context.window = context;
        vm.createContext(context);
        for (const path of {json.dumps(paths)}) {{
          vm.runInContext(fs.readFileSync(path, 'utf8'), context, {{filename: path}});
        }}
        const output = vm.runInContext({json.dumps(expression)}, context);
        process.stdout.write(JSON.stringify(output));
        """
    )
    result = subprocess.run(
        [_node(), "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def test_script_order_is_complete_and_has_no_concat_manifest():
    expected = [REPO_ROOT / "static" / "i18n.js", PARTS_DIR / "helpers.js"]
    expected.extend(PARTS_DIR / f"locale-{slug}.js" for slug, _locale in LOCALES)
    expected.append(PARTS_DIR / "runtime.js")
    assert i18n_script_paths() == expected
    assert set(PARTS_DIR.glob("*.js")) == set(expected[1:])
    assert not (PARTS_DIR / "manifest.json").exists()


def test_every_i18n_script_is_readable_and_within_the_soft_module_limit():
    over_limit = {
        path.relative_to(REPO_ROOT).as_posix(): len(
            path.read_text(encoding="utf-8").splitlines()
        )
        for path in i18n_script_paths()
        if len(path.read_text(encoding="utf-8").splitlines()) > 2_000
    }
    assert not over_limit


def test_every_script_passes_node_syntax_check():
    for path in i18n_script_paths():
        subprocess.run(
            [_node(), "--check", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )


def test_locale_parts_register_explicitly_in_the_shared_namespace():
    for slug, locale in LOCALES:
        source = (PARTS_DIR / f"locale-{slug}.js").read_text(encoding="utf-8")
        assert "global.HermesI18n" in source
        assert f"api.registerLocale({locale!r}, {{" in source
        assert source.count("api.registerLocale(") == 1
        assert "const _I18N_" not in source


def test_registered_locale_values_match_frozen_semantic_fingerprint():
    output = _run_scripts_expression(
        """(() => {
          return Object.fromEntries(
            Object.entries(LOCALES).map(([locale, bundle]) => [locale, Object.fromEntries(
              Object.keys(bundle).sort().map((key) => { const value = bundle[key]; return [key,
                typeof value === 'function'
                  ? {
                      type: 'function',
                      source: String(value)
                        .replaceAll('helpers.', '')
                        .replaceAll(' ', '')
                        .replaceAll('\\n', '')
                        .replaceAll('\\t', '')
                        .replaceAll('\\r', ''),
                    }
                  : {type: typeof value, value}
              ]; })
            )])
          );
        })()"""
    )
    import hashlib

    digest = hashlib.sha256(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert digest == EXPECTED_LOCALE_FINGERPRINT


def test_runtime_preserves_fallback_interpolation_aliases_and_public_api():
    output = _run_scripts_expression(
        """(() => {
          setLocale('zh_TW');
          const traditional = {
            resolved: resolveLocale('zh_TW'),
            saved: localStorage.getItem('hermes-lang'),
            htmlLang: document.documentElement.lang,
          };
          setLocale('it');
          return {
            locales: Object.keys(LOCALES),
            namespaceMatches: HermesI18n.locales === LOCALES,
            publicApi: ['t', 'setLocale', 'resolveLocale', 'resolvePreferredLocale',
              'loadLocale', 'applyLocaleToDOM'].every((key) => HermesI18n[key] === window[key]),
            traditional,
            positional: t('pdf_truncated', 2, 5),
            functionValue: t('n_messages', 2),
            missingKey: t('__missing_i18n_key__'),
            englishFallback: t('profile_concept_title'),
          };
        })()"""
    )
    assert output["locales"] == [locale for _slug, locale in LOCALES]
    assert output["namespaceMatches"] is True
    assert output["publicApi"] is True
    assert output["traditional"] == {
        "resolved": "zh-Hant",
        "saved": "zh-Hant",
        "htmlLang": "zh-TW",
    }
    assert output["positional"] == (
        "Showing the first 2 of 5 pages — download for the full document"
    )
    assert output["functionValue"] == "2 messaggi"
    assert output["missingKey"] == "__missing_i18n_key__"
    assert output["englishFallback"] == "Profiles vs workspaces"
