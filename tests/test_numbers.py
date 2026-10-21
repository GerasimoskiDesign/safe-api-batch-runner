"""Input parsing: what reaches the API must be clean, unique and valid."""

from pathlib import Path

import pytest

from batchrunner.numbers import normalise, parse_file, parse_lines, validation_error


@pytest.mark.parametrize(
    "raw",
    [
        "+99970123456",
        "+999 70 123 456",
        "+999-7012-3456",
        "00999 7012 3456",
        "99970123456",
        "(+999) 70.123.456",
        "  +99970123456  ",
    ],
)
def test_notations_normalise_to_one_canonical_form(raw):
    assert normalise(raw) == "+99970123456"
    assert validation_error(normalise(raw)) is None


@pytest.mark.parametrize(
    ("raw", "reason_fragment"),
    [
        ("+999 12 34", "wrong length"),
        ("+999701234567", "wrong length"),
        ("+4470123456", "wrong prefix"),
        ("call back tomorrow", "characters"),
        ("+99970123abc", "characters"),
        ("+999*0123456", "characters"),
    ],
)
def test_invalid_values_get_a_reason(raw, reason_fragment):
    assert reason_fragment in validation_error(normalise(raw))


def test_parse_skips_blanks_and_comments_and_reports_invalid_with_line_numbers():
    parsed = parse_lines(
        [
            "# header comment",
            "",
            "+99970000001",
            "   ",
            "not a number",
            "+99970000002  # inline comment",
            "+999 1",
        ]
    )
    assert parsed.numbers == ["+99970000001", "+99970000002"]
    assert parsed.ignored_lines == 3
    assert [(i.line_no, i.raw) for i in parsed.invalid] == [(5, "not a number"), (7, "+999 1")]
    assert parsed.total_lines == 7


def test_duplicates_across_notations_are_removed_keeping_first_order():
    parsed = parse_lines(["+99970000002", "+99970000001", "00999 7000 0002", "99970000001"])
    assert parsed.numbers == ["+99970000002", "+99970000001"]
    assert [(d.line_no, d.first_line_no) for d in parsed.duplicates] == [(3, 1), (4, 2)]


def test_comment_only_and_empty_inputs_yield_no_numbers():
    assert parse_lines([]).numbers == []
    assert parse_lines(["# nothing here", "", "#+99970000001"]).numbers == []


def test_parse_file_handles_utf8_bom_from_windows_editors(tmp_path: Path):
    path = tmp_path / "bom.txt"
    path.write_bytes("﻿+99970000001\r\n+99970000002\r\n".encode())
    assert parse_file(path).numbers == ["+99970000001", "+99970000002"]


def test_generated_sample_parses_to_expected_counts():
    from batchrunner.generate import generate_lines

    parsed = parse_lines(generate_lines(count=45, duplicates=4, seed=42))
    assert len(parsed.numbers) == 45
    assert len(parsed.duplicates) == 4
    assert len(parsed.invalid) == 3
    assert all(n.startswith("+999") for n in parsed.numbers)


def test_generator_is_deterministic_per_seed():
    from batchrunner.generate import generate_lines

    assert generate_lines(seed=7) == generate_lines(seed=7)
    assert generate_lines(seed=7) != generate_lines(seed=8)
