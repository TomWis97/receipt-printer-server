"""The REST API and the web UI.

    GET  /                                 web UI
    GET  /api/templates                    every template and the fields it takes
    GET  /api/templates/<name>             one template
    POST /api/templates/<name>/print       validate, render and print
    POST /api/templates/<name>/preview     validate and render, return the markup
    GET  /api/status                       the printer's own status report

Print and preview take the field values as a JSON object. Errors come back as
``{"error": "...", "fields": {"path": "message"}}`` with these status codes:

    400  the input does not match the template's fields
    404  no such template
    422  the template rendered, but the result cannot be printed
    500  the template is broken, or its code raised an error
    503  the printer cannot print right now (cover open, offline, ...)
"""

import logging
import os

from flask import Flask, jsonify, request, send_from_directory

from epson_tm_t88v import PrinterError

from .fields import validate
from .printing import PrinterUnavailable

log = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class ApiError(Exception):
    def __init__(self, status, message, fields=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.fields = fields


def create_app(template_store, printer_service):
    app = Flask(__name__, static_folder=STATIC_DIR)

    @app.errorhandler(ApiError)
    def api_error(error):
        body = {"error": error.message}
        if error.fields:
            body["fields"] = error.fields
        return jsonify(body), error.status

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/templates")
    def list_templates():
        templates = template_store.current()
        return jsonify([template.describe() for template in templates.values()])

    @app.get("/api/templates/<name>")
    def show_template(name):
        return jsonify(find_template(template_store, name).describe())

    @app.post("/api/templates/<name>/preview")
    def preview(name):
        template = find_template(template_store, name)
        markup = render(template, read_input())
        data = check(printer_service, template, markup)
        return jsonify({"markup": markup, "bytes": len(data)})

    @app.post("/api/templates/<name>/print")
    def print_template(name):
        template = find_template(template_store, name)
        markup = render(template, read_input())
        try:
            printer_service.print(markup, template.directory)
        except PrinterUnavailable as exc:
            raise ApiError(503, "the printer cannot print: %s" % exc)
        except (ValueError, PrinterError) as exc:
            raise ApiError(422, "this cannot be printed: %s" % exc)
        log.info("printed template %s", name)
        return jsonify({"result": "printed"})

    @app.get("/api/status")
    def status():
        return jsonify(printer_service.status())

    return app


def find_template(template_store, name):
    templates = template_store.current()
    if name not in templates:
        raise ApiError(404, "no template named %r" % name)
    template = templates[name]
    if template.error:
        raise ApiError(500, "template %s is broken: %s" % (name, template.error))
    return template


def read_input():
    if not request.get_data():
        return {}
    data = request.get_json(force=True, silent=True)
    if data is None:
        raise ApiError(400, "the request body must be JSON")
    return data


def render(template, data):
    values, errors = validate(template.fields, data)
    if errors:
        raise ApiError(400, "invalid input", errors)
    try:
        return template.render(values)
    except Exception as exc:
        log.exception("template %s failed to render", template.name)
        raise ApiError(500, "template %s failed: %s: %s" % (template.name, type(exc).__name__, exc))


def check(printer_service, template, markup):
    try:
        return printer_service.check(markup, template.directory)
    except (ValueError, PrinterError) as exc:
        raise ApiError(422, "this cannot be printed: %s" % exc)
