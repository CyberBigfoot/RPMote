"""YAML keystrokes and saves preserve indentation, scalar text, and encodings."""

import codecs
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from app.remote_files import TextDocument
from app.yaml_editor import YamlTextEdit
from app.yaml_support import validate_yaml

APP = QApplication.instance() or QApplication([])


@pytest.fixture
def editor():
    widget = YamlTextEdit(yaml=True)
    try:
        yield widget
    finally:
        widget.deleteLater()


def set_text(editor, text):
    editor.configure_indentation(text)
    editor.setPlainText(text)
    editor.moveCursor(QTextCursor.MoveOperation.End)


@pytest.mark.parametrize("text,expected", [
    ("services:", "services:\n  "),
    ("services: # comment", "services: # comment\n  "),
    ("  image: app", "  image: app\n  "),
    ("- name: app", "- name: app\n  "),
    ("- settings:", "- settings:\n    "),
    ("- 'a: b'", "- 'a: b'\n"),
    ('key: "a # b:"', 'key: "a # b:"\n'),
    ("script: |-", "script: |-\n  "),
    ("script: >2-", "script: >2-\n  "),
    ("script: |\n  example:", "script: |\n  example:\n  "),
    ("script: |\n  echo http://host\n  echo '#:'", "script: |\n  echo http://host\n  echo '#:'\n  "),
])
def test_enter_maintains_yaml_structure_without_changing_scalar_contents(editor, text, expected):
    set_text(editor, text)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText() == expected
    editor.undo()
    assert editor.toPlainText() == text


def test_tab_inserts_spaces_and_backspace_removes_one_indent_step(editor):
    set_text(editor, "services:\n")
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    assert editor.toPlainText() == "services:\n  "
    QTest.keyClicks(editor, "web:")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText().endswith("web:\n    ")
    QTest.keyClick(editor, Qt.Key.Key_Backspace)
    assert editor.toPlainText().endswith("web:\n  ")
    assert "\t" not in editor.toPlainText()


def test_tab_and_shift_tab_indent_selected_lines_with_single_undo(editor):
    original = "name: app\nport: 80\nother: true"
    set_text(editor, original)
    cursor = editor.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(original.index("other"), QTextCursor.MoveMode.KeepAnchor)
    editor.setTextCursor(cursor)
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    assert editor.toPlainText() == "  name: app\n  port: 80\nother: true"
    QTest.keyClick(editor, Qt.Key.Key_Backtab)
    assert editor.toPlainText() == original
    editor.undo()
    assert editor.toPlainText() == "  name: app\n  port: 80\nother: true"
    editor.undo()
    assert editor.toPlainText() == original


def test_existing_four_space_style_is_detected(editor):
    set_text(editor, "services:\n    web:\n        port: 80")
    assert editor.indent_width == 4
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText().endswith("\n        ")


def test_block_scalar_body_does_not_determine_the_indent_unit(editor):
    set_text(editor, "script: |\n   content with three spaces\nservices:\n    web: app")
    assert editor.indent_width == 4


@pytest.mark.parametrize("raw", [
    b"# compose\r\nservices:\r\n  web:\r\n    image: 'app:latest'\r\n",
    codecs.BOM_UTF16_LE + "name: demo\r\nitems:\r\n  - one\r\n".encode("utf-16-le"),
])
def test_yaml_text_round_trip_and_edit_preserve_whitespace_comments_and_encoding(editor, raw):
    doc = TextDocument.decode(raw)
    set_text(editor, doc.text)
    assert doc.encode(editor.toPlainText()) == raw
    editor.setPlainText(editor.toPlainText().replace("demo", "demo2").replace("app:latest", "app:next"))
    expected = raw.replace(b"app:latest", b"app:next") if not doc.bom else codecs.BOM_UTF16_LE + "name: demo2\r\nitems:\r\n  - one\r\n".encode("utf-16-le")
    assert doc.encode(editor.toPlainText()) == expected


def test_paste_preserves_yaml_block_scalar_indentation(editor):
    source = "script: |\n  echo one\n    echo indented\n\nservices:\n  web: app\n"
    mime = QApplication.clipboard()
    mime.setText(source)
    editor.paste()
    assert editor.toPlainText() == source


def test_other_file_types_keep_literal_tabs():
    editor = YamlTextEdit(yaml=False)
    try:
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        assert editor.toPlainText() == "\t"
    finally:
        editor.deleteLater()


def paste(editor, text):
    QApplication.clipboard().setText(text)
    editor.paste()


def test_paste_rebases_a_fragment_into_a_nested_yaml_mapping(editor):
    original = "services:\n  web:\n    "
    set_text(editor, original)
    paste(editor, 'image: "app:latest"\nports:\n  - "80:80"\n')
    assert editor.toPlainText() == 'services:\n  web:\n    image: "app:latest"\n    ports:\n      - "80:80"\n    '
    validate_yaml(editor.toPlainText())
    editor.undo()
    assert editor.toPlainText() == original


def test_paste_removes_the_copied_margin_and_repairs_indent_tabs(editor):
    set_text(editor, "services:\n  ")
    paste(editor, "\tweb:\n\t\timage: app\n")
    assert editor.toPlainText() == "services:\n  web:\n    image: app\n  "
    validate_yaml(editor.toPlainText())


def test_pasting_an_entire_file_detects_its_indent_style_for_further_typing(editor):
    paste(editor, "\ufeffservices:\r\n    web:\r\n        image: app\r\n")
    assert editor.indent_width == 4
    assert editor.toPlainText() == "services:\n    web:\n        image: app\n"
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    assert editor.toPlainText().endswith("\n    ")


def test_paste_inside_a_block_scalar_keeps_literal_tabs_and_relative_content_spaces(editor):
    set_text(editor, "script: |\n  ")
    paste(editor, "echo one\n\tcontent tab\n  extra spaces")
    assert editor.toPlainText() == "script: |\n  echo one\n  \tcontent tab\n    extra spaces"
    validate_yaml(editor.toPlainText())


def test_inline_paste_preserves_tabs_in_a_quoted_value(editor):
    set_text(editor, 'name: "')
    paste(editor, 'a\tb"')
    assert editor.toPlainText() == 'name: "a\tb"'


def test_format_replacement_is_one_undoable_edit(editor):
    original = "services:\n   web: app\n"
    set_text(editor, original)
    editor.replace_text("services:\n  web: app\n")
    assert editor.toPlainText() == "services:\n  web: app\n"
    editor.undo()
    assert editor.toPlainText() == original

