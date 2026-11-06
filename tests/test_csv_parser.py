from app.csv_parser import validate_csv


def check(text, max_rows=20):
    return validate_csv(text.encode() if isinstance(text, str) else text, max_rows)


def test_valid_csv_with_optional_phone():
    report = check("name,address,phone\nA,1 Main St,555\nB,2 Main St,\n")
    assert report.valid
    assert [r.name for r in report.rows] == ["A", "B"]
    assert report.rows[0].phone == "555"
    assert report.rows[1].phone is None


def test_phone_column_can_be_omitted_and_columns_reordered():
    report = check("Address,Name\n1 Main St,A\n")
    assert report.valid
    assert report.rows[0].name == "A"
    assert report.rows[0].address == "1 Main St"


def test_handles_utf8_bom_quotes_and_blank_lines():
    report = check('﻿name,address\n"St. Mary\'s, North","12 High St, Pune"\n\n')
    assert report.valid
    assert report.rows[0].name == "St. Mary's, North"
    assert report.rows[0].address == "12 High St, Pune"


def test_missing_required_fields_reports_every_row():
    report = check("name,address\n,1 Main St\nB,\nC,3 Main St\n")
    assert not report.valid
    assert [(e.row, e.field) for e in report.errors] == [(1, "name"), (2, "address")]
    assert report.rows == []


def test_rejects_more_than_max_rows():
    rows = "\n".join(f"H{i},Addr {i}" for i in range(21))
    report = check("name,address\n" + rows)
    assert not report.valid
    assert report.total_rows == 21
    assert "maximum is 20" in report.errors[0].message


def test_rejects_missing_and_unknown_columns():
    assert "Missing required column" in check("name,phone\nA,1\n").errors[0].message
    assert "Unknown column" in check("name,address,email\nA,B,c@d\n").errors[0].message


def test_rejects_empty_file_and_header_only():
    assert check("").errors[0].message == "CSV file is empty"
    assert check("name,address\n").errors[0].message == "CSV file has no data rows"


def test_rejects_non_utf8():
    report = check("name,address\nCaf\xe9,1 St\n".encode("latin-1"))
    assert report.errors[0].message == "CSV file must be UTF-8 encoded"


def test_rejects_rows_with_extra_columns():
    report = check("name,address\nA,B,C\n")
    assert not report.valid
    assert "Expected 2 columns" in report.errors[0].message
