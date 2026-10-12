"""Tests that need no printer: everything renders into a BufferTransport."""

import base64
import io
import os
import textwrap
from decimal import Decimal

import pytest
from PIL import Image

from epson_tm_t88v import BufferTransport, CharacterSet, Justification, Printer, commands

from receipt_printer_server.charsets import printable, split_by_table
from receipt_printer_server.config import ConfigError, check_config
from receipt_printer_server.fields import DefinitionError, check_fields, validate
from receipt_printer_server.markup import MarkupError, layout_row, print_markup
from receipt_printer_server.printing import PrinterService
from receipt_printer_server.templates import TemplateStore, load_helpers, load_templates
from receipt_printer_server.web import create_app

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(REPO, "templates")


def render(markup, base_dir=EXAMPLES):
    buffer = BufferTransport()
    print_markup(Printer(buffer, initialize=False), markup, base_dir)
    return buffer.data


def png_data_url(width=8, height=8):
    out = io.BytesIO()
    Image.new("L", (width, height), 0).save(out, "PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


# -- character tables ---------------------------------------------------------------


def test_plain_text_stays_in_the_default_table():
    assert split_by_table("hello", CharacterSet.PC437_USA_STANDARD_EUROPE) == [
        (CharacterSet.PC437_USA_STANDARD_EUROPE, "hello")
    ]


def test_euro_switches_table_and_stays_there():
    runs = split_by_table("ab €1 é", CharacterSet.PC437_USA_STANDARD_EUROPE)
    assert runs == [
        (CharacterSet.PC437_USA_STANDARD_EUROPE, "ab "),
        (CharacterSet.PC858_EURO, "€1 é"),
    ]


def test_decomposed_accents_are_combined():
    runs = split_by_table("é", CharacterSet.PC437_USA_STANDARD_EUROPE)
    assert runs == [(CharacterSet.PC437_USA_STANDARD_EUROPE, "é")]


def test_markup_sends_table_switch_and_encoded_text():
    data = render("€")
    assert commands.character_set(CharacterSet.PC858_EURO) + "€".encode("cp858") in data


def test_unprintable_character_is_an_error():
    with pytest.raises(ValueError, match="U\\+1F600"):
        render("smile 😀")


def test_printable_filter_replaces_only_unprintable_characters():
    assert printable("€ 😀 ç") == "€ ? ç"


# -- markup ---------------------------------------------------------------------------


def test_lines_holding_only_tags_print_nothing():
    data = render("<center>\nTitle\n</center>\n<b>bold</b>\nend\n")
    assert data.count(b"\n") == 3


def test_blank_line_after_a_block_element_is_kept():
    data = render('<feed lines="1"/>\n\ntext\n')
    assert b"\ntext\n" in data


def test_alignment_and_upside_down_restore_at_a_line_start():
    data = render('<center>Title</center>\n<style upside-down="true">flip</style>\nnext\n')
    assert b"Title\n" + commands.justify(0) in data
    assert b"flip\n" + commands.upside_down(False) + b"next\n" in data
    assert data.count(b"\n") == 3


def test_vertical_wraps_first_then_reverses_each_line():
    # Font A on its side is 24 dots across, so 21 characters fit on 512 dots.
    data = render("<vertical>Rotated text should read from the top downwards</vertical>")
    assert commands.rotate_90(True) in data
    assert b"dluohs txet detatoR\n" + b"pot eht morf daer\n" + b"sdrawnwod\n" in data


def test_style_shorthand_and_restore():
    data = render("<b>x</b>")
    assert data.index(commands.emphasize(True)) < data.index(b"x") < data.index(
        commands.emphasize(False)
    )


def test_unknown_element_and_attribute_are_reported():
    with pytest.raises(MarkupError, match="<blink>: unknown element"):
        render("<blink>x</blink>")
    with pytest.raises(MarkupError, match="unknown attribute 'size'"):
        render('<qr size="3">x</qr>')


def test_errors_name_the_enclosing_elements():
    with pytest.raises(MarkupError, match="<center>: <barcode>: attribute type"):
        render('<center><barcode type="nonsense">1</barcode></center>')


def test_invalid_markup_shows_the_line():
    with pytest.raises(MarkupError, match="never closed"):
        render("fine\n<b>unclosed\n")
    with pytest.raises(MarkupError, match="<b>oops</u>"):
        render("fine\n<b>oops</u>\nmore\n")


def test_barcode_data_is_checked_by_the_driver():
    with pytest.raises(MarkupError, match="EAN13"):
        render('<barcode type="ean13">12</barcode>')


def test_qr_keeps_newlines_in_its_data():
    data = render("<qr>a\nb</qr>")
    assert b"a\nb" in data


def test_rule_fills_the_line_in_the_current_width():
    assert b"-" * 42 + b"\n" in render("<rule/>")
    assert b"=" * 21 + b"\n" in render('<wide><rule char="="/></wide>')


def test_row_layout_right_aligns_last_cell_and_wraps():
    assert layout_row(["Espresso", "2,50"], None, 20) == "Espresso        2,50\n"
    assert layout_row(["a long description", "1,00"], None, 16) == (
        "a long      1,00\n" "description\n"
    )
    assert layout_row(["a", "b", "c"], [5, 5, 5], 15) == "a    b        c\n"
    with pytest.raises(MarkupError):
        layout_row(["a", "b"], [10, 10], 15)


def test_row_cell_align_keeps_padding_to_the_cell_edge():
    center, left = Justification.CENTER, Justification.LEFT
    assert layout_row(["Title"], None, 11, [center]) == "   Title   \n"
    assert layout_row(["a", "b"], [4, 4], 10, [None, left]) == "a   b   \n"
    assert layout_row(["a", "b"], [4, 4], 10, [left, None]) == "a      b\n"
    assert b" " * 18 + b"Title" + b" " * 19 + b"\n" in render(
        '<row><cell align="center">Title</cell></row>'
    )
    with pytest.raises(MarkupError):
        render('<row><cell align="middle">Title</cell></row>')


def test_image_from_template_dir_and_data_url():
    assert render('<image src="shopping-receipt/logo.png" width="100"/>')
    assert render('<image src="%s"/>' % png_data_url())


def test_image_outside_template_dir_is_refused():
    with pytest.raises(MarkupError, match="outside the template directory"):
        render('<image src="../../etc/passwd"/>', os.path.join(EXAMPLES, "log-line"))


# -- fields ---------------------------------------------------------------------------


FIELDS = check_fields(
    [
        {"name": "title", "required": True, "max_length": 5},
        {"name": "count", "type": "integer", "min": 1, "default": 1},
        {"name": "price", "type": "number"},
        {"name": "urgent", "type": "boolean"},
        {"name": "size", "type": "choice", "options": ["S", "L"]},
        {"name": "lines", "type": "list", "fields": [{"name": "text", "required": True}]},
    ]
)


def test_valid_input_is_converted():
    values, errors = validate(
        FIELDS,
        {"title": "hi", "count": "3", "price": "0.10", "urgent": "on", "lines": [{"text": "a"}]},
    )
    assert errors == {}
    assert values == {
        "title": "hi",
        "count": 3,
        "price": Decimal("0.10"),
        "urgent": True,
        "size": None,
        "lines": [{"text": "a"}],
    }


def test_invalid_input_reports_every_problem_by_path():
    _, errors = validate(
        FIELDS,
        {"title": "too long", "count": 0, "size": "M", "lines": [{}], "extra": 1},
    )
    assert set(errors) == {"title", "count", "size", "lines.0.text", "extra"}


def test_single_line_strings_refuse_newlines():
    _, errors = validate(FIELDS, {"title": "a\nb"})
    assert errors == {"title": "must be a single line"}


def test_bad_definitions_are_rejected():
    with pytest.raises(DefinitionError, match="unknown type"):
        check_fields([{"name": "x", "type": "date"}])
    with pytest.raises(DefinitionError, match="unknown keys"):
        check_fields([{"name": "x", "type": "integer", "max_length": 3}])
    with pytest.raises(DefinitionError, match="bad default"):
        check_fields([{"name": "x", "type": "integer", "default": "many"}])
    with pytest.raises(DefinitionError, match="reserved"):
        check_fields([{"name": "settings"}], reserved={"settings"})


def test_image_field_accepts_data_url_and_refuses_junk():
    values, errors = validate(check_fields([{"name": "photo", "type": "image"}]), {"photo": png_data_url()})
    assert errors == {} and values["photo"].startswith("data:image/png;base64,")
    _, errors = validate(check_fields([{"name": "photo", "type": "image"}]), {"photo": "bm90IGFuIGltYWdl"})
    assert "photo" in errors


# -- templates, config and API --------------------------------------------------------


def write_template(root, name, yaml_text, jinja_text, helpers=None):
    directory = root / name
    directory.mkdir()
    (directory / "template.yaml").write_text(textwrap.dedent(yaml_text))
    (directory / "template.j2").write_text(textwrap.dedent(jinja_text))
    if helpers:
        (directory / "helpers.py").write_text(textwrap.dedent(helpers))


def test_broken_template_is_listed_with_its_error(tmp_path):
    write_template(tmp_path, "good", "title: Good\n", "hello\n")
    write_template(tmp_path, "bad", "fields: 3\n", "hello\n")
    write_template(tmp_path, "syntax", "title: x\n", "{% if %}\n")
    templates = load_templates(str(tmp_path), {})
    assert templates["good"].error is None
    assert "must be a list" in templates["bad"].error
    assert "TemplateSyntaxError" in templates["syntax"].error


def test_helpers_and_settings_reach_the_template(tmp_path):
    write_template(
        tmp_path,
        "t",
        "settings:\n  greeting: hello\n",
        "{{ shout(settings.greeting) }} {{ settings.name | shout }}\n",
        helpers="""
            import os

            def shout(text):
                return text.upper()

            def _private():
                pass
        """,
    )
    templates = load_templates(str(tmp_path), {"t": {"name": "world"}})
    assert templates["t"].render({}) == "HELLO WORLD\n"
    assert sorted(load_helpers("t", str(tmp_path / "t"))) == ["shout"]


def test_changed_templates_are_reloaded_on_the_next_request(tmp_path):
    write_template(tmp_path, "t", "title: One\n", "{{ greet() }}\n", helpers="def greet():\n    return 'hi'\n")
    store = TemplateStore(str(tmp_path), {})
    first = store.current()["t"]
    assert store.current()["t"] is first  # nothing changed, nothing reloaded
    assert first.render({}) == "hi\n"

    (tmp_path / "t" / "template.yaml").write_text("title: Two, longer\n")
    (tmp_path / "t" / "helpers.py").write_text("def greet():\n    return 'hello again'\n")
    reloaded = store.current()["t"]
    assert reloaded.title == "Two, longer"
    assert reloaded.render({}) == "hello again\n"

    write_template(tmp_path, "new", "title: New\n", "x\n")
    assert sorted(store.current()) == ["new", "t"]
    for path in (tmp_path / "new").iterdir():
        path.unlink()
    (tmp_path / "new").rmdir()
    assert sorted(store.current()) == ["t"]


def test_config_requires_existing_templates_dir(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        check_config({"templates": "missing"}, str(tmp_path))
    with pytest.raises(ConfigError, match="unknown settings"):
        check_config({"templates": ".", "prot": 1}, str(tmp_path))
    config = check_config({"templates": "."}, str(tmp_path))
    assert config["printer"]["paper"] == "80mm"


@pytest.fixture
def client():
    settings = {"shopping-receipt": {"payee": "J. Jansen", "iban": "NL91ABNA0417164300"}}
    app = create_app(TemplateStore(EXAMPLES, settings), PrinterService(dry_run=True))
    return app.test_client()


def test_api_lists_example_templates(client):
    names = [template["name"] for template in client.get("/api/templates").get_json()]
    assert names == ["feature-test", "log-line", "shopping-receipt"]


def test_feature_test_renders_every_element(client):
    response = client.post("/api/templates/feature-test/print", json={"photo": png_data_url()})
    assert response.status_code == 200, response.get_json()


def test_api_prints_log_line(client):
    response = client.post("/api/templates/log-line/print", json={"message": "21.3 °C"})
    assert response.status_code == 200


def test_api_status_codes(client):
    assert client.post("/api/templates/nope/print", json={}).status_code == 404
    assert client.post("/api/templates/log-line/print", json={}).status_code == 400
    assert client.post("/api/templates/log-line/print", data="{not json").status_code == 400
    assert client.post("/api/templates/log-line/print", json={"message": "😀"}).status_code == 422


def test_shopping_receipt_totals_and_payment_qr(client):
    response = client.post(
        "/api/templates/shopping-receipt/preview",
        json={
            "items": [
                {"amount": 2, "description": "Espresso", "subtotal": "0.10"},
                {"amount": 1, "description": "Croissant", "subtotal": "0.20"},
            ],
            "reference": "Order 7",
        },
    )
    assert response.status_code == 200
    markup = response.get_json()["markup"]
    assert "€ 0,30" in markup
    assert "NL91ABNA0417164300\nEUR0.30\n\n\nOrder 7</qr>" in markup


def test_epc_qr_rejects_a_mistyped_iban():
    epc_qr = load_helpers("shopping-receipt", os.path.join(EXAMPLES, "shopping-receipt"))["epc_qr"]
    with pytest.raises(ValueError, match="not a valid IBAN"):
        epc_qr("J. Jansen", "NL91ABNA0417164301", Decimal("1"))
