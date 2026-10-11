"""Command line entry point: run the server, or only check the configuration."""

import argparse
import logging
import sys

import waitress

from .config import ConfigError, load_config
from .printing import PrinterService
from .templates import TemplateStore, load_templates
from .web import create_app

DEFAULT_CONFIG = "/etc/receipt-printer-server/config.yaml"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="default: %(default)s")
    parser.add_argument(
        "--check",
        action="store_true",
        help="load the configuration and all templates, report problems and exit",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        config = load_config(args.config)
    except (OSError, ConfigError) as exc:
        sys.exit("configuration error: %s" % exc)
    templates = load_templates(config["templates"], config["template_settings"])

    if args.check:
        broken = [name for name, template in templates.items() if template.error]
        for name, template in templates.items():
            print("%-24s %s" % (name, template.error or "ok"))
        sys.exit(1 if broken else 0)

    printer = config["printer"]
    service = PrinterService(printer["device"], printer["paper"], printer["dry_run"])
    store = TemplateStore(config["templates"], config["template_settings"])
    app = create_app(store, service)
    waitress.serve(app, host=config["host"], port=config["port"])


if __name__ == "__main__":
    main()
