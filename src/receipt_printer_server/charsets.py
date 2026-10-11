"""Pick the printer's character table automatically, character by character.

The printer has a dozen 8-bit character tables and can switch between them
mid-line. Text is written in the current table for as long as that works; a
character the current table lacks switches to the first table in
``PREFERRED_TABLES`` that has it.
"""

import unicodedata

from epson_tm_t88v import CharacterSet
from epson_tm_t88v.printer import CODEC_FOR_TABLE

# Tried in this order when the current table cannot print a character.
# PC437 is the printer's default; PC858 is PC437's layout with the euro sign.
PREFERRED_TABLES = [
    CharacterSet.PC437_USA_STANDARD_EUROPE,
    CharacterSet.PC858_EURO,
    CharacterSet.WPC1252,
    CharacterSet.PC850_MULTILINGUAL,
    CharacterSet.PC852_LATIN2,
    CharacterSet.PC866_CYRILLIC2,
    CharacterSet.WPC1254_TURKISH,
    CharacterSet.ISO8859_15,
    CharacterSet.PC860_PORTUGUESE,
    CharacterSet.PC863_CANADIAN_FRENCH,
    CharacterSet.PC865_NORDIC,
]


class UnprintableCharacter(ValueError):
    pass


def normalize(text):
    """Combine "e" + accent into "é", which is what the tables contain."""
    return unicodedata.normalize("NFC", text)


def can_encode(char, table):
    try:
        char.encode(CODEC_FOR_TABLE[table])
        return True
    except UnicodeEncodeError:
        return False


def table_for(char):
    """Return the first preferred table that can print ``char``, or None."""
    for table in PREFERRED_TABLES:
        if can_encode(char, table):
            return table
    return None


def split_by_table(text, current):
    """Split text into (table, piece) runs, starting from table ``current``.

    Raises UnprintableCharacter for a character no table contains.
    """
    runs = []
    piece = ""
    for char in normalize(text):
        if not can_encode(char, current):
            new_table = table_for(char)
            if new_table is None:
                raise UnprintableCharacter(
                    "%r (U+%04X) cannot be printed: none of the printer's "
                    "character tables contain it. The 'printable' filter "
                    "replaces such characters." % (char, ord(char))
                )
            if piece:
                runs.append((current, piece))
            current = new_table
            piece = ""
        piece += char
    if piece:
        runs.append((current, piece))
    return runs


def printable(text, replacement="?"):
    """Jinja2 filter: replace every character the printer cannot print."""
    return "".join(
        char if table_for(char) is not None else replacement
        for char in normalize(str(text))
    )
