"""Strict parsers shared by publication validation and runtime loading.

YAML and JSON parsers normally keep the last occurrence of a duplicate key.
That would let validation inspect a different effective document from the one
a reviewer sees, so owned release documents reject duplicates at parse time.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import cast

import yaml

__all__ = ["load_unique_json", "load_unique_yaml"]


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses repeated keys in every mapping."""


def _construct_unique_mapping(
    loader: yaml.SafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _construct_unique_json_object(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_unique_yaml(data: str | bytes) -> object:
    """Parse safe YAML and reject duplicate mapping keys."""

    return cast(object, yaml.load(data, Loader=_UniqueKeyLoader))


def load_unique_json(data: str | bytes | bytearray) -> object:
    """Parse JSON and reject duplicate object keys."""

    return cast(object, json.loads(data, object_pairs_hook=_construct_unique_json_object))
