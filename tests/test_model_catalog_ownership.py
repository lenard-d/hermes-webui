"""Static model/provider catalog ownership contracts."""


def test_config_uses_runtime_copies_of_the_static_catalog_owner():
    import api.config as config
    import api.providers as providers
    from api import model_catalog

    assert config._FALLBACK_MODELS == model_catalog.FALLBACK_MODELS
    assert config._FALLBACK_MODELS is not model_catalog.FALLBACK_MODELS
    assert config._PROVIDER_DISPLAY == model_catalog.PROVIDER_DISPLAY
    assert config._PROVIDER_DISPLAY is not model_catalog.PROVIDER_DISPLAY
    assert config._PROVIDER_ALIASES == model_catalog.PROVIDER_ALIASES
    assert config._PROVIDER_ALIASES is not model_catalog.PROVIDER_ALIASES
    assert config._PROVIDER_MODELS is not model_catalog.PROVIDER_MODELS
    for provider, models in model_catalog.PROVIDER_MODELS.items():
        assert all(model in config._PROVIDER_MODELS[provider] for model in models)
    assert providers._PROVIDER_DISPLAY is model_catalog.PROVIDER_DISPLAY
    assert providers._PROVIDER_MODELS is model_catalog.PROVIDER_MODELS


def test_catalog_provider_rows_are_independent_mutable_lists():
    from api.config.static_catalog import PROVIDER_MODELS

    assert PROVIDER_MODELS["openai"] is not PROVIDER_MODELS["openai-api"]
