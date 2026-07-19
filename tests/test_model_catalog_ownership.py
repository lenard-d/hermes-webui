"""Static model/provider catalog ownership contracts."""


def test_config_and_provider_management_share_the_catalog_owner():
    import api.config as config
    import api.providers as providers
    from api import model_catalog

    assert config._FALLBACK_MODELS is model_catalog.FALLBACK_MODELS
    assert config._PROVIDER_DISPLAY is model_catalog.PROVIDER_DISPLAY
    assert config._PROVIDER_ALIASES is model_catalog.PROVIDER_ALIASES
    assert config._PROVIDER_MODELS is model_catalog.PROVIDER_MODELS
    assert providers._PROVIDER_DISPLAY is model_catalog.PROVIDER_DISPLAY
    assert providers._PROVIDER_MODELS is model_catalog.PROVIDER_MODELS


def test_catalog_provider_rows_are_independent_mutable_lists():
    from api.model_catalog import PROVIDER_MODELS

    assert PROVIDER_MODELS["openai"] is not PROVIDER_MODELS["openai-api"]
