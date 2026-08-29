"""Central per-entity authorization for business operations.

`_check_role` answers "may this role ever run this operation".  It cannot answer
"may this person touch *this* case", so a 担当 could advance a colleague's case by
naming its id.  This module supplies the missing half: every operation declares
which parameter carries its ownership binding, and `apply_operation` resolves that
binding before any mutation runs.

Design rules that the negative tests pin:

* The table is exhaustive.  `assert_registry_covered` fails when an operation is
  added without a scope decision, so a new mutation cannot arrive unguarded.
* Undeclared operations are denied for restricted roles rather than allowed.
* A declared key that is absent or blank contributes no permission.  The operation's
  own validation decides whether it was required; omitting an id can never widen
  access because the operation then has no target to act on.
* Entities rebuilt from the audit log (applications, IT sessions, messages) bind to
  their case first and to their recording actor second.  An entity with neither
  binding is unowned and therefore not someone else's to protect.
"""
from __future__ import annotations

from . import access

# --- scope kinds -----------------------------------------------------------
CASE = "case"          # params[key] is a case id
ENTITY = "entity"      # params[key] identifies an audit-replayed record
TASK = "task"          # params[key] is a task id (tasks.assignee is the binding)
PROPERTY = "property"  # params[key] is a property id
CUSTOMER = "customer"  # params[key] is a customer id
NONE = "none"          # webhook intake, exports, drafts and settings: no case owner

# Operations whose ownership binding lives behind an audit replay helper.
_ENTITY_LOADERS = {
    "application": "_load_application",
    "it_session": "_load_it_session",
    "message": "_load_message",
    "inquiry": None,  # resolved through load_inquiries
}

# --- the table -------------------------------------------------------------
# Every operation in operations.OPERATIONS must appear exactly once.
SCOPES: dict[str, tuple] = {
    # -- direct case binding -------------------------------------------------
    "activity_report": (CASE, "case_id"),
    "application_create": (CASE, "case_id"),
    "billing_create": (CASE, "case_id"),
    "bukkaku_code_assign": (CASE, "case_id"),
    "bukkaku_send": (CASE, "case_id"),
    "case_advance": (CASE, "case_id"),
    "case_lose": (CASE, "case_id"),
    "contact_log_add": (CASE, "case_id"),
    "contract_create": (CASE, "case_id"),
    "hearing_create": (CASE, "case_id"),
    "inquiry_create": (CASE, "case_id"),
    "it_session_create": (CASE, "case_id"),
    "juusetsu_consent_record": (CASE, "case_id"),
    "juusetsu_deliver": (CASE, "case_id"),
    "line_flex_property_send": (CASE, "case_id"),
    "line_send": (CASE, "case_id"),
    "moveout_settle": (CASE, "case_id"),
    "portal_link": (CASE, "case_id"),
    "property_status_set": (CASE, "case_id"),
    "reins_prepare": (CASE, "case_id"),
    "reins_record": (CASE, "case_id"),
    "requirement_check": (CASE, "case_id"),
    "schedule_slots": (CASE, "case_id"),
    "stage_advance": (CASE, "case_id"),
    "viewing_schedule": (CASE, "case_id"),

    # -- audit-replayed entities --------------------------------------------
    "application_advance": (ENTITY, "application", "application_id"),
    "screening_result": (ENTITY, "application", "application_id"),
    "it_advance": (ENTITY, "it_session", "session_id"),
    "it_check_requirement": (ENTITY, "it_session", "session_id"),
    "it_schedule_confirm": (ENTITY, "it_session", "session_id"),
    "message_queue": (ENTITY, "message", "message_id"),
    "message_send": (ENTITY, "message", "message_id"),
    "inquiry_resolve": (ENTITY, "inquiry", "inquiry_id"),

    # -- task ownership ------------------------------------------------------
    "task_done": (TASK, "task_id"),
    "task_snooze": (TASK, "task_id"),
    "task_unsnooze": (TASK, "task_id"),

    # -- property / customer scoped -----------------------------------------
    "attribute_update": (CUSTOMER, "customer_id"),
    "liff_publish": (PROPERTY, "property_id"),
    "proposal_draft": (PROPERTY, "property_id"),
    "viewing_list": (PROPERTY, "property_id"),

    # -- no case-ownership dimension ----------------------------------------
    # Creation paths stamp the actor as assignee; there is no prior owner to check.
    "customer_case_create": (NONE,),
    "lead_convert": (NONE,),
    "lead_quick_add": (NONE,),
    "property_register": (NONE,),
    "line_start_it_juusetsu": (NONE,),
    # Inbound webhooks: the caller is the channel, not a member of staff.
    "call_receive": (NONE,),
    "fax_receive": (NONE,),
    "line_receive": (NONE,),
    "line_harness_pull": (NONE,),
    "inbox_ingest": (NONE,),
    # Gate-guarded sends and approvals: privileged roles only (see OP_ROLES).
    "approval_decide": (NONE,),
    "hold_release": (NONE,),
    "esign_create": (NONE,),
    "esign_send": (NONE,),
    "fax_confirm_send": (NONE,),
    "line_confirm_send": (NONE,),
    "extraction_approve": (NONE,),
    "billing_reconcile": (NONE,),
    "reconcile_deposits": (NONE,),
    "it_gate_set": (NONE,),
    # Drafts, reads and office-wide utilities that touch no single case.
    "asset_attest": (NONE,),
    "caller_directory_add": (NONE,),
    "extraction_save": (NONE,),
    "followup_generate": (NONE,),
    "liff_export": (NONE,),
    "message_draft": (NONE,),
    "obi_swap": (NONE,),
    "ocr_extract": (NONE,),
    "ocr_read": (NONE,),
    "overdue_reminders": (NONE,),
    "permission_record": (NONE,),
    "renewal_generate": (NONE,),
    "zoning_lookup": (NONE,),
}


class _ActorViewer:
    """access.py speaks Viewer; apply_operation only carries actor and role."""

    __slots__ = ("user", "role", "_identities", "_data_dir")

    def __init__(self, data_dir, actor: str, role: str):
        self.user = str(actor or "").strip()
        self.role = str(role or "")
        self._identities = None
        self._data_dir = data_dir

    def sees_all_rows(self) -> bool:
        return self.role != "担当"

    def identities(self) -> frozenset[str]:
        if self._identities is None:
            names = {self.user} if self.user else set()
            try:
                from . import auth

                record = auth.load_users(self._data_dir).get(self.user) or {}
                display = str(record.get("display_name") or "").strip()
                if display:
                    names.add(display)
            except Exception:
                pass
            self._identities = frozenset(names)
        return self._identities


def _case_assignee(data_dir, case_id: str) -> tuple[bool, str]:
    """Return (case exists, its assignee).  Ambiguous rows read as unresolvable."""
    rows = [row for row in access._case_rows(data_dir)
            if str(row.get("case_id") or "").strip() == case_id]
    if len(rows) != 1:
        return False, ""
    return True, str(rows[0].get("assignee") or "").strip()


def _assignment_is_active(data_dir) -> bool:
    """Whether this ledger has started using explicit担当 ownership."""
    return any(str(row.get("assignee") or "").strip()
               for row in access._case_rows(data_dir))


def _param(params, key: str) -> str:
    try:
        return str(params.get(key) or "").strip()
    except AttributeError:
        return ""


def _entity_case_and_actor(data_dir, kind: str, entity_id: str):
    """Return (case_id, recording_actor) for an audit-replayed record."""
    from . import operations

    record = None
    if kind == "inquiry":
        loader = getattr(operations, "load_inquiries", None)
        if callable(loader):
            try:
                record = next((r for r in loader(data_dir)
                               if str(r.get("inquiry_id") or "").strip() == entity_id), None)
            except Exception:
                record = None
    else:
        loader = getattr(operations, _ENTITY_LOADERS.get(kind) or "", None)
        if callable(loader):
            try:
                record = loader(data_dir, entity_id)
            except Exception:
                record = None
    if not isinstance(record, dict):
        return "", ""
    return (str(record.get("case_id") or "").strip(),
            str(record.get("actor") or "").strip())


def _task_allowed(data_dir, viewer, task_id: str) -> tuple[bool, str]:
    """(allowed, owner).  Task owners are authoritative once case assignment is active."""
    from . import operations

    try:
        rows = operations._store(data_dir).query("tasks", "task_id = ?", (task_id,))
    except Exception:
        return False, "不明"
    if len(rows) != 1:
        # Missing or ambiguous: let the operation raise its own 404 rather than
        # inventing an ownership verdict, but never treat it as a grant.
        return (not rows), "不明"
    assignee = str(rows[0].get("assignee") or "").strip()
    if not _assignment_is_active(data_dir):
        # Older fixtures and ledgers used task担当 labels before auth display-name
        # bindings existed.  Keep them operable until explicit case ownership starts.
        return True, assignee
    if not assignee or assignee in viewer.identities():
        return True, assignee
    return False, assignee


def _entity_scope_allowed(data_dir, viewer, field: str, entity_id: str) -> bool:
    """Property/customer scope: all matching cases must be explicitly mine.

    An entity with no case attached has no owner to protect, so it stays reachable.
    Assignmentless legacy ledgers also stay operable.  Once any row has a担当,
    blank matching rows are unassigned work and cannot become a mutation grant.
    """
    rows = [row for row in access._case_rows(data_dir)
            if str(row.get(field) or "").strip() == entity_id]
    if not rows:
        return True
    if not _assignment_is_active(data_dir):
        return True
    mine = viewer.identities()
    return all(str(row.get("assignee") or "").strip() in mine for row in rows)


def _case_mutation_allowed(data_dir, viewer, case_id: str) -> tuple[bool, str]:
    """Ownership rule for mutations.

    Unknown ids are denied once assignment is active so a guess never becomes a probe.
    Assignmentless legacy ledgers stay operable and let the operation report its own
    404.  Once any row has a担当, blank rows are unassigned work and cannot become a
    mutation grant.  Triage is then a read workflow; assignment must be explicit.
    """
    exists, assignee = _case_assignee(data_dir, case_id)
    if not exists:
        if not _assignment_is_active(data_dir):
            return True, ""
        return False, f"案件 {case_id} が見つかりません。"
    if not assignee:
        if not _assignment_is_active(data_dir):
            return True, ""
        return False, f"案件 {case_id} はまだ担当が割り当てられていません。"
    if assignee in viewer.identities():
        return True, ""
    return False, f"案件 {case_id} は {assignee} の担当です。"


def check(data_dir, op: str, params, actor: str, role: str) -> tuple[bool, str]:
    """Return (allowed, reason).  Fail-closed for anything undeclared."""
    viewer = _ActorViewer(data_dir, actor, role)
    if viewer.sees_all_rows():
        return True, ""

    scope = SCOPES.get(op)
    if scope is None:
        return False, (f"操作 {op} は担当者スコープが未定義のため実行できません。"
                       "責任者に連絡してください。")

    kind = scope[0]
    if kind == NONE:
        return True, ""

    if kind == CASE:
        case_id = _param(params, scope[1])
        if not case_id:
            return True, ""
        return _case_mutation_allowed(data_dir, viewer, case_id)

    if kind == TASK:
        task_id = _param(params, scope[1])
        if not task_id:
            return True, ""
        allowed, owner = _task_allowed(data_dir, viewer, task_id)
        if allowed:
            return True, ""
        return False, f"タスク {task_id} は {owner} の担当です。"

    if kind == ENTITY:
        entity_kind, key = scope[1], scope[2]
        entity_id = _param(params, key)
        if not entity_id:
            return True, ""
        case_id, recorded_actor = _entity_case_and_actor(data_dir, entity_kind, entity_id)
        if case_id:
            return _case_mutation_allowed(data_dir, viewer, case_id)
        if recorded_actor and recorded_actor not in viewer.identities():
            return False, f"{entity_id} は {recorded_actor} が扱っています。"
        return True, ""

    if kind in (PROPERTY, CUSTOMER):
        field = "property_id" if kind == PROPERTY else "customer_id"
        entity_id = _param(params, scope[1])
        if not entity_id:
            return True, ""
        if _entity_scope_allowed(data_dir, viewer, field, entity_id):
            return True, ""
        label = "物件" if kind == PROPERTY else "顧客"
        return False, f"{label} {entity_id} の案件はあなたの担当ではありません。"

    return False, f"操作 {op} のスコープ種別 {kind} を解釈できません。"


def assert_registry_covered(registry) -> None:
    """Every operation declares a scope, and no scope names a dead operation."""
    declared = set(SCOPES)
    registered = set(registry)
    undeclared = sorted(registered - declared)
    if undeclared:
        raise AssertionError(
            "スコープ未宣言の操作があります（担当ロールから到達不能になります）: "
            + ", ".join(undeclared))
    stale = sorted(declared - registered)
    if stale:
        raise AssertionError("実在しない操作のスコープが残っています: " + ", ".join(stale))
