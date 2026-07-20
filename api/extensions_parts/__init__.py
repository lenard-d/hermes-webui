"""Cohesive implementation modules behind :mod:`api.extensions`.

Callers should keep importing :mod:`api.extensions`; the facade preserves the
historical interface and monkeypatch seams while these modules concentrate the
security and lifecycle implementations.
"""
