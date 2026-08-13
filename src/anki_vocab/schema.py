"""The schema DSL, turned into JSON Schema.

Pure transformation of a preset's `schema:` section. Raises SchemaError;
`config` rewraps it as ConfigError.
"""

from __future__ import annotations

from typing import Any


class SchemaError(ValueError):
    """A schema section that cannot be turned into JSON Schema."""


def render(template: str, **variables: str) -> str:
    """Replace `{key}` with its value, leaving unknown braces alone."""
    out = template
    for key, value in variables.items():
        out = out.replace("{" + key + "}", "" if value is None else str(value))
    return out



def normalize_spec(spec: Any) -> dict[str, Any]:
    """Accept the shorthand form `field: "description"`."""
    if isinstance(spec, str):
        return {"type": "string", "description": spec}
    if not isinstance(spec, dict):
        raise SchemaError(f"Invalid field definition: {spec!r}")
    return spec


def _make_nullable(node: dict[str, Any]) -> None:
    """Strict mode expresses "optional" as a nullable type."""
    node_type = node.get("type")
    if isinstance(node_type, str):
        node["type"] = [node_type, "null"]
    elif isinstance(node_type, list) and "null" not in node_type:
        node["type"] = [*node_type, "null"]
    if "enum" in node and None not in node["enum"]:
        node["enum"] = [*node["enum"], None]


def build(
    schema_cfg: dict[str, Any], lang_vars: dict[str, str], strict: bool = False
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, raw_spec in schema_cfg.items():
        spec = normalize_spec(raw_spec)
        ftype = spec.get("type", "string")
        node: dict[str, Any] = {"type": ftype}

        if description := spec.get("description"):
            node["description"] = render(description, **lang_vars)

        if ftype == "string":
            if "enum" in spec:
                node["enum"] = list(spec["enum"])
        elif ftype == "array":
            items = spec.get("items", "string")
            node["items"] = {"type": items} if isinstance(items, str) else items
        elif ftype == "object":
            nested = build(spec.get("properties", {}), lang_vars, strict)
            if not strict:
                nested["required"] = []
            node.update(nested)
        else:
            raise SchemaError(
                f"Unsupported type '{ftype}' on field '{name}' "
                "(use string, array or object)."
            )

        is_required = bool(spec.get("required"))
        if strict and not is_required:
            _make_nullable(node)

        properties[name] = node
        if is_required:
            required.append(name)

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties) if strict else required,
    }


def paths(schema_cfg: dict[str, Any]) -> set[str]:
    """All valid schema paths, dotted for nested ones."""
    paths: set[str] = set()

    def walk(prefix: str, node: dict[str, Any]) -> None:
        for name, raw_spec in node.items():
            path = f"{prefix}{name}"
            paths.add(path)
            spec = normalize_spec(raw_spec)
            if spec.get("type") == "object":
                walk(f"{path}.", spec.get("properties", {}))

    walk("", schema_cfg)
    return paths
