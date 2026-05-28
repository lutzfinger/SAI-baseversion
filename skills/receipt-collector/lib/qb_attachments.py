"""
QuickBooks Online — Attachable (file attachment) helpers.

The QB v3 REST API attaches files via a special multipart endpoint
`POST /v3/company/<realm>/upload`. Each upload carries two parts per
file:

  file_metadata_NN  application/json   — describes which entity to link to
  file_content_NN   <file mime>        — the binary content

A successful upload returns an Attachable object whose Id can be linked
to multiple entities (we always link to one Purchase).

Idempotency: we put a stable marker in the Attachable.Note field
("[sai-receipt:<trip>:<purchase_id>] <filename>") and query Attachable
list before upload so re-runs don't create duplicates.
"""
from __future__ import annotations

import json
import mimetypes
import os
import uuid
from pathlib import Path


def _make_marker(trip_slug: str, entity_type: str, entity_id: str, filename: str) -> str:
    return f"[sai-receipt:{trip_slug}:{entity_type}:{entity_id}] {filename}"


def find_existing(
    client, trip_slug: str, entity_type: str, entity_id: str, filename: str,
) -> dict | None:
    """Return the Attachable already linked to this entity with the matching marker.

    Works for any entity type (Purchase, Invoice, Bill, etc.).
    """
    marker = _make_marker(trip_slug, entity_type, entity_id, filename)
    safe = marker.replace("'", "''")
    q = f"SELECT * FROM Attachable WHERE Note LIKE '%{safe}%' MAXRESULTS 50"
    resp = client._request(
        "GET",
        f"/v3/company/{client.realm}/query",
        params={"query": q, "minorversion": "75"},
    )
    if resp.status_code != 200:
        return None
    rows = resp.json().get("QueryResponse", {}).get("Attachable", [])
    for a in rows:
        for ref in a.get("AttachableRef") or []:
            ent = ref.get("EntityRef") or {}
            if ent.get("type") == entity_type and str(ent.get("value")) == str(entity_id):
                return a
    return None


def upload_for_entity(
    client,
    entity_type: str,
    entity_id: str,
    file_path: Path,
    trip_slug: str,
    note_prefix: str = "",
    include_on_send: bool = False,
) -> dict:
    """Upload a file and attach it to any QB entity (Purchase, Invoice, Bill...).

    Idempotent: if a marker-matching Attachable already exists on this
    entity, returns it without re-uploading.

    Set include_on_send=True for Invoice attachments that should travel
    with the email when QB sends the invoice to the customer.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(file_path)
    filename = file_path.name

    existing = find_existing(client, trip_slug, entity_type, entity_id, filename)
    if existing:
        return existing

    mime, _ = mimetypes.guess_type(str(file_path))
    if not mime:
        mime = "application/octet-stream"

    note = (note_prefix.rstrip() + "\n\n" if note_prefix else "") + _make_marker(
        trip_slug, entity_type, entity_id, filename
    )
    metadata = {
        "AttachableRef": [
            {
                "EntityRef": {"type": entity_type, "value": str(entity_id)},
                "IncludeOnSend": include_on_send,
            }
        ],
        "FileName": filename,
        "ContentType": mime,
        "Note": note.strip(),
    }

    boundary = f"----SAIBoundary{uuid.uuid4().hex}"

    def part(name: str, content_type: str, content: bytes, *, filename: str | None = None) -> bytes:
        disp = f'form-data; name="{name}"'
        if filename:
            disp += f'; filename="{filename}"'
        head = (
            f"--{boundary}\r\n"
            f"Content-Disposition: {disp}\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        return head + content + b"\r\n"

    body = (
        part("file_metadata_01", "application/json", json.dumps(metadata).encode())
        + part("file_content_01", mime, file_path.read_bytes(), filename=filename)
        + f"--{boundary}--\r\n".encode()
    )

    resp = client._request(
        "POST",
        f"/v3/company/{client.realm}/upload",
        params={"minorversion": "75"},
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"upload_for_entity failed {entity_type} Id={entity_id} file={filename}: "
            f"{resp.status_code} {resp.text[:400]}"
        )
    out = resp.json()
    if "AttachableResponse" in out:
        return out["AttachableResponse"][0]["Attachable"]
    return out.get("Attachable", out)


# Backwards-compat shims
def upload_for_purchase(client, purchase_id, file_path, trip_slug, note_prefix=""):
    return upload_for_entity(client, "Purchase", purchase_id, file_path,
                             trip_slug=trip_slug, note_prefix=note_prefix,
                             include_on_send=False)


def upload_for_invoice(client, invoice_id, file_path, trip_slug, note_prefix="",
                        include_on_send=True):
    """Attach a file to a QB Invoice. include_on_send=True (default) means
    the PDF travels with the email when QB sends the invoice to the customer."""
    return upload_for_entity(client, "Invoice", invoice_id, file_path,
                             trip_slug=trip_slug, note_prefix=note_prefix,
                             include_on_send=include_on_send)
