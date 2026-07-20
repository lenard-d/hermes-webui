"""Private implementation modules behind the :mod:`api.models` facade.

Callers must import :mod:`api.models`, which owns initialization, synchronization,
and transactional reload of these modules. Individual parts remain importable for
inspection and tooling as an implementation detail; direct part reload is not a
supported API and can bypass the facade's compatibility guarantees.
"""

import importlib
import sys


if "api.models" not in sys.modules:
    importlib.import_module("api.models")
