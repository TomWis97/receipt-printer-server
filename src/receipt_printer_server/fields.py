"""The input fields a template declares, and validation of submitted data.

Fields are declared in template.yaml as a list, for example::

    fields:
      - name: message
        type: string
        required: true
        max_length: 200

The same declarations drive the web form and the API's input checks.
"""

import base64
import binascii
import io
import re
from decimal import Decimal, InvalidOperation

from PIL import Image

# Keys every field may have, and the extra keys each type allows.
COMMON_KEYS = {"name", "type", "label", "help", "required", "default"}
TYPE_KEYS = {
    "string": {"multiline", "min_length", "max_length", "pattern"},
    "integer": {"min", "max"},
    "number": {"min", "max"},
    "boolean": set(),
    "choice": {"options"},
    "list": {"fields", "min_items", "max_items"},
    "image": set(),
}

NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DefinitionError(ValueError):
    """A template.yaml declares its fields incorrectly."""


class InvalidValue(ValueError):
    pass


# -- checking declarations --------------------------------------------------------


def check_fields(fields, reserved=()):
    """Check field declarations and fill in defaults. Returns the cleaned list."""
    if fields is None:
        return []
    if not isinstance(fields, list):
        raise DefinitionError("'fields' must be a list")
    cleaned = []
    names = set()
    for field in fields:
        field = check_field(field)
        if field["name"] in names:
            raise DefinitionError("field %r is declared twice" % field["name"])
        if field["name"] in reserved:
            raise DefinitionError("field name %r is reserved" % field["name"])
        names.add(field["name"])
        cleaned.append(field)
    return cleaned


def check_field(field):
    if not isinstance(field, dict) or "name" not in field:
        raise DefinitionError("every field needs a 'name'")
    name = field["name"]
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise DefinitionError("field name %r must be a valid identifier" % name)
    kind = field.get("type", "string")
    if kind not in TYPE_KEYS:
        raise DefinitionError(
            "field %r: unknown type %r (known: %s)" % (name, kind, ", ".join(TYPE_KEYS))
        )
    unknown = set(field) - COMMON_KEYS - TYPE_KEYS[kind]
    if unknown:
        raise DefinitionError(
            "field %r: unknown keys for type %s: %s" % (name, kind, ", ".join(sorted(unknown)))
        )

    field = dict(field)
    field["type"] = kind
    field.setdefault("label", name.replace("_", " ").capitalize())
    field.setdefault("required", False)
    if kind == "choice":
        if not field.get("options") or not isinstance(field["options"], list):
            raise DefinitionError("field %r: a choice needs a list of options" % name)
        field["options"] = [str(option) for option in field["options"]]
    if kind == "list":
        field["fields"] = check_fields(field.get("fields"))
        if not field["fields"]:
            raise DefinitionError("field %r: a list needs 'fields' for its items" % name)
    if "default" in field:
        try:
            field["default"] = convert(field, field["default"], name)
        except InvalidValue as exc:
            raise DefinitionError("field %r: bad default: %s" % (name, exc))
    return field


# -- validating input -----------------------------------------------------------------


def validate(fields, data, path=""):
    """Check submitted data against field declarations.

    Returns (values, errors). ``errors`` maps a field path such as
    ``items.2.amount`` to a message; it is empty when the input is valid.
    """
    values = {}
    errors = {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        errors[path or "(input)"] = "expected an object with field values"
        return values, errors

    known = {field["name"] for field in fields}
    for key in data:
        if key not in known:
            errors[join(path, key)] = "unknown field"

    for field in fields:
        name = field["name"]
        field_path = join(path, name)
        raw = data.get(name)
        if is_missing(raw):
            if field["required"]:
                errors[field_path] = "this field is required"
            else:
                values[name] = field.get("default", empty_value(field))
            continue
        try:
            values[name] = convert(field, raw, field_path, errors)
        except InvalidValue as exc:
            errors[field_path] = str(exc)
    return values, errors


def join(path, name):
    return "%s.%s" % (path, name) if path else str(name)


def is_missing(value):
    return value is None or value == "" or value == []


def empty_value(field):
    if field["type"] == "string":
        return ""
    if field["type"] == "list":
        return []
    if field["type"] == "boolean":
        return False
    return None


def convert(field, value, path, errors=None):
    """Convert one submitted value to its Python type, or raise InvalidValue.

    Values may arrive as strings (web forms, Home Assistant templates), so
    numbers and booleans are accepted in their text form too.
    """
    kind = field["type"]
    if kind == "string":
        return convert_string(field, value)
    if kind == "integer":
        return check_range(field, convert_integer(value))
    if kind == "number":
        return check_range(field, convert_number(value))
    if kind == "boolean":
        return convert_boolean(value)
    if kind == "choice":
        if str(value) not in field["options"]:
            raise InvalidValue("must be one of: %s" % ", ".join(field["options"]))
        return str(value)
    if kind == "list":
        return convert_list(field, value, path, errors)
    if kind == "image":
        return convert_image(value)
    raise InvalidValue("unknown type %r" % kind)


def convert_string(field, value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise InvalidValue("must be text")
    value = str(value)
    if "\n" in value and not field.get("multiline"):
        raise InvalidValue("must be a single line")
    if "min_length" in field and len(value) < field["min_length"]:
        raise InvalidValue("must be at least %d characters" % field["min_length"])
    if "max_length" in field and len(value) > field["max_length"]:
        raise InvalidValue("must be at most %d characters" % field["max_length"])
    if "pattern" in field and not re.fullmatch(field["pattern"], value):
        raise InvalidValue("does not match the pattern %s" % field["pattern"])
    return value


def convert_integer(value):
    if isinstance(value, bool):
        raise InvalidValue("must be a whole number")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value).strip())
    except ValueError:
        raise InvalidValue("must be a whole number")


def convert_number(value):
    """Numbers become Decimal, so money adds up exactly: 0.10 + 0.20 is 0.30."""
    if isinstance(value, bool):
        raise InvalidValue("must be a number")
    try:
        number = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation:
        raise InvalidValue("must be a number")
    if not number.is_finite():
        raise InvalidValue("must be a number")
    return number


def check_range(field, number):
    if "min" in field and number < field["min"]:
        raise InvalidValue("must be at least %s" % field["min"])
    if "max" in field and number > field["max"]:
        raise InvalidValue("must be at most %s" % field["max"])
    return number


def convert_boolean(value):
    if isinstance(value, bool):
        return value
    if str(value).strip().lower() in ("1", "true", "yes", "on"):
        return True
    if str(value).strip().lower() in ("0", "false", "no", "off"):
        return False
    raise InvalidValue("must be true or false")


def convert_list(field, value, path, errors):
    if not isinstance(value, list):
        raise InvalidValue("must be a list")
    if "min_items" in field and len(value) < field["min_items"]:
        raise InvalidValue("needs at least %d items" % field["min_items"])
    if "max_items" in field and len(value) > field["max_items"]:
        raise InvalidValue("allows at most %d items" % field["max_items"])
    items = []
    for index, item in enumerate(value):
        item_values, item_errors = validate(field["fields"], item, join(path, index))
        if errors is None and item_errors:
            # Only defaults are converted without an errors dict.
            raise InvalidValue("; ".join("%s: %s" % pair for pair in item_errors.items()))
        if errors is not None:
            errors.update(item_errors)
        items.append(item_values)
    return items


def convert_image(value):
    """Accept a data: URL or bare base64; return a data: URL the markup can use."""
    if not isinstance(value, str):
        raise InvalidValue("must be a base64 encoded image or data: URL")
    payload = value.split(",", 1)[1] if value.startswith("data:") else value
    try:
        data = base64.b64decode(payload, validate=True)
        picture = Image.open(io.BytesIO(data))
        picture.verify()
    except (binascii.Error, ValueError, OSError):
        raise InvalidValue("is not an image the server can read")
    mime = Image.MIME.get(picture.format, "application/octet-stream")
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
