"""Print markup: the small XML language a template renders to.

A template produces text with tags in it, for example::

    <center><big>SHOP</big></center>
    <row><cell>2 x Espresso</cell><cell>5.00</cell></row>
    <qr>https://example.com</qr>
    <cut/>

Text is printed as written, newlines included. A newline directly after a tag
is dropped when the printer is already at the start of a line, so a line that
contains nothing but tags prints nothing. See README.md for every element.

Each element maps onto one method of the epson_tm_t88v driver. Attribute
names are the driver's argument names, with dashes instead of underscores.
"""

import base64
import io
import os
import textwrap
import xml.etree.ElementTree as ElementTree

from PIL import Image, ImageOps

from epson_tm_t88v import (
    Barcode,
    CharacterSet,
    Color,
    CutMode,
    DrawerPin,
    Font,
    HriPosition,
    ImageCommand,
    Justification,
    MaxiCodeMode,
    Pdf417Type,
    PrintDirection,
    PrinterError,
    QrErrorCorrection,
    TransportError,
    Underline,
    from_image,
)

from .charsets import split_by_table


class MarkupError(ValueError):
    pass


# -- attribute converters -----------------------------------------------------


def text(value):
    return value


def integer(value):
    try:
        return int(value)
    except ValueError:
        raise MarkupError("expected a whole number, got %r" % value)


def integer_list(value):
    return [integer(part) for part in value.split(",")]


def boolean(value):
    if value.lower() in ("1", "true", "yes", "on"):
        return True
    if value.lower() in ("0", "false", "no", "off"):
        return False
    raise MarkupError("expected true or false, got %r" % value)


def choice(enum_class, prefix=""):
    """Convert e.g. "double" to Underline.DOUBLE, or "4" to MaxiCodeMode.MODE_4."""

    def convert(value):
        name = prefix + value.strip().upper().replace("-", "_")
        if name in enum_class.__members__:
            return enum_class[name]
        options = [
            member[len(prefix):].lower().replace("_", "-")
            for member in enum_class.__members__
        ]
        raise MarkupError("expected one of %s, got %r" % (", ".join(options), value))

    return convert


# -- what each element accepts --------------------------------------------------
#
# Attribute name in the markup -> (driver argument name, converter).

ALIGN = ("align", choice(Justification))

STYLE_ATTRIBUTES = {
    "font": ("font", choice(Font)),
    "bold": ("bold", boolean),
    "underline": ("underline", choice(Underline)),
    "width": ("width", integer),
    "height": ("height", integer),
    "invert": ("reverse", boolean),
    "upside-down": ("upside_down", boolean),
    "rotate": ("rotate_90", boolean),
    "double-strike": ("double_strike", boolean),
    "smoothing": ("smoothing", boolean),
    "align": ("justify", choice(Justification)),
    "color": ("color", choice(Color)),
}

# Tags that are a fixed <style>.
SHORTHANDS = {
    "b": {"bold": True},
    "u": {"underline": Underline.SINGLE},
    "invert": {"reverse": True},
    "small": {"font": Font.B},
    "wide": {"width": 2},
    "tall": {"height": 2},
    "big": {"width": 2, "height": 2},
    "left": {"justify": Justification.LEFT},
    "center": {"justify": Justification.CENTER},
    "right": {"justify": Justification.RIGHT},
    "red": {"color": Color.RED},
}

# Tags whose text is the data of a barcode or 2-D symbol. Each tag is also the
# name of the driver method that prints it.
SYMBOLS = {
    "barcode": {
        "type": ("symbology", choice(Barcode)),
        "height": ("height", integer),
        "module-width": ("module_width", integer),
        "hri": ("hri", choice(HriPosition)),
        "hri-font": ("hri_font", choice(Font)),
        "align": ALIGN,
    },
    "qr": {
        "module": ("module", integer),
        "error-correction": ("error_correction", choice(QrErrorCorrection)),
        "align": ALIGN,
    },
    "pdf417": {
        "columns": ("columns", integer),
        "rows": ("rows", integer),
        "module-width": ("module_width", integer),
        "row-height": ("row_height", integer),
        "error-correction": ("error_correction", integer),
        "kind": ("kind", choice(Pdf417Type)),
        "align": ALIGN,
    },
    "maxicode": {
        "mode": ("mode", choice(MaxiCodeMode, prefix="MODE_")),
        "align": ALIGN,
    },
}

IMAGE_ATTRIBUTES = {
    "src": ("src", text),
    "width": ("width", integer),
    "dither": ("dither", boolean),
    "threshold": ("threshold", integer),
    "invert": ("invert", boolean),
    "align": ALIGN,
    "scale-x": ("scale_x", integer),
    "scale-y": ("scale_y", integer),
    "command": ("command", choice(ImageCommand)),
    "color": ("ink", choice(Color)),
}

ROW_ATTRIBUTES = {"widths": ("widths", integer_list)}
CELL_ATTRIBUTES = {"align": ALIGN}
RULE_ATTRIBUTES = {"char": ("char", text)}
FEED_ATTRIBUTES = {"lines": ("lines", integer), "dots": ("dots", integer)}
CUT_ATTRIBUTES = {"mode": ("mode", choice(CutMode)), "feed": ("feed", integer)}
DRAWER_ATTRIBUTES = {
    "pin": ("pin", choice(DrawerPin, prefix="PIN_")),
    "on-ms": ("on_ms", integer),
}
SPACING_ATTRIBUTES = {"line": ("line", integer), "char": ("char", integer)}
MARGINS_ATTRIBUTES = {"left": ("left", integer), "width": ("width", integer)}
PAGE_ATTRIBUTES = {
    "x": ("x", integer),
    "y": ("y", integer),
    "width": ("width", integer),
    "height": ("height", integer),
    "direction": ("direction", choice(PrintDirection)),
}
AT_ATTRIBUTES = {"x": ("x", integer), "y": ("y", integer)}


def attributes(element, allowed, required=()):
    """Convert an element's attributes into driver keyword arguments."""
    result = {}
    for name, value in element.attrib.items():
        if name not in allowed:
            raise MarkupError(
                "unknown attribute %r (allowed: %s)"
                % (name, ", ".join(allowed) or "none")
            )
        argument, convert = allowed[name]
        try:
            result[argument] = convert(value)
        except MarkupError as exc:
            raise MarkupError("attribute %s: %s" % (name, exc))
    for name in required:
        if name not in element.attrib:
            raise MarkupError("attribute %r is required" % name)
    return result


def no_children(element):
    if len(element):
        raise MarkupError("cannot contain other elements")


def empty(element):
    no_children(element)
    if (element.text or "").strip():
        raise MarkupError("cannot contain text")


# -- rendering --------------------------------------------------------------------


def parse(markup):
    try:
        return ElementTree.fromstring("<receipt>" + markup + "</receipt>")
    except ElementTree.ParseError as exc:
        line_number = exc.position[0]
        lines = markup.split("\n")
        if line_number < len(lines):
            where = lines[line_number - 1]
        else:
            # Only the closing </receipt> we added is left: something is unclosed.
            where = "(at the end: an element was opened but never closed)"
        raise MarkupError(
            "the template did not produce valid markup: %s\n    %s" % (exc, where)
        )


def print_markup(printer, markup, base_dir):
    """Send rendered markup to a driver Printer. Images are read from base_dir."""
    Renderer(printer, base_dir).run(parse(markup))


class Renderer:
    def __init__(self, printer, base_dir):
        self.printer = printer
        self.base_dir = base_dir
        self.table = CharacterSet.PC437_USA_STANDARD_EUROPE
        self.at_line_start = True
        self.line_spacing = None  # None is the printer's default
        self.char_spacing = 0
        self.margin_left = 0
        self.area_width = printer.profile.print_width_dots
        self.handlers = {
            "style": self.style,
            "image": self.image,
            "row": self.row,
            "rule": self.rule,
            "feed": self.feed,
            "cut": self.cut,
            "drawer": self.drawer,
            "spacing": self.spacing,
            "margins": self.margins,
            "page": self.page,
            "at": self.at,
            "vertical": self.vertical,
        }

    def run(self, root):
        # The printer keeps its table between jobs; start every job from a known one.
        self.printer.set_charset(self.table)
        self.contents(root)
        self.printer.ensure_line_start()

    def contents(self, element):
        self.text(element.text)
        for child in element:
            self.element(child)
            self.text(child.tail)

    def text(self, value):
        if not value:
            return
        if self.at_line_start and value.startswith("\n"):
            value = value[1:]
        self.write(value)

    def write(self, value):
        """Print text, switching character tables wherever needed."""
        for table, piece in split_by_table(value, self.table):
            if table != self.table:
                self.printer.set_charset(table)
                self.table = table
            self.printer.text(piece)
        if value:
            self.at_line_start = value.endswith("\n")

    def element(self, element):
        tag = element.tag
        try:
            if tag in SHORTHANDS:
                attributes(element, {})
                self.styled(element, SHORTHANDS[tag])
            elif tag in SYMBOLS:
                self.symbol(element)
            elif tag in self.handlers:
                self.handlers[tag](element)
            else:
                known = sorted(list(SHORTHANDS) + list(SYMBOLS) + list(self.handlers))
                raise MarkupError("unknown element (known: %s)" % ", ".join(known))
        except TransportError:
            raise
        except (ValueError, PrinterError) as exc:
            raise MarkupError("<%s>: %s" % (tag, exc)) from exc

    def chars_per_line(self):
        style = self.printer.current_style
        char_dots = self.printer.profile.font_width[style.font] + self.char_spacing
        return self.area_width // (char_dots * style.width)

    def new_line(self):
        if not self.at_line_start:
            self.write("\n")

    # -- elements ---------------------------------------------------------------

    def style(self, element):
        self.styled(element, attributes(element, STYLE_ATTRIBUTES))

    def styled(self, element, style):
        # Alignment and upside-down only change at the start of a line, so
        # they get lines of their own: <center>Title</center> is a line.
        whole_lines = "justify" in style or "upside_down" in style
        if whole_lines:
            self.new_line()
        with self.printer.style(**style):
            self.contents(element)
            if whole_lines:
                self.new_line()

    def symbol(self, element):
        required = ["type"] if element.tag == "barcode" else []
        options = attributes(element, SYMBOLS[element.tag], required)
        no_children(element)
        data = (element.text or "").strip()
        if not data:
            raise MarkupError("needs data between its tags")
        method = getattr(self.printer, element.tag)
        method(data=data, **options)
        self.at_line_start = True

    def image(self, element):
        empty(element)
        options = attributes(element, IMAGE_ATTRIBUTES, required=["src"])
        picture = self.load_image(options.pop("src"))
        width = options.pop("width", None)
        if width:
            height = max(1, round(picture.height * width / picture.width))
            picture = picture.resize((width, height), Image.LANCZOS)
        raster = from_image(
            picture,
            max_width=self.area_width,
            dither=options.pop("dither", True),
            threshold=options.pop("threshold", 128),
            invert=options.pop("invert", False),
        )
        self.printer.image(raster, **options)
        self.at_line_start = True

    def load_image(self, src):
        """Open an image from a data: URL or a file in the template directory."""
        if src.startswith("data:"):
            header, _, payload = src.partition(",")
            if not header.endswith(";base64"):
                raise MarkupError("only base64 data: URLs are supported")
            source = io.BytesIO(base64.b64decode(payload))
        else:
            base = os.path.realpath(self.base_dir)
            path = os.path.realpath(os.path.join(base, src))
            if not path.startswith(base + os.sep):
                raise MarkupError("%r is outside the template directory" % src)
            if not os.path.isfile(path):
                raise MarkupError("%r not found in the template directory" % src)
            source = path
        try:
            picture = Image.open(source)
            # Phone photos are stored sideways with a note saying which way is up.
            return ImageOps.exif_transpose(picture)
        except OSError as exc:
            raise MarkupError("cannot read image: %s" % exc)

    def row(self, element):
        widths = attributes(element, ROW_ATTRIBUTES).get("widths")
        if (element.text or "").strip():
            raise MarkupError("text must be inside <cell>")
        cells = []
        aligns = []
        for child in element:
            if child.tag != "cell":
                raise MarkupError("may only contain <cell> elements")
            if (child.tail or "").strip():
                raise MarkupError("text must be inside <cell>")
            aligns.append(attributes(child, CELL_ATTRIBUTES).get("align"))
            no_children(child)
            cells.append((child.text or "").strip())
        self.new_line()
        self.write(layout_row(cells, widths, self.chars_per_line(), aligns))

    def rule(self, element):
        empty(element)
        char = attributes(element, RULE_ATTRIBUTES).get("char", "-")
        if len(char) != 1:
            raise MarkupError("char must be a single character")
        self.new_line()
        self.write(char * self.chars_per_line() + "\n")

    def feed(self, element):
        empty(element)
        options = attributes(element, FEED_ATTRIBUTES)
        if "dots" in options:
            self.printer.feed_dots(options["dots"])
        else:
            self.printer.feed(options.get("lines", 1))
        self.at_line_start = True

    def cut(self, element):
        empty(element)
        self.printer.cut(**attributes(element, CUT_ATTRIBUTES))
        self.at_line_start = True

    def drawer(self, element):
        empty(element)
        self.printer.open_drawer(**attributes(element, DRAWER_ATTRIBUTES))

    def spacing(self, element):
        options = attributes(element, SPACING_ATTRIBUTES)
        previous = (self.line_spacing, self.char_spacing)
        self.set_spacing(
            options.get("line", self.line_spacing), options.get("char", self.char_spacing)
        )
        self.contents(element)
        self.set_spacing(*previous)

    def set_spacing(self, line, char):
        self.printer.set_line_spacing(line)
        self.printer.set_char_spacing(char)
        self.line_spacing = line
        self.char_spacing = char

    def margins(self, element):
        options = attributes(element, MARGINS_ATTRIBUTES)
        previous = (self.margin_left, self.area_width)
        left = options.get("left", 0)
        width = options.get("width", self.printer.profile.print_width_dots - left)
        self.set_margins(left, width)
        self.contents(element)
        self.set_margins(*previous)

    def set_margins(self, left, width):
        # Margins apply from the start of a line, so the driver ends the current one.
        self.printer.set_margins(left, width)
        self.margin_left = left
        self.area_width = width
        self.at_line_start = True

    def page(self, element):
        options = attributes(element, PAGE_ATTRIBUTES, required=["height"])
        options.setdefault("x", 0)
        options.setdefault("y", 0)
        options.setdefault("width", self.printer.profile.print_width_dots)
        previous = self.area_width
        self.area_width = options["width"]
        with self.printer.page(**options):
            self.contents(element)
        self.area_width = previous
        self.at_line_start = True

    def vertical(self, element):
        """Text that reads top to bottom once the receipt is turned a quarter
        turn counter-clockwise, its lines becoming columns from left to right.

        The printer's rotation turns each character clockwise but still lays
        the line out left to right, which then reads bottom to top. So the
        text is wrapped here, where the line width is known, and each line is
        sent reversed.
        """
        attributes(element, {})
        no_children(element)
        style = self.printer.current_style
        # A rotated character lies on its side: its height runs across the paper.
        char_dots = self.printer.profile.font_height[style.font] * style.height
        width = self.area_width // (char_dots + self.char_spacing)
        lines = []
        for paragraph in (element.text or "").strip().split("\n"):
            lines.extend(textwrap.wrap(paragraph, width) or [""])
        self.new_line()
        with self.printer.style(rotate_90=True):
            for line in lines:
                self.write(line[::-1] + "\n")

    def at(self, element):
        empty(element)
        options = attributes(element, AT_ATTRIBUTES, required=["x"])
        self.printer.at(options["x"], options.get("y"))


JUSTIFY = {
    Justification.LEFT: str.ljust,
    Justification.CENTER: str.center,
    Justification.RIGHT: str.rjust,
}


def layout_row(cells, widths, line_width, aligns=None):
    """Lay cells out as columns; the last one is right-aligned.

    Without widths the first cell gets whatever the others leave. Text too
    long for its column wraps onto extra lines instead of being cut off.
    aligns holds a Justification or None per cell; None keeps the default.
    Trailing spaces are dropped, except the padding of a cell given an
    alignment, so an inverted centred cell stays black to its edge.
    """
    if not cells:
        raise MarkupError("needs at least one <cell>")
    if widths is None:
        rest = sum(len(cell) + 1 for cell in cells[1:])
        widths = [line_width - rest] + [len(cell) + 1 for cell in cells[1:]]
        if widths[0] < 1:
            raise MarkupError("the cells do not fit on one %d-character line" % line_width)
    if len(widths) != len(cells):
        raise MarkupError("widths needs one number per cell")
    if sum(widths) > line_width:
        raise MarkupError(
            "widths add up to %d, but a line holds %d characters" % (sum(widths), line_width)
        )
    if aligns is None:
        aligns = [None] * len(cells)
    if len(aligns) != len(cells):
        raise MarkupError("aligns needs one value per cell")
    # Everything up to the end of the last explicitly aligned cell is kept.
    keep = max(
        (sum(widths[: index + 1]) for index, align in enumerate(aligns) if align is not None),
        default=0,
    )
    aligns = [
        align if align is not None
        else Justification.RIGHT if index == len(cells) - 1
        else Justification.LEFT
        for index, align in enumerate(aligns)
    ]
    columns = [textwrap.wrap(cell, width) or [""] for cell, width in zip(cells, widths)]
    lines = []
    for number in range(max(len(column) for column in columns)):
        parts = []
        for column, width, align in zip(columns, widths, aligns):
            part = column[number] if number < len(column) else ""
            parts.append(JUSTIFY[align](part, width))
        line = "".join(parts)
        lines.append(line[:keep] + line[keep:].rstrip() + "\n")
    return "".join(lines)
