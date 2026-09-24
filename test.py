from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from PIL import Image


DEFAULT_OUTPUT_DIR = Path("textract_results")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "document"


def natural_page_number(path: Path) -> int:
    match = re.search(r"(\d+)$", path.stem)
    return int(match.group(1)) if match else 0


def render_pdf_pages(pdf_path: Path, render_dir: Path, dpi: int) -> list[Path]:
    """Render any PDF into one Textract-compatible JPEG per page."""
    prefix = render_dir / "page"
    command = [
        "pdftoppm",
        "-jpeg",
        "-r",
        str(dpi),
        "-jpegopt",
        "quality=92",
        str(pdf_path),
        str(prefix),
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "pdftoppm is required but was not found in PATH. Install Poppler first."
        ) from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "unknown render error"
        raise RuntimeError(f"Could not render {pdf_path.name}: {message}") from exc

    pages = sorted(render_dir.glob("page-*.jpg"), key=natural_page_number)
    if not pages:
        raise RuntimeError(f"No pages were rendered from {pdf_path.name}")
    return pages


def recover_missed_checkbox_marks(
    blocks: list[dict[str, Any]], image_path: Path
) -> None:
    """Mark a checkbox selected when its centre visibly contains ink.

    Textract can miss handwritten crosses that touch or slightly extend beyond
    a checkbox border. Checking only the centre avoids treating unrelated
    ticks elsewhere on the page as a selected checkbox.
    """
    with Image.open(image_path) as image:
        grayscale = image.convert("L")
        width, height = grayscale.size

        for block in blocks:
            if (
                block.get("BlockType") != "SELECTION_ELEMENT"
                or block.get("SelectionStatus") != "NOT_SELECTED"
            ):
                continue

            box = block.get("Geometry", {}).get("BoundingBox", {})
            left = float(box.get("Left", 0))
            top = float(box.get("Top", 0))
            box_width = float(box.get("Width", 0))
            box_height = float(box.get("Height", 0))
            if box_width <= 0 or box_height <= 0:
                continue

            # Exclude the printed square border; a real cross passes through
            # this central area, whereas an empty box is mostly white here.
            x0 = max(0, int((left + box_width * 0.25) * width))
            y0 = max(0, int((top + box_height * 0.25) * height))
            x1 = min(width, int((left + box_width * 0.75) * width))
            y1 = min(height, int((top + box_height * 0.75) * height))
            if x1 <= x0 or y1 <= y0:
                continue

            pixels = list(grayscale.crop((x0, y0, x1, y1)).getdata())
            dark_pixel_ratio = sum(pixel < 130 for pixel in pixels) / len(pixels)
            if dark_pixel_ratio >= 0.07:
                block["SelectionStatus"] = "SELECTED"
                block["SelectionDetection"] = "local_checkbox_ink"


def analyze_pages(textract: Any, pdf_path: Path, dpi: int) -> dict[str, Any]:
    """Analyze rendered pages individually and combine them as one response."""
    all_blocks: list[dict[str, Any]] = []
    page_api_metadata: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="textract_pages_") as temp_dir:
        page_images = render_pdf_pages(pdf_path, Path(temp_dir), dpi)

        for page_number, image_path in enumerate(page_images, start=1):
            print(f"  Textract page {page_number}/{len(page_images)}", flush=True)
            response = textract.analyze_document(
                Document={"Bytes": image_path.read_bytes()},
                FeatureTypes=["FORMS", "TABLES"],
            )
            recover_missed_checkbox_marks(response.get("Blocks", []), image_path)

            for block in response.get("Blocks", []):
                block["Page"] = page_number
                all_blocks.append(block)

            metadata = response.get("ResponseMetadata", {})
            page_api_metadata.append(
                {
                    "page": page_number,
                    "request_id": metadata.get("RequestId"),
                    "http_status_code": metadata.get("HTTPStatusCode"),
                }
            )

    return {
        "DocumentMetadata": {"Pages": len(page_api_metadata)},
        "Blocks": all_blocks,
        "PageApiMetadata": page_api_metadata,
    }


def normalize_ocr_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:BIG|BCI)\s*:", "BIC:", text, flags=re.IGNORECASE)
    return text


def clean_extracted_value(label: str, value: str | None) -> str | None:
    """Remove label text that Textract sometimes attaches to a field value."""
    if not value:
        return None

    value = normalize_ocr_text(value)
    if re.search(r"e-?mail", label, re.IGNORECASE):
        email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value)
        if email:
            return email.group(0)
    return value


def extract_clean_data(response: dict[str, Any]) -> dict[str, Any]:
    blocks = response.get("Blocks", [])
    block_map = {block["Id"]: block for block in blocks}

    def child_text(block: dict[str, Any]) -> str:
        parts: list[str] = []
        for relationship in block.get("Relationships", []):
            if relationship.get("Type") != "CHILD":
                continue
            for child_id in relationship.get("Ids", []):
                child = block_map.get(child_id, {})
                if child.get("BlockType") == "WORD":
                    parts.append(child.get("Text", ""))
                elif (
                    child.get("BlockType") == "SELECTION_ELEMENT"
                    and child.get("SelectionStatus") == "SELECTED"
                ):
                    parts.append("[X]")
        return normalize_ocr_text(" ".join(part for part in parts if part))

    lines = [
        {
            "page": block.get("Page", 1),
            "text": normalize_ocr_text(block["Text"]),
            "confidence": round(block.get("Confidence", 0), 2),
        }
        for block in blocks
        if block.get("BlockType") == "LINE" and block.get("Text")
    ]

    fields: list[dict[str, Any]] = []
    for block in blocks:
        is_key = (
            block.get("BlockType") == "KEY_VALUE_SET"
            and "KEY" in block.get("EntityTypes", [])
        )
        if not is_key:
            continue

        key_text = child_text(block)
        value_parts: list[str] = []
        has_selection_element = False
        for relationship in block.get("Relationships", []):
            if relationship.get("Type") != "VALUE":
                continue
            for value_id in relationship.get("Ids", []):
                if value_id not in block_map:
                    continue
                value_block = block_map[value_id]
                for child_relationship in value_block.get("Relationships", []):
                    if child_relationship.get("Type") != "CHILD":
                        continue
                    for child_id in child_relationship.get("Ids", []):
                        child = block_map.get(child_id, {})
                        if child.get("BlockType") == "SELECTION_ELEMENT":
                            has_selection_element = True
                value_parts.append(child_text(value_block))

        raw_value_text = normalize_ocr_text(" ".join(part for part in value_parts if part))
        attached_label = re.search(
            r"\s+(rechnungsversand\s*:?)\s*$",
            raw_value_text,
            re.IGNORECASE,
        )
        if attached_label and re.search(r"e-?mail", key_text, re.IGNORECASE):
            key_text = f"{key_text.rstrip(':').strip()} {attached_label.group(1)}"
            raw_value_text = raw_value_text[: attached_label.start()].strip()

        value_text = clean_extracted_value(key_text, raw_value_text)
        if key_text or value_text:
            fields.append(
                {
                    "page": block.get("Page", 1),
                    "key": key_text,
                    "value": value_text or None,
                    "selected": "[X]" in (value_text or ""),
                    "checkbox": has_selection_element,
                    "confidence": round(block.get("Confidence", 0), 2),
                }
            )

    tables: list[dict[str, Any]] = []
    table_number_by_page: defaultdict[int, int] = defaultdict(int)
    for table_block in (
        block for block in blocks if block.get("BlockType") == "TABLE"
    ):
        page = table_block.get("Page", 1)
        table_number_by_page[page] += 1
        cells: list[dict[str, Any]] = []

        for relationship in table_block.get("Relationships", []):
            if relationship.get("Type") != "CHILD":
                continue
            for cell_id in relationship.get("Ids", []):
                cell = block_map.get(cell_id, {})
                if cell.get("BlockType") == "CELL":
                    cells.append(cell)

        row_count = max((cell.get("RowIndex", 0) for cell in cells), default=0)
        column_count = max(
            (cell.get("ColumnIndex", 0) for cell in cells), default=0
        )
        rows = [[None for _ in range(column_count)] for _ in range(row_count)]
        for cell in cells:
            value = child_text(cell)
            rows[cell["RowIndex"] - 1][cell["ColumnIndex"] - 1] = value or None

        tables.append(
            {
                "page": page,
                "table_number_on_page": table_number_by_page[page],
                "rows": rows,
            }
        )

    return {"lines": lines, "fields": fields, "tables": tables}


def unique(values: Any) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value is None or value == "":
            continue
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def iban_is_valid(iban: str) -> bool:
    compact = re.sub(r"[^A-Z0-9]", "", iban.upper())
    if not 15 <= len(compact) <= 34 or not re.fullmatch(
        r"[A-Z]{2}\d{2}[A-Z0-9]+", compact
    ):
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(
        str(ord(char) - 55) if char.isalpha() else char for char in rearranged
    )
    return int(numeric) % 97 == 1


def extract_ibans(texts: list[str]) -> list[dict[str, Any]]:
    candidates: list[str] = []
    for text in texts:
        upper = text.upper()
        for match in re.finditer(r"DE\s*\d(?:[\s-]*\d){19}", upper):
            candidates.append(re.sub(r"[^A-Z0-9]", "", match.group(0)))

        label_match = re.search(r"\bIBAN\s*:?[\s-]*(.+)", upper)
        if label_match:
            tail = re.split(r"\b(?:BIC|SWIFT)\b", label_match.group(1))[0]
            compact = re.sub(r"[^A-Z0-9]", "", tail)
            if compact.startswith("DE") and len(compact) >= 22:
                candidates.append(compact[:22])
            else:
                match = re.match(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact)
                if match:
                    candidates.append(match.group(0))

    return [
        {"value": iban, "checksum_valid": iban_is_valid(iban)}
        for iban in unique(candidates)
    ]


def extract_bics(texts: list[str]) -> list[str]:
    results: list[str] = []
    for text in texts:
        match = re.search(
            r"\b(?:BIC|SWIFT)\s*:?[\s-]*([A-Z0-9\s]+)", text.upper()
        )
        if not match:
            continue
        candidate = re.sub(r"[^A-Z0-9]", "", match.group(1))
        if len(candidate) >= 11:
            candidate = candidate[:11]
        if len(candidate) in {8, 9, 10, 11}:
            results.append(candidate)
    return unique(results)


def values_for_keys(
    fields: list[dict[str, Any]], patterns: tuple[str, ...]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    combined = re.compile("|".join(patterns), flags=re.IGNORECASE)
    for field in fields:
        if not combined.search(field["key"]):
            continue
        value = field.get("value")
        if value:
            value = value.replace("[X]", "").strip() or None
        results.append(
            {
                "page": field["page"],
                "label": field["key"],
                "value": value,
                "confidence": field["confidence"],
            }
        )
    return results


def first_line_matching(lines: list[str], patterns: tuple[str, ...]) -> str | None:
    combined = re.compile("|".join(patterns), flags=re.IGNORECASE)
    return next((line for line in lines if combined.search(line)), None)


def build_generic_schema(
    source_file: str,
    page_count: int,
    clean_data: dict[str, Any],
) -> dict[str, Any]:
    """Map any bank-change form without provider-specific customer values."""
    fields = clean_data["fields"]
    line_texts = [item["text"] for item in clean_data["lines"]]
    field_texts = [
        f"{field['key'].rstrip(':')}: {field['value']}"
        for field in fields
        if field.get("value")
    ]
    all_texts = line_texts + field_texts

    fields_by_key: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    canonical_key_names: dict[str, str] = {}
    for field in fields:
        casefolded_key = field["key"].casefold()
        canonical_key = canonical_key_names.setdefault(casefolded_key, field["key"])
        fields_by_key[canonical_key].append(
            {
                "page": field["page"],
                "original_label": field["key"],
                "value": field.get("value"),
                "selected": field["selected"],
                "confidence": field["confidence"],
            }
        )

    email_pattern = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
    date_pattern = re.compile(
        r"\b(?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:\d{2}|\d{4})\b"
    )
    creditor_pattern = re.compile(r"\bDE\d{2}[A-Z0-9]{3}\d{11}\b", re.IGNORECASE)

    selected_options = [
        {"page": field["page"], "label": field["key"], "selected": True}
        for field in fields
        if field["selected"]
    ]

    address_and_contact_fields = values_for_keys(
        fields,
        (
            r"firma",
            r"anschrift",
            r"stra(?:ß|ss)e",
            r"plz",
            r"\bort\b",
            r"e-?mail",
            r"telefon|\btel\.?|\bfax\b",
        ),
    )

    return {
        "schema_name": "generic_bank_details_change",
        "schema_version": "2.0",
        "source_file": source_file,
        "page_count": page_count,
        "document_type": first_line_matching(
            line_texts,
            (
                r"antrag.*(?:bankverbindung|bank details)",
                r"änderungen?.*(?:bankverbindung|stammdaten)",
                r"bankverbindung",
            ),
        ),
        "organizations": unique(
            line
            for line in line_texts
            if re.search(r"\b(?:GmbH|AG|S\.\s*A\.)\b", line)
        ),
        "identifiers": {
            "customer_numbers": values_for_keys(
                fields, (r"kunden(?:nummer|-?nr\.?)", r"customer\s*(?:number|id)")
            ),
            "terminal_ids": values_for_keys(
                fields, (r"terminal\s*-?\s*(?:id|nr|nummer)",)
            ),
            "merchant_or_vu_numbers": unique(
                line
                for line in line_texts
                if re.search(r"\b(?:VU|merchant)\b", line, re.IGNORECASE)
                and re.search(r"\d", line)
            ),
            "creditor_identifiers": unique(
                match.group(0).upper()
                for text in all_texts
                for match in creditor_pattern.finditer(text.replace(" ", ""))
            ),
        },
        "company_address_and_contact_fields": address_and_contact_fields,
        "banking": {
            "account_holders": values_for_keys(
                fields,
                (r"kontoinhaber", r"account\s*holder", r"vorname.*nachname"),
            ),
            "bank_or_credit_institutions": values_for_keys(
                fields, (r"bankinstitut", r"kreditinstitut", r"credit institution")
            ),
            "ibans": extract_ibans(all_texts),
            "bics": extract_bics(all_texts),
        },
        "effective_date_fields": values_for_keys(
            fields,
            (
                r"gilt\s+ab",
                r"gültig\s+ab",
                r"verwendung.*datum",
                r"effective\s+(?:from|date)",
            ),
        ),
        "all_detected_dates": unique(
            match.group(0)
            for text in all_texts
            for match in date_pattern.finditer(text)
        ),
        "selected_options": selected_options,
        "sepa_mandate": {
            "present": any("SEPA" in text.upper() for text in line_texts),
            "mandate_reference_text": unique(
                line
                for line in line_texts
                if re.search(r"mandatsreferenz|mandate reference", line, re.IGNORECASE)
            ),
            "signature_or_signatory_fields": values_for_keys(
                fields,
                (r"unterschrift", r"zeichnungsberechtigt", r"signature", r"datum"),
            ),
        },
        "emails": unique(
            match.group(0)
            for text in all_texts
            for match in email_pattern.finditer(text)
        ),
        "all_detected_form_fields": dict(fields_by_key),
    }


def process_pdf(
    textract: Any,
    pdf_path: Path,
    output_root: Path,
    dpi: int,
    rebuild: bool = False,
) -> Path:
    print(f"Processing {pdf_path.name}", flush=True)
    document_dir = output_root / safe_name(pdf_path.stem)
    document_dir.mkdir(parents=True, exist_ok=True)
    raw_path = document_dir / "textract_output.json"
    clean_path = document_dir / "clean_output.json"

    if rebuild and raw_path.is_file():
        print("  Rebuilding from existing Textract JSON", flush=True)
        response = json.loads(raw_path.read_text(encoding="utf-8"))
    else:
        response = analyze_pages(textract, pdf_path, dpi)

    clean_data = extract_clean_data(response)
    page_count = response["DocumentMetadata"]["Pages"]
    schema = build_generic_schema(pdf_path.name, page_count, clean_data)

    raw_path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    clean_path.write_text(
        json.dumps(
            {
                "source_file": pdf_path.name,
                "page_count": page_count,
                "key_fields": schema,
                "text": "\n\n".join(
                    f"===== PAGE {page} =====\n"
                    + "\n".join(
                        item["text"]
                        for item in clean_data["lines"]
                        if item["page"] == page
                    )
                    for page in range(1, page_count + 1)
                ),
                "lines": clean_data["lines"],
                "fields": clean_data["fields"],
                "tables": clean_data["tables"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        f"  Saved {clean_path} "
        f"({len(clean_data['lines'])} lines, {len(clean_data['fields'])} fields, "
        f"{len(clean_data['tables'])} tables)",
        flush=True,
    )
    return clean_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch OCR bank-detail PDFs with AWS Textract"
    )
    parser.add_argument(
        "pdfs",
        nargs="*",
        type=Path,
        help='PDF files. With no arguments, all files matching "BV *.pdf" are used.',
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild clean JSON from existing raw results without calling AWS.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv()

    pdfs = args.pdfs or sorted(Path.cwd().glob("BV *.pdf"))
    if not pdfs:
        print('No PDFs found. Pass files or add PDFs matching "BV *.pdf".', file=sys.stderr)
        return 2

    missing = [str(path) for path in pdfs if not path.is_file()]
    if missing:
        print(f"Input file(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2

    required_env = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION")
    missing_env = [name for name in required_env if not os.getenv(name)]
    if missing_env:
        print(f"Missing environment values: {', '.join(missing_env)}", file=sys.stderr)
        return 2

    textract = boto3.client(
        "textract",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_REGION"),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    for pdf_path in pdfs:
        try:
            process_pdf(textract, pdf_path, args.output_dir, args.dpi, args.rebuild)
        except (RuntimeError, BotoCoreError, ClientError) as exc:
            failures.append(pdf_path.name)
            print(f"FAILED {pdf_path.name}: {exc}", file=sys.stderr, flush=True)

    if failures:
        print(f"Failed documents: {', '.join(failures)}", file=sys.stderr)
        return 1

    print(f"Completed {len(pdfs)} document(s). Output: {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
