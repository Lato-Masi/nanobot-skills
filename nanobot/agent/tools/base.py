"""Base class for agent tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple, Type, Union

JSON_SCHEMA_TYPE = Union[str, List[str]]
PYTHON_TYPE = Union[Type[str], Type[int], Type[float], Type[bool], Type[list], Type[dict], Tuple[Type, ...]]


class Tool(ABC):
    """
    Abstract base class for agent tools.

    Tools are capabilities that the agent can use to interact with
    the environment, such as reading files, executing commands, etc.
    """

    _TYPE_MAP: Dict[str, PYTHON_TYPE] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    @staticmethod
    def _resolve_type(schema_type: JSON_SCHEMA_TYPE) -> str | None:
        """
        Resolve JSON Schema type to a simple string.

        JSON Schema allows ``"type": ["string", "null"]`` (union types).
        We extract the first non-null type so validation/casting works.
        """
        if isinstance(schema_type, list):
            for item in schema_type:
                if item != "null":
                    return item
            return None
        return schema_type

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in function calls."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what the tool does."""

    @property
    @abstractmethod
    def parameters(self) -> Dict[str, Any]:
        """JSON Schema for tool parameters."""

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """
        Execute the tool with given parameters.

        Args:
            **kwargs: Tool-specific parameters.

        Returns:
            Result of the tool execution (string or list of content blocks).
        """

    def set_context(self, **kwargs: Any) -> None:
        """Set message context for tools that need it (optional)."""

    def cast_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Apply safe schema-driven casts before validation."""
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            return params

        return self._cast_object(params, schema)

    def _cast_object(self, obj: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
        """Cast an object (dict) according to schema."""
        props = schema.get("properties", {})
        return {key: self._cast_value(value, props[key]) if key in props else value for key, value in obj.items()}

    def _cast_value(self, val: Any, schema: Dict[str, Any]) -> Any:
        """Cast a single value according to schema."""
        target_type = self._resolve_type(schema.get("type"))

        if target_type == "integer":
            try:
                return int(val)
            except (ValueError, TypeError):
                return val
        if target_type == "number":
            try:
                return float(val)
            except (ValueError, TypeError):
                return val
        if target_type == "string":
            return str(val)
        if target_type == "boolean":
            if isinstance(val, str):
                val_lower = val.lower()
                if val_lower in ("true", "1", "yes"):
                    return True
                if val_lower in ("false", "0", "no"):
                    return False
            return val
        if target_type == "array" and isinstance(val, list):
            item_schema = schema.get("items")
            return [self._cast_value(item, item_schema) for item in val] if item_schema else val
        if target_type == "object" and isinstance(val, dict):
            return self._cast_object(val, schema)

        return val

    def validate_params(self, params: Dict[str, Any]) -> List[str]:
        """Validate tool parameters against JSON schema. Returns error list (empty if valid)."""
        if not isinstance(params, dict):
            return [f"parameters must be an object, got {type(params).__name__}"]
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
        return self._validate(params, {**schema, "type": "object"}, "")

    def _validate(
        self, val: Any, schema: Dict[str, Any], path: str
    ) -> List[str]:
        """Recursively validate a value against a JSON schema."""
        errors: List[str] = []

        raw_type = schema.get("type")
        nullable = isinstance(raw_type, list) and "null" in raw_type
        target_type = self._resolve_type(raw_type)

        if nullable and val is None:
            return []

        if target_type:
            expected_type = self._TYPE_MAP.get(target_type)
            if expected_type and not isinstance(val, expected_type):
                errors.append(f"{path or 'parameter'} should be {target_type}")

        if "enum" in schema and val not in schema["enum"]:
            errors.append(f"{path or 'parameter'} must be one of {schema['enum']}")

        if isinstance(val, (int, float)):
            if "minimum" in schema and val < schema["minimum"]:
                errors.append(f"{path or 'parameter'} must be >= {schema['minimum']}")
            if "maximum" in schema and val > schema["maximum"]:
                errors.append(f"{path or 'parameter'} must be <= {schema['maximum']}")

        if isinstance(val, str):
            if "minLength" in schema and len(val) < schema["minLength"]:
                errors.append(f"{path or 'parameter'} must be at least {schema['minLength']} chars")
            if "maxLength" in schema and len(val) > schema["maxLength"]:
                errors.append(f"{path or 'parameter'} must be at most {schema['maxLength']} chars")

        if target_type == "object" and isinstance(val, dict):
            errors.extend(self._validate_object(val, schema, path))

        if target_type == "array" and isinstance(val, list):
            errors.extend(self._validate_array(val, schema, path))

        return errors

    def _validate_object(
        self, obj: Dict[str, Any], schema: Dict[str, Any], path: str
    ) -> List[str]:
        """Validate an object against a JSON schema."""
        errors: List[str] = []
        props = schema.get("properties", {})
        required = schema.get("required", [])

        for key in required:
            if key not in obj:
                errors.append(f"missing required {path + '.' + key if path else key}")

        for key, value in obj.items():
            if key in props:
                errors.extend(self._validate(value, props[key], path + "." + key if path else key))

        return errors

    def _validate_array(
        self, arr: List[Any], schema: Dict[str, Any], path: str
    ) -> List[str]:
        """Validate an array against a JSON schema."""
        errors: List[str] = []
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(arr):
                errors.extend(self._validate(item, item_schema, f"{path}[{i}]"))
        return errors

    def to_schema(self) -> Dict[str, Any]:
        """Convert tool to OpenAI function schema format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
