# Bank Verification Review Prototype

## Run

From the project root:

```powershell
python prototype\server.py
```

Open <http://127.0.0.1:8000>.

The server reads the AWS credentials already configured in the parent `.env` file.

## Workflow

1. Upload a PDF (single-page or multi-page).
2. Review the source PDF on the left and deduplicated extracted fields on the right.
3. Edit, add, or delete fields.
4. Click **Approve & Download Excel**.

Raw and clean extraction records are stored under `prototype/runs/<document-id>/`.
Uploaded PDFs are stored under `prototype/uploads/` for local preview.
