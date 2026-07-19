"""Validate every checked-in DevClean JSON Schema without network access."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing import Resource as SchemaResource

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"
EXPECTED_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _load_schema(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-standard JSON constant: {value}")
        ),
    )
    if not isinstance(value, dict):
        raise ValueError(f"schema root must be an object: {path}")
    return value


def _iter_refs(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str):
            yield ref
        for child in value.values():
            yield from _iter_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_refs(child)


def validate_schema_directory(schema_dir: Path = SCHEMA_DIR) -> tuple[Path, ...]:
    paths = tuple(sorted(schema_dir.glob("*.schema.json")))
    if not paths:
        raise ValueError(f"no JSON Schemas found under {schema_dir}")

    schemas: list[tuple[Path, dict[str, Any]]] = []
    resources: list[tuple[str, SchemaResource[Any]]] = []
    seen_ids: set[str] = set()
    for path in paths:
        schema = _load_schema(path)
        if schema.get("$schema") != EXPECTED_DIALECT:
            raise ValueError(f"{path.name}: expected JSON Schema 2020-12 dialect")
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id.startswith("https://"):
            raise ValueError(f"{path.name}: $id must be an absolute HTTPS URI")
        if schema_id in seen_ids:
            raise ValueError(f"duplicate schema $id: {schema_id}")
        seen_ids.add(schema_id)
        Draft202012Validator.check_schema(schema)
        schemas.append((path, schema))
        resources.append((schema_id, SchemaResource.from_contents(schema)))

    registry = Registry().with_resources(resources)
    for path, schema in schemas:
        schema_id = str(schema["$id"])
        resolver = registry.resolver(schema_id)
        for ref in _iter_refs(schema):
            target_document, _ = urldefrag(urljoin(schema_id, ref))
            if target_document not in seen_ids:
                raise ValueError(f"{path.name}: unresolved or non-local $ref {ref!r}")
            try:
                resolver.lookup(ref)
            except Exception as error:
                raise ValueError(f"{path.name}: invalid $ref {ref!r}: {error}") from error

    return paths


def main() -> int:
    paths = validate_schema_directory()
    print(f"Validated {len(paths)} local JSON Schemas (draft 2020-12).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
