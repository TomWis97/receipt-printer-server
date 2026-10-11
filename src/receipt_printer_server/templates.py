"""Loading print templates from their directories, and rendering them.

Every subdirectory of the templates directory is one template::

    templates/log-line/
        template.yaml   title, description, fields, default settings
        template.j2     Jinja2 that renders to print markup (see markup.py)
        helpers.py      optional: functions the template can call
        logo.png        optional: images, referenced as <image src="logo.png"/>

A template that fails to load is still listed, with its error, so a mistake
in one template is visible without taking the others down. Templates are
reloaded as soon as any of their files change; see TemplateStore.
"""

import datetime
import inspect
import logging
import os
import threading
import types

import jinja2
import yaml

from .charsets import printable
from .fields import DefinitionError, check_fields

log = logging.getLogger(__name__)

DEFINITION_FILE = "template.yaml"
JINJA_FILE = "template.j2"
HELPERS_FILE = "helpers.py"

# Names the server puts in every template's context; fields cannot use them.
RESERVED_NAMES = {"settings", "now", "printable"}


class PrintTemplate:
    def __init__(self, name, directory):
        self.name = name
        self.directory = directory
        self.title = name
        self.description = ""
        self.fields = []
        self.settings = {}
        self.error = None
        self.environment = None

    def load(self, configured_settings):
        definition = read_yaml(os.path.join(self.directory, DEFINITION_FILE))
        if not isinstance(definition, dict):
            raise DefinitionError("%s must contain a mapping" % DEFINITION_FILE)
        unknown = set(definition) - {"title", "description", "fields", "settings"}
        if unknown:
            raise DefinitionError("unknown keys in %s: %s" % (DEFINITION_FILE, ", ".join(sorted(unknown))))

        self.title = definition.get("title", self.name)
        self.description = definition.get("description", "")
        self.fields = check_fields(definition.get("fields"), RESERVED_NAMES)
        # Settings from template.yaml are defaults; the server config overrides them.
        self.settings = dict(definition.get("settings") or {})
        self.settings.update(configured_settings or {})

        if not os.path.isfile(os.path.join(self.directory, JINJA_FILE)):
            raise DefinitionError("%s is missing" % JINJA_FILE)
        self.environment = make_environment(self.directory)
        for name, function in load_helpers(self.name, self.directory).items():
            self.environment.globals[name] = function
            self.environment.filters[name] = function
        # Compile now, so syntax errors show up at startup rather than at print time.
        self.environment.get_template(JINJA_FILE)

    def render(self, values):
        """Render the template with validated values. Returns print markup."""
        template = self.environment.get_template(JINJA_FILE)
        return template.render(values, settings=self.settings)

    def describe(self):
        """What the API and web UI show about this template."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "fields": self.fields,
            "error": self.error,
        }


def make_environment(directory):
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(directory),
        # Escaping keeps "<" and "&" in submitted data from being read as markup.
        autoescape=True,
        # Lines holding only {% ... %} disappear instead of printing blank lines.
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        # A misspelt variable is an error, not an empty string on paper.
        undefined=jinja2.StrictUndefined,
        # TemplateStore reloads the whole template - fields, helpers and all -
        # when a file changes; Jinja reloading only template.j2 on its own
        # could pair a new template with old fields.
        auto_reload=False,
    )
    environment.globals["now"] = datetime.datetime.now
    environment.filters["printable"] = printable
    return environment


def load_helpers(template_name, directory):
    """Import helpers.py and return its public functions by name."""
    path = os.path.join(directory, HELPERS_FILE)
    if not os.path.isfile(path):
        return {}
    module_name = "print_templates.%s.helpers" % template_name.replace("-", "_")
    module = types.ModuleType(module_name)
    module.__file__ = path
    # Compiled from source every time: Python's bytecode cache only notices a
    # change in size or in whole seconds, so it could run an older version.
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    exec(compile(source, path, "exec"), module.__dict__)
    return {
        name: function
        for name, function in inspect.getmembers(module, inspect.isfunction)
        # Skip private helpers and functions the module merely imported.
        if not name.startswith("_") and function.__module__ == module_name
    }


def read_yaml(path):
    if not os.path.isfile(path):
        raise DefinitionError("%s is missing" % os.path.basename(path))
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class TemplateStore:
    """Every template, reloaded from disk whenever one of its files changes.

    ``current()`` is called at the start of each request. It compares each
    template directory's files (name, size, modification time) with what was
    seen last time and reloads only templates that changed, so editing a
    template, its helpers or its images takes effect on the next request.
    """

    def __init__(self, directory, template_settings):
        self.directory = directory
        self.template_settings = template_settings
        self.templates = {}
        self.fingerprints = {}
        self.lock = threading.Lock()

    def current(self):
        """Reload what changed. Returns {name: PrintTemplate}."""
        with self.lock:
            names = template_names(self.directory)
            for name in list(self.templates):
                if name not in names:
                    log.info("template %s was removed", name)
                    del self.templates[name]
                    del self.fingerprints[name]
            for name in names:
                path = os.path.join(self.directory, name)
                fingerprint = directory_fingerprint(path)
                if self.fingerprints.get(name) != fingerprint:
                    self.templates[name] = load_template(
                        name, path, self.template_settings.get(name)
                    )
                    self.fingerprints[name] = fingerprint
            return dict(self.templates)


def template_names(directory):
    return [
        name
        for name in sorted(os.listdir(directory))
        if not name.startswith((".", "_"))
        and os.path.isdir(os.path.join(directory, name))
    ]


def directory_fingerprint(path):
    """What changes when any file in a template directory changes."""
    fingerprint = []
    for root, directories, files in os.walk(path):
        directories.sort()
        for name in sorted(files):
            info = os.stat(os.path.join(root, name))
            fingerprint.append((os.path.join(root, name), info.st_size, info.st_mtime_ns))
    return fingerprint


def load_template(name, path, settings):
    template = PrintTemplate(name, path)
    try:
        template.load(settings)
        log.info("loaded template %s", name)
    except Exception as exc:
        # Kept and shown in the UI and API, so the error is not lost.
        template.error = "%s: %s" % (type(exc).__name__, exc)
        log.error("template %s failed to load: %s", name, template.error)
    return template


def load_templates(directory, template_settings):
    """Load every template under ``directory`` once. Returns {name: PrintTemplate}."""
    templates = TemplateStore(directory, template_settings).current()
    unknown = set(template_settings) - set(templates)
    if unknown:
        log.warning("settings given for unknown templates: %s", ", ".join(sorted(unknown)))
    return templates
