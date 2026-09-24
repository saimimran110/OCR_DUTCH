from __future__ import annotations

import io
import json
import os
import re
import sys
import traceback
import uuid
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


WEB_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = WEB_ROOT.parent
UPLOAD_ROOT = WEB_ROOT / "uploads"
RUN_ROOT = WEB_ROOT / "runs"
MAX_UPLOAD_BYTES = 30 * 1024 * 1024

sys.path.insert(0, str(PROJECT_ROOT))
from test import analyze_pages, build_generic_schema, extract_clean_data  # noqa: E402


def clean_label(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value.rstrip(":").strip() or "Unnamed field"


def canonical(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def dedupe_fields(
    clean_data: dict[str, Any], schema: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return unique editable fields while retaining distinct repeated values."""
    candidates: list[dict[str, Any]] = []

    for field in clean_data["fields"]:
        label = clean_label(field.get("key", ""))
        value = field.get("value") or ""
        value = value.replace("[X]", "").strip()
        # A checkbox/tick without text is not a field value. Keep the field
        # empty so the reviewer can enter a value instead of seeing a false
        # value such as "Selected".
        candidates.append(
            {
                # Preserve the label exactly as detected on the document.
                "label": label,
                "value": value,
                "page": field.get("page"),
                "confidence": field.get("confidence"),
                "selected": bool(field.get("selected")),
                "checkbox": bool(field.get("checkbox")),
            }
        )

    def add_schema_value(label: str, value: Any, valid: bool | None = None) -> None:
        if value in (None, ""):
            return
        candidate = {
            "label": label,
            "value": str(value),
            "page": None,
            "confidence": None,
        }
        if valid is not None:
            candidate["validation"] = "valid" if valid else "review"
        candidates.append(candidate)

    identifiers = schema.get("identifiers", {})
    for item in identifiers.get("customer_numbers", []):
        add_schema_value("Kundennummer", item.get("value"))
    for item in identifiers.get("terminal_ids", []):
        add_schema_value("Terminal-ID", item.get("value"))
    for value in identifiers.get("creditor_identifiers", []):
        add_schema_value("Creditor Identifier", value)

    banking = schema.get("banking", {})
    for item in banking.get("ibans", []):
        add_schema_value("IBAN", item.get("value"), item.get("checksum_valid"))
    for value in banking.get("bics", []):
        add_schema_value("BIC", value)

    # Keep only fields directly detected by Textract. Schema-derived fields
    # have no source confidence/page and are not needed in the review UI.
    candidates = [
        field for field in candidates if field.get("confidence") is not None
    ]

    unique_fields: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for field in candidates:
        marker = (canonical(field["label"]), field["value"].casefold().strip())
        if marker in seen_pairs:
            continue
        seen_pairs.add(marker)
        unique_fields.append(field)

    # Repeated labels with different values remain present, but receive unique
    # display names so the approved spreadsheet has no duplicate field names.
    totals: dict[str, int] = {}
    for field in unique_fields:
        key = canonical(field["label"])
        totals[key] = totals.get(key, 0) + 1
    occurrences: dict[str, int] = {}
    for field in unique_fields:
        key = canonical(field["label"])
        occurrences[key] = occurrences.get(key, 0) + 1
        field["id"] = uuid.uuid4().hex
        if totals[key] > 1:
            field["label"] = f"{field['label']} ({occurrences[key]})"

    return unique_fields


def build_excel(fields: list[dict[str, Any]], source_file: str) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Approved Fields"
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = f"A1:D{max(len(fields) + 1, 2)}"

    headers = ["Field", "Value", "Source Page", "OCR Confidence"]
    header_fill = PatternFill("solid", fgColor="2563EB")
    for column, header in enumerate(headers, start=1):
        cell = worksheet.cell(row=1, column=column, value=header)
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")

    for row, field in enumerate(fields, start=2):
        worksheet.cell(row=row, column=1, value=str(field.get("label", "")))
        worksheet.cell(row=row, column=2, value=str(field.get("value", "")))
        worksheet.cell(row=row, column=3, value=field.get("page"))
        confidence = field.get("confidence")
        worksheet.cell(
            row=row,
            column=4,
            value=float(confidence) / 100 if confidence not in (None, "") else None,
        )
        if confidence not in (None, ""):
            worksheet.cell(row=row, column=4).number_format = "0.00%"

    widths = [34, 72, 14, 17]
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width
    worksheet.sheet_properties.pageSetUpPr.fitToPage = True
    worksheet.oddHeader.center.text = f"Approved extraction — {source_file}"

    metadata = workbook.create_sheet("Metadata")
    metadata.append(["Source File", source_file])
    metadata.append(["Approved Field Count", len(fields)])
    metadata.column_dimensions["A"].width = 24
    metadata.column_dimensions["B"].width = 60

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


class PrototypeHandler(SimpleHTTPRequestHandler):
    server_version = "BankVerificationPrototype/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/api/health":
            self.send_json({"status": "ok"})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        try:
            if route == "/api/extract":
                self.handle_extract()
            elif route == "/api/export":
                self.handle_export()
            else:
                self.send_json({"error": "Endpoint not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:  # Prototype server: return a useful UI error.
            traceback.print_exc()
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_body(self) -> bytes:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            raise ValueError("The request body is empty.")
        if content_length > MAX_UPLOAD_BYTES:
            raise ValueError("The PDF exceeds the 30 MB prototype limit.")
        return self.rfile.read(content_length)

    def multipart_file(self) -> tuple[str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("Expected a multipart PDF upload.")
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + self.read_body()
        )
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            if part.get_param("name", header="content-disposition") != "file":
                continue
            filename = part.get_filename() or "document.pdf"
            return filename, part.get_payload(decode=True) or b""
        raise ValueError("No PDF file was included in the upload.")

    def handle_extract(self) -> None:
        filename, content = self.multipart_file()
        if not filename.lower().endswith(".pdf") or not content.startswith(b"%PDF-"):
            raise ValueError("Only valid PDF files are supported.")

        document_id = uuid.uuid4().hex
        upload_path = UPLOAD_ROOT / f"{document_id}.pdf"
        run_dir = RUN_ROOT / document_id
        run_dir.mkdir(parents=True, exist_ok=True)
        upload_path.write_bytes(content)

        textract = boto3.client(
            "textract",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION"),
        )
        response = analyze_pages(textract, upload_path, dpi=200)
        clean_data = extract_clean_data(response)
        page_count = response["DocumentMetadata"]["Pages"]
        schema = build_generic_schema(filename, page_count, clean_data)
        fields = dedupe_fields(clean_data, schema)

        (run_dir / "textract_output.json").write_text(
            json.dumps(response, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (run_dir / "clean_output.json").write_text(
            json.dumps(
                {
                    "source_file": filename,
                    "page_count": page_count,
                    "key_fields": schema,
                    "fields": fields,
                    "lines": clean_data["lines"],
                    "tables": clean_data["tables"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        confidences = [
            field["confidence"]
            for field in fields
            if field.get("confidence") is not None
        ]
        self.send_json(
            {
                "document_id": document_id,
                "filename": filename,
                "pdf_url": f"/uploads/{document_id}.pdf",
                "page_count": page_count,
                "field_count": len(fields),
                "average_confidence": (
                    round(sum(confidences) / len(confidences), 1)
                    if confidences
                    else None
                ),
                "fields": fields,
            }
        )

    def handle_export(self) -> None:
        body = self.read_body()
        payload = json.loads(body.decode("utf-8"))
        fields = payload.get("fields")
        if not isinstance(fields, list) or not fields:
            raise ValueError("There are no approved fields to export.")

        # Apply the same exact-pair deduplication to user-edited fields.
        approved: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for field in fields:
            label = clean_label(str(field.get("label", "")))
            value = str(field.get("value", "")).strip()
            marker = (canonical(label), value.casefold())
            if marker in seen:
                continue
            seen.add(marker)
            approved.append(
                {
                    "label": label,
                    "value": value,
                    "page": field.get("page"),
                    "confidence": field.get("confidence"),
                }
            )

        source_file = str(payload.get("filename") or "document.pdf")
        excel = build_excel(approved, source_file)
        download_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(source_file).stem)
        download_name = f"{download_name}_approved.xlsx"

        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.send_header(
            "Content-Disposition", f'attachment; filename="{download_name}"'
        )
        self.send_header("Content-Length", str(len(excel)))
        self.end_headers()
        self.wfile.write(excel)


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    host = os.getenv("PROTOTYPE_HOST", "127.0.0.1")
    port = int(os.getenv("PROTOTYPE_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), PrototypeHandler)
    print(f"Prototype running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
