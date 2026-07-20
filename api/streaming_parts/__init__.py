"""Cohesive implementation modules behind the :mod:`api.streaming` facade.

Callers should continue importing and patching ``api.streaming``.  The facade
passes that canonical module into parts whose behavior depends on patchable
streaming globals.
"""
