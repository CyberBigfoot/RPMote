"""Formatting must retain YAML meaning, comments, tags, aliases and document versions."""

from unittest import mock

import pytest
from ruamel.yaml import YAML

from app.remote_files import MAX_EDIT_BYTES
from app.yaml_support import YamlError, format_yaml, normalize_indentation_tabs, prepare_yaml, validate_yaml


def read(text):
    return YAML(typ="rt", pure=True).load(text)


def test_format_normalizes_inconsistent_indents_and_retains_quotes_and_comments():
    source = '# deployment\nservices:\n   web:\n       image: "app:latest" # chosen version\n       ports: ["80:80", "443:443"]\n'
    formatted = format_yaml(source, 2)
    assert '\n  web:\n    image: "app:latest"' in formatted
    assert '# deployment' in formatted and '# chosen version' in formatted
    assert '["80:80", "443:443"]' in formatted
    assert read(formatted) == read(source)
    assert format_yaml(formatted, 2) == formatted


def test_format_keeps_literal_and_folded_scalar_values_with_extra_indentation_and_tabs():
    source = 'script: |-\n  echo one\n    echo extra spaces\n  \tkeep content tab\nmessage: >+\n  first line\n  next line\n\n'
    formatted = format_yaml(source, 4)
    assert read(formatted) == read(source)
    assert "|-" in formatted and ">+" in formatted
    assert "\tkeep content tab" in formatted


def test_anchors_merges_custom_tags_and_key_order_are_preserved():
    source = 'defaults: &defaults\n  image: "app:latest"\nweb:\n  <<: *defaults\n  token: !vault "keep secret as text"\n'
    formatted = format_yaml(source, 4)
    assert "&defaults" in formatted and "*defaults" in formatted
    assert '!vault "keep secret as text"' in formatted
    assert list(read(formatted)) == ["defaults", "web"]
    assert read(formatted)["web"]["image"] == "app:latest"


def test_named_anchors_are_kept_even_when_there_is_no_alias_reference():
    source = 'defaults: &keep {image: "app:latest"}\n'
    assert "&keep" in format_yaml(source)


def test_multi_document_versions_and_explicit_markers_are_preserved():
    source = '%YAML 1.1\n---\nenabled: yes\n...\n%YAML 1.2\n---\nenabled: yes\n'
    formatted = format_yaml(source)
    assert "%YAML 1.1" in formatted and "%YAML 1.2" in formatted
    assert "...\n" in formatted
    before = list(YAML().load_all(source))
    after = list(YAML().load_all(formatted))
    assert after == before == [{"enabled": True}, {"enabled": "yes"}]


@pytest.mark.parametrize("source", ["", "# only comments\n", "# heading\n---\n# inside\n", "---\n# empty\n---\na: 2\n"])
def test_empty_documents_and_comment_only_files_are_kept_exactly(source):
    assert format_yaml(source) == source


def test_comments_before_document_start_are_kept():
    source = '# deployment header\n\n---\nservices:\n   web: app\n'
    formatted = format_yaml(source)
    assert formatted.startswith("# deployment header\n\n---\n")
    assert read(formatted) == read(source)


@pytest.mark.parametrize("source", [
    "name: first\nname: second\n",
    "services:\n  web:\n    image: app\n   bad: indent\n",
    "items: [one, two\n",
])
def test_invalid_yaml_including_duplicate_keys_reports_a_location(source):
    with pytest.raises(YamlError, match=r"YAML error at line \d+, column \d+"):
        validate_yaml(source)


def test_structural_tabs_are_repaired_while_scalar_and_quoted_tabs_are_preserved():
    source = 'services:\n\tweb:\n\t\tscript: |\n\t\t  \tpayload\n\t\tport: 80\nvalue: "a\tb"\n'
    prepared = prepare_yaml(source)
    assert prepared == 'services:\n  web:\n    script: |\n      \tpayload\n    port: 80\nvalue: "a\tb"\n'
    assert read(prepared)["services"]["web"]["script"] == "\tpayload\n"


def test_sequence_mapping_scalar_body_ends_before_the_next_sibling_key():
    source = 'items:\n  - script: |\n      a\n    \tkey: value\n'
    normalized = normalize_indentation_tabs(source)
    assert '\n      key: value' in normalized
    assert '\t' not in normalized


def test_multiline_quoted_scalar_contents_are_not_normalized():
    source = 'value: "first\n  \tcontinued\n  last"\nnext:\n\tkey: value\n'
    normalized = normalize_indentation_tabs(source)
    assert '  \tcontinued' in normalized
    assert normalized.endswith('next:\n  key: value\n')
    validate_yaml(normalized)


def test_python_tags_remain_data_and_never_execute():
    source = 'payload: !!python/object/apply:os.system ["echo do not execute"]\n'
    with mock.patch("os.system") as execute:
        validate_yaml(source)
        formatted = format_yaml(source)
    execute.assert_not_called()
    assert "!!python/object/apply:os.system" in formatted


def test_unsupported_recursive_alias_formatting_keeps_the_draft_instead_of_losing_the_reference():
    source = 'value: &value {self: *value}\n'
    with pytest.raises(YamlError, match="draft was kept unchanged"):
        format_yaml(source)


def test_oversized_yaml_and_excessive_nesting_fail_with_a_readable_error():
    with pytest.raises(YamlError, match="2 MiB"):
        format_yaml("#" + "x" * MAX_EDIT_BYTES)
    with pytest.raises(YamlError):
        validate_yaml("value: " + "[" * 120 + "0" + "]" * 120)

