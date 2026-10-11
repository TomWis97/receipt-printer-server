"""Reading and checking the YAML configuration file. See config.example.yaml."""

import os

import yaml

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8080,
    "templates": None,
    "printer": {},
    "template_settings": {},
}

PRINTER_DEFAULTS = {
    "device": None,
    "paper": "80mm",
    "dry_run": False,
}


class ConfigError(ValueError):
    pass


def load_config(path):
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError("%s must contain a mapping" % path)
    return check_config(data, os.path.dirname(os.path.abspath(path)))


def check_config(data, config_dir):
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ConfigError("unknown settings: %s" % ", ".join(sorted(unknown)))
    config = dict(DEFAULTS)
    config.update(data)

    printer = config["printer"] or {}
    unknown = set(printer) - set(PRINTER_DEFAULTS)
    if unknown:
        raise ConfigError("unknown printer settings: %s" % ", ".join(sorted(unknown)))
    config["printer"] = dict(PRINTER_DEFAULTS)
    config["printer"].update(printer)
    if config["printer"]["paper"] not in ("80mm", "58mm"):
        raise ConfigError("printer.paper must be 80mm or 58mm")

    if not config["templates"]:
        raise ConfigError("'templates' must name the directory holding the templates")
    # A relative path is relative to the config file, not to wherever we were started.
    config["templates"] = os.path.join(config_dir, config["templates"])
    if not os.path.isdir(config["templates"]):
        raise ConfigError("templates directory %s does not exist" % config["templates"])

    if not isinstance(config["port"], int):
        raise ConfigError("port must be a number")
    if not isinstance(config["template_settings"] or {}, dict):
        raise ConfigError("template_settings must map template names to settings")
    config["template_settings"] = config["template_settings"] or {}
    return config
