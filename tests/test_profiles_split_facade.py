"""Compatibility contracts for the extracted profile implementation owners."""

import builtins
import dis
import inspect
import pickle

import api.profiles as profiles
from api.profiles_parts import catalog, cron_scope, management, runtime_scope


MOVED_FUNCTIONS = {
    runtime_scope: (
        "snapshot_skill_home_modules",
        "restore_skill_home_modules",
        "_stringify_env_value",
        "get_profile_runtime_env",
        "filter_runtime_env_for_gateway_parity",
        "_agent_registry_credential_env_names",
        "_profile_secret_env_names",
        "_apply_profile_env_to_process",
        "_resolve_secret_scope_module",
        "_resolve_hermes_home_override",
        "_set_hermes_home",
        "_reload_dotenv",
    ),
    cron_scope: (
        "_cron_profile_context_depth",
        "_push_cron_profile_context_depth",
        "_pop_cron_profile_context_depth",
        "_home_for_scheduled_cron_job",
        "install_cron_scheduler_profile_isolation",
    ),
    catalog: (
        "_skills_stats_lock_for",
        "_skill_tree_max_mtime_ns",
        "_compute_profile_skills_stats",
        "_get_profile_skills_stats",
        "_invalidate_list_profiles_cache",
        "_build_profile_rows_fast",
        "list_profiles_api",
        "_profile_visible_from_meta",
        "_default_profile_dict",
    ),
    management: (
        "_validate_profile_name",
        "_profiles_root",
        "_resolve_named_profile_home",
        "_create_profile_fallback",
        "_resolve_env_var_for_provider",
        "_upsert_dotenv_line",
        "_write_api_key_to_dotenv",
        "_write_endpoint_to_config",
        "_clean_profile_config_value",
        "_split_webui_provider_model_value",
        "_strip_webui_provider_prefix",
        "_profile_model_selection_exists",
        "_get_available_models_for_profile_validation",
        "_validate_profile_model_selection",
        "_write_model_defaults_to_config",
        "create_profile_api",
        "delete_profile_api",
    ),
}


def test_moved_functions_are_real_picklable_facade_functions():
    for owner, names in MOVED_FUNCTIONS.items():
        for name in names:
            facade_function = getattr(profiles, name)
            owner_function = getattr(owner, name)

            assert facade_function is not owner_function
            assert facade_function.__code__ is owner_function.__code__
            assert facade_function.__module__ == "api.profiles"
            assert facade_function.__globals__ is vars(profiles)
            assert inspect.unwrap(facade_function).__globals__ is vars(profiles)
            assert pickle.loads(pickle.dumps(facade_function)) is facade_function


def test_moved_function_global_dependencies_exist_on_facade():
    for names in MOVED_FUNCTIONS.values():
        for name in names:
            facade_function = getattr(profiles, name)
            required_globals = {
                instruction.argval
                for instruction in dis.get_instructions(facade_function)
                if instruction.opname in {"LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"}
            }
            missing = {
                global_name
                for global_name in required_globals
                if global_name not in vars(profiles)
                and not hasattr(builtins, global_name)
            }

            assert missing == set(), f"{name} is missing facade globals: {sorted(missing)}"


def test_moved_public_context_managers_keep_facade_identity():
    names = (
        "profile_env_for_background_worker",
        "profile_env_for_active_request_readonly",
        "profile_env_for_active_request",
        "profile_scope_for_detached_worker",
    )

    for name in names:
        facade_function = getattr(profiles, name)

        assert facade_function.__module__ == "api.profiles"
        assert inspect.unwrap(facade_function).__globals__ is vars(profiles)
        assert pickle.loads(pickle.dumps(facade_function)) is facade_function
