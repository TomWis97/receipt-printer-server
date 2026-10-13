# receipt-printer-server

A small REST API and web UI for printing on an Epson TM-T88V. Each print job
is a **template**: a directory with a Jinja2 file, a list of the input fields it
takes, and optionally some Python and images. Send the field values in a POST
request, or fill them in on the web page, and the template is validated,
rendered and printed.

It uses the [`epson-tm-t88v`](../lib-epson-tm-t88v) driver for everything that
touches the printer.

* **Python, Flask, Jinja2**, served by waitress.
* **No authentication.** It is meant for a trusted home network.
* **Nothing reaches paper until the whole job has rendered.** Each job is first
  rendered into memory, so a bad barcode, an unknown tag or a character the
  printer cannot print gives an error instead of half a receipt.
* **Character tables are picked automatically.** `€`, `é`, `ß`, `ş`, `Ж`… just
  work. The printer switches table mid-line where needed.

---

## Trying it locally

```bash
python3 -m venv .venv
.venv/bin/pip install -e ../lib-epson-tm-t88v -e '.[test]'
cp config.example.yaml config.yaml        # set dry_run: true to spare paper
.venv/bin/receipt-printer-server --config config.yaml
```

Then browse to <http://localhost:8080>. Run the tests with
`.venv/bin/python -m pytest tests`. They need no printer.

`--check` loads the configuration and every template, reports what is wrong
and exits. Deployment uses it to refuse a broken configuration.

---

## Configuration

One YAML file. `/etc/receipt-printer-server/config.yaml` unless `--config`
says otherwise. Every setting:

```yaml
host: 0.0.0.0
port: 8080
templates: templates          # directory of templates; relative to this file

printer:
  device: /dev/usb/lp0        # leave out to use the first /dev/usb/lp* found
  paper: 80mm                 # or 58mm
  dry_run: false              # true: render and check, but do not print

template_settings:            # handed to templates as `settings`
  shopping-receipt:
    payee: J. Jansen
    iban: NL91 ABNA 0417 1643 00
```

---

## The API

| | |
|---|---|
| `GET /api/templates` | every template, with its fields |
| `GET /api/templates/<name>` | one template |
| `POST /api/templates/<name>/print` | validate, render and print |
| `POST /api/templates/<name>/preview` | validate and render; returns the markup, prints nothing |
| `GET /api/status` | the printer's status, as the printer reports it |

The body of `print` and `preview` is a JSON object with the field values. A
template without fields takes an empty body.

```bash
curl -X POST http://receipt-printer:8080/api/templates/log-line/print \
     -H 'Content-Type: application/json' \
     -d '{"message": "Living room 21.3 °C"}'
```

Errors come back as JSON, `{"error": "...", "fields": {"items.0.amount": "must be at least 1"}}`:

| Status | Meaning |
|---|---|
| 400 | the input does not match the template's fields; `fields` says which |
| 404 | no such template |
| 422 | the template rendered, but the result cannot be printed (e.g. an emoji) |
| 500 | the template is broken, or its Python code raised an error |
| 503 | the printer cannot print right now: cover open, out of paper, offline… |

Jobs are printed one at a time. A request returns once the printer reports
that it has finished the job.

### From Home Assistant

```yaml
rest_command:
  receipt_log:
    url: http://receipt-printer:8080/api/templates/log-line/print
    method: POST
    content_type: application/json
    payload: '{"message": {{ message | to_json }}}'
```

```yaml
action: rest_command.receipt_log
data:
  message: "Living room {{ states('sensor.living_room_temperature') }} °C"
```

Numbers and booleans may be sent as strings. That is what Home Assistant
templates produce, and the server converts them.

---

## Writing a template

Each subdirectory of the templates directory is a template, named after the
directory:

```
templates/shopping-receipt/
├── template.yaml    title, description, fields, default settings
├── template.j2      Jinja2 that renders to print markup
├── helpers.py       optional: Python functions the template can call
└── logo.png         optional: images, used as <image src="logo.png"/>
```

The examples `log-line` and `shopping-receipt` are the quickest way in;
`feature-test` uses every element of the markup and doubles as a reference.
Changes take effect on the next request: the server notices when any file in a
template directory changes (`template.yaml`, `template.j2`, `helpers.py`, images)
and reloads that template, and new or removed directories show up the same
way. No restart needed. A
template that fails to load is still listed, with its error, in both the API
and the web UI. The other templates keep working.

### template.yaml

```yaml
title: Shopping receipt
description: Shown in the web UI.

fields:
  - name: shop                # the variable name in template.j2
    label: Shop name          # optional; defaults to the name
    help: Shown under the input.
    default: Receipt
    max_length: 21
  - name: items
    type: list
    min_items: 1
    fields:                   # the fields of each item
      - name: amount
        type: integer
        min: 1
        default: 1
      - name: subtotal
        type: number
        required: true

settings:                     # defaults for `settings`; the config overrides them
  iban: ""
```

| Type | Becomes | Options |
|---|---|---|
| `string` (default) | `str` | `multiline`, `min_length`, `max_length`, `pattern` |
| `integer` | `int` | `min`, `max` |
| `number` | `Decimal`, so money adds up exactly | `min`, `max` |
| `boolean` | `bool` | |
| `choice` | `str` | `options` (a list) |
| `list` | list of dicts | `fields`, `min_items`, `max_items` |
| `image` | `data:` URL, for `<image src="...">` | |

Every field also takes `label`, `help`, `required` (default false) and
`default`. A missing optional field gets its `default`. Without one it gets an
empty string, list or `false`, or `None` for the other types. Unknown fields in
a request are rejected, which catches typos. A `string` without
`multiline: true` refuses line breaks.

### template.j2

Ordinary Jinja2. These are available inside it:

| | |
|---|---|
| every field, by name | `{{ message }}`, `{% for item in items %}` |
| `settings` | from `template.yaml`, overridden by `template_settings` in the config |
| `now()` | the current local time, a `datetime` |
| `printable` filter | replaces characters the printer cannot print with `?` |
| every public function in `helpers.py` | as a function *and* as a filter: `money(x)` or `x \| money` |

Lines holding only `{% ... %}` vanish. Values are escaped, so a `<` in user
input prints as `<` and is not read as a tag. A misspelt variable is an error,
not a blank. To return markup from a helper on purpose, wrap it in
`markupsafe.Markup`.

Helpers are plain Python and can do anything, such as fetching a code from
another API:

```python
# helpers.py
import requests            # installed with the server

def door_code(url):
    return requests.get(url, timeout=5).json()["code"]
```

```jinja
Today's code: <big>{{ door_code(settings.code_url) }}</big>
```

Helpers run on every print *and* every preview.

### Print markup

`template.j2` renders to text with tags in it. Text prints as written,
including its line breaks. A line that holds nothing but tags prints nothing,
so this:

```
<center>
<big>SHOP</big>
</center>
<rule/>
```

prints two lines, not four. The markup is XML, so every tag is closed (`<cut/>`,
`<b>…</b>`), and an error names the line it happened on.

**Text style.** These nest, and each restores the previous style when it closes.

| Tag | Effect |
|---|---|
| `<b>` | bold |
| `<u>` | underline |
| `<invert>` | white on black |
| `<small>` | font B (56 columns instead of 42) |
| `<wide>`, `<tall>`, `<big>` | double width, height, or both |
| `<left>`, `<center>`, `<right>` | alignment; applies to whole lines |
| `<red>` | second colour, on two-colour paper |
| `<style …>` | anything: `font="a\|b"` `bold` `underline="none\|single\|double"` `width="1-8"` `height="1-8"` `invert` `upside-down` `rotate` `double-strike` `smoothing` `align="left\|center\|right"` `color="black\|red"` |

Yes/no attributes take `true`/`false`: `<style upside-down="true">`.

`rotate` turns each character a quarter turn clockwise, but the line still
runs left to right, so it reads from bottom to top once the receipt is turned.
For text that reads normally, use `<vertical>` (below). Adding `upside-down`
does not help: it turns the characters the other way *and* reverses their
order, so it reads bottom to top as well.

Positions in page mode are in dots: Font A characters are 12 wide, Font B 9,
and the paper holds 512. To centre text of n Font A characters, put it at
`x = (512 - 12 × n) / 2`.

**Layout.**

| Tag | |
|---|---|
| `<row><cell>Espresso</cell><cell>2,50</cell></row>` | columns across the line. The last cell is right-aligned, the first takes the remaining width. `widths="20,10,12"` sets them. Text too long for its column wraps. `<cell align="center">` (or `left`, `right`) aligns one cell and keeps its padding, so `<invert><row><cell align="center">Title</cell></row></invert>` is a centred bar across the paper. |
| `<rule/>`, `<rule char="="/>` | a line across the paper in the current font and width |
| `<feed lines="2"/>`, `<feed dots="30"/>` | blank space |
| `<cut/>`, `<cut mode="full" feed="5"/>` | feed past the cutter and cut. On this unit partial and full cuts are the same. |
| `<spacing line="40" char="2">…</spacing>` | line and character spacing, in dots. Line spacing runs baseline to baseline: Font A is 24 dots tall and the default is 30, so 6 dots of white. Up to 127. |
| `<margins left="40" width="400">…</margins>` | narrower print area, in dots |
| `<page height="200">…<at x="10" y="50"/>…</page>` | page mode: place text anywhere in an area, print it at once. `x`, `y`, `width`, `direction="left-to-right\|bottom-to-top\|…"`. The `y` of `<at>` is where the *bottom* of the text goes, so use at least the font height (24 for Font A). |
| `<at x="100"/>` | jump to a horizontal position |
| `<vertical>…</vertical>` | text to read with the receipt turned a quarter counter-clockwise: top to bottom, in columns from left to right. Plain text only; style it from outside, e.g. `<b><vertical>…</vertical></b>`. Font A fits 21 characters per column. |
| `<drawer/>` | kick a cash drawer: `pin="2\|5"` `on-ms="100"` |

**Codes.** The data goes between the tags and may contain line breaks.

| Tag | Attributes |
|---|---|
| `<qr>…</qr>` | `module="1-16"` (dot size, default 4), `error-correction="l\|m\|q\|h"`, `align` |
| `<barcode type="ean13">…</barcode>` | `type`: `upc-a upc-e ean13 ean8 code39 itf codabar code93 code128 gs1-128 gs1-databar-omnidirectional gs1-databar-truncated gs1-databar-limited gs1-databar-expanded`; `height`, `module-width`, `hri="none\|above\|below\|both"`, `hri-font="a\|b"`, `align` |
| `<pdf417>…</pdf417>` | `columns`, `rows`, `module-width`, `row-height`, `error-correction="0-8"`, `kind="standard\|truncated"`, `align` |
| `<maxicode>…</maxicode>` | `mode="2-6"`, `align` |

**Images.**

```
<image src="logo.png" width="200"/>
<image src="{{ photo }}"/>             (an `image` field)
```

`src` is a file in the template's directory or a `data:` URL. `width` scales
the image, in dots (the paper holds 512). Images wider than the paper are
scaled down. More attributes: `dither="false"` (better for line art),
`threshold="0-255"`, `invert`, `align`, `scale-x`/`scale-y` (1 or 2),
`command="graphics|raster"`, `color="black|red"`.

Attribute names are those of the driver's methods, with dashes. The driver's
README describes what each one does on paper.

---

## Deployment

The Ansible role in `ansible/roles/receipt_printer_server` installs the
server as the systemd unit `receipt-printer-server`. It is written for
AlmaLinux 10 on a Raspberry Pi, and uses only distribution-neutral modules
plus firewalld. See `ansible/playbook.example.yaml`. It needs the
`ansible.posix` collection (`ansible-galaxy collection install ansible.posix`)
for the firewalld task.

It clones **both** this repository and the driver's from git, installs them
into a virtualenv at `/opt/receipt-printer-server/venv`, writes
`/etc/receipt-printer-server/config.yaml` and runs the service as the user
`receipt-printer`, a member of `lp`. It opens the port in firewalld.

| Variable | Default |
|---|---|
| `receipt_printer_server_repo` | `https://github.com/TomWis97/receipt-printer-server.git` |
| `receipt_printer_server_driver_repo` | `https://github.com/TomWis97/lib-epson-tm-t88v.git` |
| `receipt_printer_server_version`, `…_driver_version` | `main` |
| `receipt_printer_server_templates_src` | empty: use the example templates of the checkout. Set it to a directory on the controller to deploy your own templates instead; see below. |
| `receipt_printer_server_templates_dir` | `/etc/receipt-printer-server/templates` when `…_templates_src` is set, otherwise the `templates/` directory of the checkout |
| `receipt_printer_server_template_settings` | `{}` |
| `receipt_printer_server_host`, `…_port` | `0.0.0.0`, `80`. Below 1024 the unit grants the service `CAP_NET_BIND_SERVICE` and nothing else. |
| `receipt_printer_server_printer_device`, `…_paper`, `…_dry_run` | `/dev/usb/lp0`, `80mm`, `false` |
| `receipt_printer_server_user`, `…_install_dir`, `…_config_dir` | `receipt-printer`, `/opt/receipt-printer-server`, `/etc/receipt-printer-server` |
| `receipt_printer_server_packages` | `[git-core, python3, python3-packaging, rsync]`, the EL names; `python3-packaging` is for Ansible's `pip` module. Debian would need `git` and `python3-venv`. |
| `receipt_printer_server_firewalld` | `true`: open the port in firewalld |
| `receipt_printer_server_firewalld_zone` | unset: the default zone |

A new commit in either repository reinstalls the package and restarts the
service. The configuration is written only if `--check` accepts it, and after
a new commit of this repository a handler runs `--check` before the restart,
so a broken template stops the play.

**Your own templates.** Templates often hold things that do not belong in a
public repository. Keep them anywhere on the controller and point
`receipt_printer_server_templates_src` at the directory, for example per host
next to `host_vars`:

```
host_vars/printer-pi.yaml        receipt_printer_server_templates_src: "{{ playbook_dir }}/host_files/{{ inventory_hostname }}/receipt-templates"
host_files/printer-pi/receipt-templates/
└── shopping-receipt/            template.yaml, template.j2, logo.png, ...
```

Not *inside* `host_vars/printer-pi/`: Ansible loads every `.yaml`, `.yml`,
`.json` and extensionless file below it as variables, so each `template.yaml`
would turn into host variables named `title`, `fields` and `settings`.

The role mirrors the directory to `/etc/receipt-printer-server/templates` with
`ansible.posix.synchronize`, so rsync is needed on the controller as well as on
the target. A template removed on the controller is removed on the target. The
files belong to root, readable by the service's group but not writable: a
template's `helpers.py` is code the service runs. The server picks up a changed
template on the next request without a restart; a handler runs `--check`
afterwards, so a broken one still stops the play. Switching back to the example
templates leaves the old copy in `/etc/receipt-printer-server/templates`.

The role uses `command` once, for that handler: no module runs a program's own
validation, and the `validate:` option only applies when the configuration
file itself changes.

**Not yet run on AlmaLinux.** The role passes `ansible-lint` and a syntax
check, but has not been run against a real host. Things to look at on the
first run: that `/dev/usb/lp0` exists and belongs to the `lp` group (the
`usblp` kernel module must be available in the Pi's kernel), and that SELinux
lets the service open it. The service is started from `/opt` by systemd, so
it should run unconfined; `ausearch -m avc -ts recent` shows a denial if not.

**CUPS.** If CUPS is installed on the Pi it may claim the printer, and then
`/dev/usb/lp0` does not exist. Remove CUPS, or keep it away from this printer.

---

## When the printer stops

The printer answers `503` with the printer's own explanation. This unit
sometimes lights its paper LED and goes offline with a full roll in it.
**Opening and closing the cover clears it.** If a job died half-way, the server
repairs the command stream before the next one (the driver's `resync()`), so
there is nothing to reset by hand.

## Licence

MIT.
