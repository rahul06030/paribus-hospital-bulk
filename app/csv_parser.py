"""CSV parsing and validation.

File-level problems (not UTF-8, wrong headers, too many rows) and row-level
problems (missing name or address) are both collected into a ValidationReport,
so a client sees every problem in one response instead of fixing them one at a time.
"""

import csv
import io
from typing import Dict, List, Tuple

from app.models import HospitalRow, RowError, ValidationReport

REQUIRED_COLUMNS = ("name", "address")
OPTIONAL_COLUMNS = ("phone",)
KNOWN_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

MAX_FIELD_LENGTH = 255


def validate_csv(content: bytes, max_rows: int) -> ValidationReport:
    def fail(message: str, field: str = None) -> ValidationReport:
        return ValidationReport(
            valid=False,
            total_rows=0,
            max_rows=max_rows,
            errors=[RowError(row=0, field=field, message=message)],
        )

    if not content or not content.strip():
        return fail("CSV file is empty")

    try:
        text = content.decode("utf-8-sig")  # tolerate the BOM Excel adds
    except UnicodeDecodeError:
        return fail("CSV file must be UTF-8 encoded")

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        return fail("CSV file has no header row")

    columns = [h.strip().lower() for h in header]
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        return fail(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Expected header: name,address,phone (phone optional)",
            field="header",
        )
    unknown = [c for c in columns if c not in KNOWN_COLUMNS]
    if unknown:
        return fail(f"Unknown column(s): {', '.join(unknown)}", field="header")
    duplicates = sorted({c for c in columns if columns.count(c) > 1})
    if duplicates:
        return fail(f"Duplicate column(s): {', '.join(duplicates)}", field="header")

    index = {name: columns.index(name) for name in columns}
    rows: List[HospitalRow] = []
    errors: List[RowError] = []
    seen: Dict[Tuple[str, str, str], int] = {}
    data_row = 0

    try:
        for record in reader:
            if not any(cell.strip() for cell in record):
                continue  # skip blank lines
            data_row += 1
            if data_row > max_rows:
                continue  # keep counting so the error states the real total

            if len(record) > len(columns):
                errors.append(
                    RowError(row=data_row, message=f"Expected {len(columns)} columns, got {len(record)}")
                )
                continue

            def cell(col: str) -> str:
                i = index.get(col)
                return record[i].strip() if i is not None and i < len(record) else ""

            values = {col: cell(col) for col in KNOWN_COLUMNS}
            row_errors = [
                RowError(row=data_row, field=col, message=f"{col} is required")
                for col in REQUIRED_COLUMNS
                if not values[col]
            ]
            row_errors += [
                RowError(row=data_row, field=col, message=f"{col} exceeds {MAX_FIELD_LENGTH} characters")
                for col in KNOWN_COLUMNS
                if len(values[col]) > MAX_FIELD_LENGTH
            ]
            if row_errors:
                errors.extend(row_errors)
                continue

            # Identical rows would be indistinguishable upstream, which breaks
            # matching a saved record back to its row after an ambiguous failure.
            key = (values["name"].lower(), values["address"].lower(), values["phone"].lower())
            if key in seen:
                errors.append(RowError(row=data_row, message=f"Duplicate of row {seen[key]}"))
                continue
            seen[key] = data_row

            rows.append(
                HospitalRow(
                    row=data_row,
                    name=values["name"],
                    address=values["address"],
                    phone=values["phone"] or None,
                )
            )
    except csv.Error as exc:
        errors.append(RowError(row=data_row + 1, message=f"Malformed CSV: {exc}"))

    if data_row == 0:
        errors.append(RowError(row=0, message="CSV file has no data rows"))
    elif data_row > max_rows:
        errors.insert(
            0, RowError(row=0, message=f"CSV has {data_row} hospitals; the maximum is {max_rows}")
        )

    return ValidationReport(
        valid=not errors,
        total_rows=data_row,
        max_rows=max_rows,
        errors=errors,
        rows=rows if not errors else [],
    )
