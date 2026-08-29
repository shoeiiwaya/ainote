"""Fail-closed entity ownership checks for authenticated web operations.

Read-only screens may show unassigned work to staff so it can be triaged.  Files,
documents and mutations are different: a staff member must be the explicit assignee.
The SQLite business store is authoritative when it exists; a corrupt or incomplete
store never falls back to a potentially stale CSV copy for an authorization decision.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path


def _case_rows(data_dir) -> list[dict[str, str]]:
    root = Path(data_dir)
    db = root / "hub.db"
    if db.exists():
        try:
            connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            try:
                columns = {
                    str(row[1])
                    for row in connection.execute('PRAGMA table_info("cases")')
                }
                required = {"case_id", "property_id", "customer_id", "assignee"}
                if not required <= columns:
                    return []
                return [dict(row) for row in connection.execute(
                    'SELECT case_id, property_id, customer_id, assignee FROM "cases"'
                )]
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return []

    source = root / "cases.csv"
    if not source.is_file():
        return []
    try:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            return [
                {
                    "case_id": str(row.get("案件ID") or ""),
                    "property_id": str(row.get("物件ID") or ""),
                    "customer_id": str(row.get("顧客ID") or ""),
                    "assignee": str(row.get("担当") or ""),
                }
                for row in csv.DictReader(handle)
            ]
    except (OSError, csv.Error, UnicodeError):
        return []


def _is_privileged(viewer) -> bool:
    if viewer is None:
        return False
    sees_all_rows = getattr(viewer, "sees_all_rows", None)
    if callable(sees_all_rows):
        return bool(sees_all_rows())
    return getattr(viewer, "role", None) != "担当"


def viewer_identities(viewer) -> frozenset[str]:
    """Strings that read as "this person" in a ledger's 担当 column.

    Login ids are ascii while ledgers carry Japanese names, so an id-only comparison
    silently denies every legitimate assignee.  The administrator binds the two by
    recording display_name on the account.
    """
    resolve = getattr(viewer, "identities", None)
    if callable(resolve):
        return resolve()
    return frozenset(v for v in (str(getattr(viewer, "user", "") or "").strip(),) if v)


def _exact_case_binding(data_dir, case_id: str,
                        customer_id: str | None = None) -> dict[str, str] | None:
    """Resolve one authoritative case row without applying a role shortcut."""
    target_case = str(case_id or "").strip()
    if not target_case:
        return None
    matches = [row for row in _case_rows(data_dir)
               if str(row.get("case_id") or "").strip() == target_case]
    if len(matches) != 1:
        return None
    row = matches[0]
    if customer_id is not None:
        target_customer = str(customer_id or "").strip()
        if (not target_customer
                or str(row.get("customer_id") or "").strip() != target_customer):
            return None
    return row


def _viewer_may_read_binding(viewer, row: dict[str, str]) -> bool:
    if viewer is None:
        return False
    if _is_privileged(viewer):
        return True
    return str(row.get("assignee") or "").strip() in viewer_identities(viewer)


def case_access_allowed(data_dir, viewer, case_id: str) -> bool:
    """Allow staff mutations/secret reads only for one explicitly assigned case."""
    return authorized_case_binding(data_dir, viewer, case_id) is not None


def authorized_case_binding(data_dir, viewer, case_id: str) -> dict[str, str] | None:
    """Return one exact case row only after its assignment policy succeeds."""
    row = _exact_case_binding(data_dir, case_id)
    if row is None or not _viewer_may_read_binding(viewer, row):
        return None
    return dict(row)


def case_customer_binding(data_dir, viewer, case_id: str,
                          customer_id: str) -> dict[str, str] | None:
    """Return one exact case/customer join after assignment authorization.

    Privileged roles may skip the assignee comparison, but never the requested
    case/customer join.  Callers expose the same 404 for every ``None`` result.
    """
    row = _exact_case_binding(data_dir, case_id, customer_id)
    if row is None or not _viewer_may_read_binding(viewer, row):
        return None
    return dict(row)


def related_entity_access_allowed(data_dir, viewer, field: str, entity_id: str) -> bool:
    """Authorize a property/customer only when all matching cases belong to staff."""
    if viewer is None:
        return False
    if _is_privileged(viewer):
        return True
    if field not in {"property_id", "customer_id"}:
        return False
    target = str(entity_id or "").strip()
    if not target:
        return False
    mine = viewer_identities(viewer)
    matches = [row for row in _case_rows(data_dir)
               if str(row.get(field) or "").strip() == target]
    return bool(matches) and all(
        str(row.get("assignee") or "").strip() in mine for row in matches)


def document_access_allowed(data_dir, viewer, doc_id: str,
                            version: int | None = None) -> bool:
    """Resolve metadata and ownership without opening document body bytes."""
    if viewer is None:
        return False
    try:
        from . import documents

        meta = documents.get_version_metadata(data_dir, doc_id, version)
    except Exception:
        return False
    case_id = str(meta.get("case_id") or "").strip()
    stored_customer = str(meta.get("customer_id") or "").strip()
    # This legacy helper has no caller-supplied case/customer tuple to compare.
    # A fully bound document still has an exact tuple available in its metadata,
    # so even a privileged caller must prove that stored case/customer join.  Only
    # older internal documents missing one side retain the compatibility shortcut.
    # The product /case export path never uses the shortcut: it supplies the full
    # requested tuple to case_bound_document_metadata below.
    if _is_privileged(viewer):
        if case_id and stored_customer:
            return _exact_case_binding(data_dir, case_id, stored_customer) is not None
        return True
    if not case_id:
        return False
    row = _exact_case_binding(data_dir, case_id)
    if row is None:
        return False
    if stored_customer and str(row.get("customer_id") or "").strip() != stored_customer:
        return False
    return _viewer_may_read_binding(viewer, row)


def case_bound_document_metadata(data_dir, viewer, *, case_id: str,
                                 customer_id: str, doc_id: str,
                                 version: int | None = None,
                                 requested_format: str | None = None,
                                 require_four_kind: bool = True) -> dict | None:
    """Authorize an exact case/customer/document/version/format tuple metadata-first.

    This is the trust boundary used by the case workspace export route.  It never
    invokes ``documents.get_version`` and therefore cannot read body bytes before
    the tuple and assignee checks have passed.
    """
    binding = case_customer_binding(data_dir, viewer, case_id, customer_id)
    if binding is None:
        return None
    try:
        from . import documents

        meta = documents.get_version_metadata(data_dir, doc_id, version)
    except Exception:
        return None
    # The document store has a legacy filesystem-safety normalizer which maps
    # unsafe characters such as ``/`` to ``_``.  That must not make two raw
    # caller-supplied identifiers equivalent at this authorization boundary.
    # Require the persisted canonical identifier to equal the requested value
    # before any document body byte can be opened.
    requested_doc = str(doc_id or "")
    if str(meta.get("doc_id") or "") != requested_doc:
        return None
    if str(meta.get("case_id") or "").strip() != str(case_id or "").strip():
        return None
    stored_customer = str(meta.get("customer_id") or "").strip()
    requested_customer = str(customer_id or "").strip()
    # A missing legacy customer binding is not an implicit match.  The case
    # workspace exports only versions that were explicitly saved for this exact
    # customer; migration must write that fact before any body byte is opened.
    if stored_customer != requested_customer:
        return None
    canonical_kind = documents.canonical_four_document_kind(meta.get("kind") or "")
    if require_four_kind and canonical_kind is None:
        return None
    output_format = None
    if requested_format is not None:
        output_format = str(requested_format or "").strip().lower()
        if (not output_format
                or output_format not in documents.four_document_output_formats(canonical_kind or "")):
            return None
    result = dict(meta)
    result["customer_id"] = requested_customer
    result["canonical_kind"] = canonical_kind
    if output_format is not None:
        result["output_format"] = output_format
    result["assignee"] = str(binding.get("assignee") or "")
    return result


def list_case_four_document_metadata(data_dir, viewer, *, case_id: str,
                                     customer_id: str) -> list[dict]:
    """List only authorized four-kind metadata for one exact case/customer."""
    if case_customer_binding(data_dir, viewer, case_id, customer_id) is None:
        return []
    from . import documents

    rows = []
    for summary in documents.list_documents(data_dir):
        if str(summary.get("case_id") or "").strip() != str(case_id or "").strip():
            continue
        meta = case_bound_document_metadata(
            data_dir, viewer, case_id=case_id, customer_id=customer_id,
            doc_id=str(summary.get("doc_id") or ""),
            version=int(summary.get("latest") or 0), require_four_kind=True,
        )
        if meta is not None:
            rows.append(meta)
    return rows


def document_summary_access_allowed(data_dir, viewer, summary: dict) -> bool:
    if viewer is None:
        return False
    case_id = str(summary.get("case_id") or "").strip()
    if not case_id:
        return _is_privileged(viewer)
    row = _exact_case_binding(data_dir, case_id)
    if row is None:
        return False
    stored_customer = str(summary.get("customer_id") or "").strip()
    if stored_customer and str(row.get("customer_id") or "").strip() != stored_customer:
        return False
    return _viewer_may_read_binding(viewer, row)
