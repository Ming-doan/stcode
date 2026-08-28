"""
Signature → JSON Schema. The half of `@tool` that talks to the model.

CLAUDE.md §11: "Every tool is a pydantic model with a docstring — the docstring *is* the
prompt the model sees." This module makes that literal. A Python signature is already a
complete description of a call — names, types, optionality, defaults — so re-declaring
it as a hand-written schema is duplication that drifts. Here the signature *is* the
schema and the docstring *is* the description; nothing about a tool is written twice.

Two details worth knowing before editing:

- **Descriptions come from two places, and `Annotated` wins.** `Annotated[str, Field(
  description=...)]` sits next to the type, which is where a constraint belongs; the
  docstring's `Args:` block is the ergonomic fallback, because most tools read better
  with their parameters explained in prose underneath the summary.
- **`title` is stripped everywhere.** Pydantic titles every field and every nested
  model, which is pure restatement of the key it is filed under. Those tokens sit in
  the cached prompt prefix of every single turn (§8), so they are worth removing once
  here rather than tolerating forever.
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any, Callable, get_type_hints

import docstring_parser
from pydantic import BaseModel, Field, create_model
from pydantic.fields import FieldInfo

_SKIPPED_KINDS = frozenset({inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD})
"""`*args`/`**kwargs` are dropped rather than rejected: a tool may legitimately have
them for Python-side callers, but they have no JSON Schema spelling a model could fill
in, so they simply are not part of its advertised interface."""

# Keys whose values are themselves schema nodes. Anything not listed here is left
# untouched — notably the *keys* under `properties`, which are user-chosen parameter
# names and must never be mistaken for schema keywords.
_NODE_VALUES = ("items", "additionalProperties", "not", "contains", "propertyNames")
_NODE_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_NODE_MAPS = ("properties", "$defs", "definitions", "patternProperties")


def split_docstring(fn: Callable[..., Any]) -> tuple[str, dict[str, str]]:
    """Return the prose a tool advertises and its per-parameter descriptions.

    The `Args:` block is deliberately *excluded* from the prose: those descriptions are
    about to be attached to the parameters themselves in the schema, and repeating them
    in the description costs tokens to say the same thing in a place the model is less
    likely to associate with the argument it is filling in.
    """
    raw = inspect.getdoc(fn) or ""
    if not raw.strip():
        return "", {}

    parsed = docstring_parser.parse(raw)
    prose = "\n\n".join(part for part in (parsed.short_description, parsed.long_description) if part)
    params = {
        param.arg_name: param.description.strip()
        for param in parsed.params
        if param.arg_name and param.description
    }

    # Returns/Raises/Examples survive in the prose only if the parser did not claim
    # them; when it did, re-attach Returns, since "what comes back" is the one section
    # a caller genuinely needs and dropping it silently makes tools harder to use.
    if parsed.returns is not None and parsed.returns.description:
        prose = f"{prose}\n\nReturns: {parsed.returns.description.strip()}"
    return prose.strip(), params


def strip_titles(schema: Any) -> Any:
    """Recursively drop pydantic's auto-generated `title` keys, in place.

    Walks only through keys that hold schema nodes, so a parameter that happens to be
    called `title` keeps both its name and its own description.
    """
    if isinstance(schema, list):
        for item in schema:
            strip_titles(item)
        return schema
    if not isinstance(schema, dict):
        return schema

    schema.pop("title", None)
    for key in _NODE_VALUES:
        if isinstance(schema.get(key), dict):
            strip_titles(schema[key])
    for key in _NODE_LISTS:
        if isinstance(schema.get(key), list):
            strip_titles(schema[key])
    for key in _NODE_MAPS:
        node = schema.get(key)
        if isinstance(node, dict):
            for value in node.values():
                strip_titles(value)
    return schema


def build_params_model(
    fn: Callable[..., Any],
    *,
    exclude: frozenset[str] = frozenset(),
    model_name: str | None = None,
) -> type[BaseModel]:
    """Build the pydantic model that validates one call's arguments.

    `exclude` is how the runtime parameter disappears: it is a real parameter of the
    Python function and not a parameter of the *tool*, so it is filtered before the
    model is built rather than deleted from the schema afterwards — that way validation
    rejects a model that tries to pass one, instead of quietly accepting it.
    """
    signature = inspect.signature(fn)
    # `include_extras` keeps Annotated metadata alive; without it every Field(...)
    # description attached to a parameter is erased before we can read it.
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        # A tool annotated with a name this module cannot resolve (a TYPE_CHECKING-only
        # import, a forward reference to something local) should degrade to an untyped
        # parameter, not take the whole harness down at import time.
        hints = {}

    _, doc_params = split_docstring(fn)
    fields: dict[str, tuple[Any, FieldInfo]] = {}

    for name, parameter in signature.parameters.items():
        if name in exclude or name == "self" or parameter.kind in _SKIPPED_KINDS:
            continue

        annotation = hints.get(name, parameter.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = Any

        field = _field_for(annotation, parameter.default, doc_params.get(name))
        fields[name] = (annotation, field)

    name = model_name or f"{getattr(fn, '__name__', 'tool').title().replace('_', '')}Params"
    return create_model(name, **fields)  # type: ignore[call-overload, no-any-return]


def _field_for(annotation: Any, default: Any, doc_description: str | None) -> FieldInfo:
    """Build the extra field metadata pydantic cannot read off the signature itself.

    Deliberately minimal. Pydantic already merges an `Annotated[..., Field(...)]` with
    the field passed alongside it, so this only supplies what is genuinely missing: the
    default (which lives on the parameter, not the annotation) and a description when
    the annotation does not already carry one. Merging the annotated `FieldInfo` here
    instead loses the default — the annotation's copy has none, and it wins.
    """
    kwargs: dict[str, Any] = {}
    if doc_description and not _annotated_field_has_description(annotation):
        kwargs["description"] = doc_description
    if default is not inspect.Parameter.empty:
        kwargs["default"] = default
    # Passing `description=None` explicitly is not the same as omitting it: pydantic
    # tracks which attributes were *set*, and an explicit None clobbers the description
    # the annotation already carried.
    return Field(**kwargs)


def _annotated_field_has_description(annotation: Any) -> bool:
    if not get_origin_is_annotated(annotation):
        return False
    return any(
        isinstance(meta, FieldInfo) and meta.description for meta in annotation.__metadata__
    )


def get_origin_is_annotated(annotation: Any) -> bool:
    return hasattr(annotation, "__metadata__") and getattr(annotation, "__origin__", None) is not None


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Render a params model as the object schema a `ToolDefinition` carries."""
    schema = model.model_json_schema()
    strip_titles(schema)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema.pop("description", None)  # the model's own docstring; the tool's is separate
    return schema


def schema_from_signature(
    fn: Callable[..., Any], *, exclude: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Convenience path for callers that want the schema without the model."""
    return json_schema_for(build_params_model(fn, exclude=exclude))


__all__ = [
    "build_params_model",
    "json_schema_for",
    "schema_from_signature",
    "split_docstring",
    "strip_titles",
]

# Re-exported for tools that want to attach a description to a parameter inline.
Annotated = Annotated
