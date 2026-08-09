"""Packaged copies of the owned schemas enforced by the runtime.

The release loader and the dispatch path validate against these copies rather
than reading the source checkout, so an installed wheel enforces the same
boundary the contracts do. `tests/test_loading.py` keeps each packaged copy
equal to the authoritative file under `schemas/`.
"""
