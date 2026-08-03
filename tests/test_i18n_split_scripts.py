from __future__ import annotations

import json
import re
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


def _run_lazy_locale_case(locale: str) -> object:
    initial_paths = [
        REPO_ROOT / "static" / "i18n.js",
        PARTS_DIR / "helpers.js",
        PARTS_DIR / "locale-en.js",
        PARTS_DIR / "runtime.js",
    ]
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const path = require('path');
        const vm = require('vm');
        const storage = {{}};
        const requested = [];
        const context = {{
          URL,
          console,
          localStorage: {{
            getItem: (key) => storage[key] ?? null,
            setItem: (key, value) => {{ storage[key] = String(value); }},
          }},
          document: {{
            currentScript: null,
            documentElement: {{ lang: '' }},
            querySelectorAll: () => [],
            createElement: () => ({{}}),
            head: {{ appendChild: null }},
          }},
        }};
        context.window = context;
        vm.createContext(context);
        context.document.head.appendChild = (script) => {{
          requested.push(script.src);
          const filename = path.basename(new URL(script.src).pathname);
          const localePath = path.join({json.dumps(str(PARTS_DIR))}, filename);
          vm.runInContext(fs.readFileSync(localePath, 'utf8'), context, {{filename: localePath}});
          Promise.resolve().then(() => script.onload());
        }};
        const initialPaths = {json.dumps([str(path) for path in initial_paths])};
        for (const sourcePath of initialPaths) {{
          context.document.currentScript = {{
            src: 'https://example.test/static/i18n_parts/' + path.basename(sourcePath) + '?v=test-build',
          }};
          vm.runInContext(fs.readFileSync(sourcePath, 'utf8'), context, {{filename: sourcePath}});
        }}
        context.document.currentScript = null;
        Promise.all([
          vm.runInContext({json.dumps(f"setLocale({json.dumps(locale)})")}, context),
          vm.runInContext({json.dumps(f"setLocale({json.dumps(locale)})")}, context),
        ]).then(() => {{
          const output = vm.runInContext(`({{
            locales: Object.keys(LOCALES),
            supported: SUPPORTED_LOCALES.map((entry) => entry.code),
            active: _locale === LOCALES[{json.dumps(locale)}],
            settingsTitle: t('settings_title'),
            saved: localStorage.getItem('hermes-lang'),
            htmlLang: document.documentElement.lang,
          }})`, context);
          output.requested = requested;
          process.stdout.write(JSON.stringify(output));
        }}).catch((error) => {{
          console.error(error);
          process.exitCode = 1;
        }});
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


def test_initial_html_only_loads_english_locale_bundle():
    index_source = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    eager_locale_slugs = re.findall(
        r'i18n_parts/locale-([a-z_]+)\.js', index_source
    )
    assert eager_locale_slugs == ["en"]


def test_service_worker_only_precaches_english_locale_bundle():
    worker_source = (REPO_ROOT / "static" / "sw.js").read_text(encoding="utf-8")
    shell_assets = worker_source[worker_source.index("const SHELL_ASSETS") : worker_source.index("function deleteOldShellCaches")]
    precached_locale_slugs = re.findall(
        r'i18n_parts/locale-([a-z_]+)\.js', shell_assets
    )
    assert precached_locale_slugs == ["en"]
    assert "Translation bundles other than English are loaded only when selected" in worker_source


def test_non_english_locale_is_loaded_once_on_demand():
    output = _run_lazy_locale_case("de")
    assert output["locales"] == ["en", "de"]
    assert output["supported"] == [locale for _slug, locale in LOCALES]
    assert output["active"] is True
    assert output["settingsTitle"] == "Einstellungen"
    assert output["saved"] == "de"
    assert output["htmlLang"] == "de-DE"
    assert output["requested"] == [
        "https://example.test/static/i18n_parts/locale-de.js?v=test-build"
    ]


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
