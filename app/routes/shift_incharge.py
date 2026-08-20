from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, flash, request, abort
from flask_login import login_required, current_user

from app import db
from app.models.activity import (
    Activity, ActivityType, ApprovalStatus,
    INSPECTION_TYPES, FLIGHT_COVERAGE_TYPES, MAINTENANCE_TYPES, TSR_TYPES, MIC_TYPES, QUALITY_TYPES,
)
from app.models.station import Station
from app.models.shift import Shift
from app.models.aircraft import Aircraft
from app.models.airline import Airline
from app.models.user import User, UserRole
from app.models.inspection import InspectionForm
from app.forms.shift_incharge_forms import ApproveForm, RejectForm
from app.utils.decorators import roles_required
from app.utils.audit import log_action
from app.utils.notify import notify_user

shift_incharge_bp = Blueprint("shift_incharge", __name__, url_prefix="/shift-incharge")

# Reuse the same category configuration the Engineer module uses, so KPI
# tiles/category charts line up exactly with "My Activities".
from app.routes.engineer import CATEGORIES  # noqa: E402


# --------------------------------------------------------------------------
# Shift scoping - a Shift Incharge may only see/manage activities that
# belong to a shift they are assigned to lead. Super Admin gets full
# oversight (no restriction) for administration/troubleshooting.
# --------------------------------------------------------------------------
def _my_shift_ids():
    if current_user.is_super_admin:
        return None  # None => unrestricted
    return [s.id for s in current_user.shifts_led]


def _scope_query(query):
    shift_ids = _my_shift_ids()
    if shift_ids is None:
        return query
    if not shift_ids:
        return query.filter(Activity.id == -1)  # no shift assigned -> nothing in scope
    return query.filter(Activity.shift_id.in_(shift_ids))


def _check_activity_access(activity):
    shift_ids = _my_shift_ids()
    if shift_ids is None:
        return
    if activity.shift_id not in shift_ids:
        abort(403)


def _my_shifts():
    if current_user.is_super_admin:
        return Shift.query.order_by(Shift.name).all()
    return sorted(current_user.shifts_led, key=lambda s: s.name.value)


# --------------------------------------------------------------------------
# Dashboard - database-driven KPIs + charts (no dummy numbers).
# --------------------------------------------------------------------------
@shift_incharge_bp.route("/dashboard")
@login_required
@roles_required(UserRole.SHIFT_INCHARGE, UserRole.SUPER_ADMIN)
def dashboard():
    base = _scope_query(Activity.query)
    approved_q = base.filter(Activity.approval_status == ApprovalStatus.APPROVED)

    pending = base.filter(Activity.approval_status == ApprovalStatus.PENDING_APPROVAL).count()
    approved = approved_q.count()
    rejected = base.filter(Activity.approval_status == ApprovalStatus.REJECTED).count()
    total_activities = base.count()

    category_counts = {}
    for key, cfg in CATEGORIES.items():
        category_counts[key] = approved_q.filter(Activity.activity_type.in_(cfg["types"])).count()

    kpis = {
        "activities": total_activities,
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
        "maintenance_check": category_counts.get("maintenance_check", 0),
        "tsr": category_counts.get("tsr", 0),
        "mic": category_counts.get("mic", 0),
        "replacement": approved_q.filter(Activity.activity_type == ActivityType.REPLACEMENT).count(),
        "quality": category_counts.get("quality", 0),
        "cf": category_counts.get("cf", 0),
        "cf_removal": category_counts.get("cf_removal", 0),
        "carry_forward": approved_q.filter(Activity.activity_type.in_((ActivityType.CF, ActivityType.CF_REMOVAL))).count(),
        "unscheduled": approved_q.filter(Activity.activity_type == ActivityType.PIREP_UNSCHEDULED_MAINTENANCE).count(),
    }

    status_chart = {
        "labels": ["Pending", "Approved", "Rejected"],
        "data": [pending, approved, rejected],
    }
    category_chart = {
        "labels": [cfg["label"] for cfg in CATEGORIES.values()],
        "data": [category_counts.get(key, 0) for key in CATEGORIES.keys()],
    }

    recent_pending = (
        base.filter(Activity.approval_status == ApprovalStatus.PENDING_APPROVAL)
        .order_by(Activity.created_at.desc())
        .limit(6)
        .all()
    )

    return render_template(
        "shift_incharge/dashboard.html",
        kpis=kpis,
        status_chart=status_chart,
        category_chart=category_chart,
        recent_pending=recent_pending,
        my_shifts=_my_shifts(),
        # Shift Incharge no longer has approval authority - only Super
        # Admin (viewing this page for oversight) still does. The template
        # uses this to hide approve/reject entry points without touching
        # anything else on the page.
        can_approve=current_user.is_super_admin,
    )


# --------------------------------------------------------------------------
# Approval Center - Pending | Approved | Rejected tabs.
# --------------------------------------------------------------------------
STATUS_TABS = {
    "pending": ApprovalStatus.PENDING_APPROVAL,
    "approved": ApprovalStatus.APPROVED,
    "rejected": ApprovalStatus.REJECTED,
}


@shift_incharge_bp.route("/approvals")
@login_required
@roles_required(UserRole.SUPER_ADMIN)
def approval_center():
    tab = request.args.get("tab", "pending").strip()
    if tab not in STATUS_TABS:
        tab = "pending"
    status = STATUS_TABS[tab]

    query = _scope_query(Activity.query).filter(Activity.approval_status == status)
    if tab == "pending":
        query = query.order_by(Activity.created_at.desc())
    else:
        query = query.order_by(Activity.approved_at.desc())

    page = request.args.get("page", 1, type=int)
    pagination = query.paginate(page=page, per_page=20, error_out=False)

    counts = {key: _scope_query(Activity.query).filter(Activity.approval_status == val).count() for key, val in STATUS_TABS.items()}

    return render_template(
        "shift_incharge/approval_center.html",
        pagination=pagination,
        activities=pagination.items,
        tab=tab,
        counts=counts,
    )


@shift_incharge_bp.route("/approvals/<int:activity_id>")
@login_required
@roles_required(UserRole.SUPER_ADMIN)
def review_activity(activity_id):
    activity = Activity.query.get_or_404(activity_id)
    _check_activity_access(activity)
    approve_form = ApproveForm()
    reject_form = RejectForm()
    back_tab = {
        ApprovalStatus.PENDING_APPROVAL: "pending",
        ApprovalStatus.APPROVED: "approved",
        ApprovalStatus.REJECTED: "rejected",
    }.get(activity.approval_status, "pending")
    return render_template(
        "shift_incharge/review.html",
        activity=activity,
        approve_form=approve_form,
        reject_form=reject_form,
        back_tab=back_tab,
    )


@shift_incharge_bp.route("/approvals/<int:activity_id>/approve", methods=["POST"])
@login_required
@roles_required(UserRole.SUPER_ADMIN)
def approve_activity(activity_id):
    activity = Activity.query.get_or_404(activity_id)
    _check_activity_access(activity)

    if activity.approval_status != ApprovalStatus.PENDING_APPROVAL:
        flash("Only activities pending approval can be approved.", "warning")
        return redirect(url_for("shift_incharge.review_activity", activity_id=activity.id))

    form = ApproveForm()
    if form.validate_on_submit():
        activity.approval_status = ApprovalStatus.APPROVED
        activity.approval_remarks = (form.remarks.data or "").strip() or None
        activity.approved_by_id = current_user.id
        activity.approved_at = datetime.utcnow()
        db.session.commit()

        log_action(
            "APPROVE", entity_type="Activity", entity_id=activity.id,
            description=f"Approved {activity.activity_type.label} activity logged by {activity.logged_by.full_name if activity.logged_by else 'engineer'}",
        )
        notify_user(
            activity.logged_by_id,
            "Activity Approved",
            f"Your {activity.activity_type.label} activity for {activity.activity_date.strftime('%d %b %Y')} was approved by {current_user.full_name}.",
            link=url_for("engineer.view_activity", activity_id=activity.id),
        )
        flash("Activity approved.", "success")
    else:
        flash("Could not approve this activity.", "danger")

    return redirect(url_for("shift_incharge.approval_center", tab="pending"))


@shift_incharge_bp.route("/approvals/<int:activity_id>/reject", methods=["POST"])
@login_required
@roles_required(UserRole.SUPER_ADMIN)
def reject_activity(activity_id):
    activity = Activity.query.get_or_404(activity_id)
    _check_activity_access(activity)

    if activity.approval_status != ApprovalStatus.PENDING_APPROVAL:
        flash("Only activities pending approval can be rejected.", "warning")
        return redirect(url_for("shift_incharge.review_activity", activity_id=activity.id))

    form = RejectForm()
    if form.validate_on_submit():
        activity.approval_status = ApprovalStatus.REJECTED
        activity.approval_remarks = form.remarks.data.strip()
        activity.approved_by_id = current_user.id
        activity.approved_at = datetime.utcnow()
        db.session.commit()

        log_action(
            "REJECT", entity_type="Activity", entity_id=activity.id,
            description=f"Rejected {activity.activity_type.label} activity: {activity.approval_remarks}",
        )
        notify_user(
            activity.logged_by_id,
            "Activity Rejected",
            f"Your {activity.activity_type.label} activity for {activity.activity_date.strftime('%d %b %Y')} was rejected: {activity.approval_remarks}",
            link=url_for("engineer.view_activity", activity_id=activity.id),
        )
        flash("Activity rejected.", "success")
        return redirect(url_for("shift_incharge.approval_center", tab="pending"))

    flash("Rejection remarks are required to reject an activity.", "danger")
    return redirect(url_for("shift_incharge.review_activity", activity_id=activity.id))


# --------------------------------------------------------------------------
# Shift Monitoring - shift-wise KPI summary + filterable activity list.
# --------------------------------------------------------------------------
@shift_incharge_bp.route("/monitoring")
@login_required
@roles_required(UserRole.SHIFT_INCHARGE, UserRole.SUPER_ADMIN)
def shift_monitoring():
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    station_id = request.args.get("station_id", "").strip()
    engineer_id = request.args.get("engineer_id", "").strip()
    aircraft_id = request.args.get("aircraft_id", "").strip()
    airline_id = request.args.get("airline_id", "").strip()
    activity_type = request.args.get("activity_type", "").strip()
    status_filter = request.args.get("status", "").strip()
    category = request.args.get("category", "").strip()
    page = request.args.get("page", 1, type=int)

    # --- Shift-wise summary (approved counts only, per the statistics rule) ---
    summary = []
    for sh in _my_shifts():
        sh_q = Activity.query.filter(Activity.shift_id == sh.id)
        appr_q = sh_q.filter(Activity.approval_status == ApprovalStatus.APPROVED)
        summary.append({
            "shift": sh,
            "total": sh_q.count(),
            "pending": sh_q.filter(Activity.approval_status == ApprovalStatus.PENDING_APPROVAL).count(),
            "approved": appr_q.count(),
            "rejected": sh_q.filter(Activity.approval_status == ApprovalStatus.REJECTED).count(),
            "maintenance_check": appr_q.filter(Activity.activity_type == ActivityType.MAINTENANCE_CHECK).count(),
            "tsr": appr_q.filter(Activity.activity_type.in_(TSR_TYPES)).count(),
            "mic": appr_q.filter(Activity.activity_type.in_(MIC_TYPES)).count(),
            "replacement": appr_q.filter(Activity.activity_type == ActivityType.REPLACEMENT).count(),
            "ri": appr_q.filter(Activity.activity_type.in_(QUALITY_TYPES)).count(),
            "cf_removal": appr_q.filter(Activity.activity_type == ActivityType.CF_REMOVAL).count(),
            "cf": appr_q.filter(Activity.activity_type == ActivityType.CF).count(),
            "unscheduled": appr_q.filter(Activity.activity_type == ActivityType.PIREP_UNSCHEDULED_MAINTENANCE).count(),
        })

    # --- Filtered activity list ---
    query = _scope_query(Activity.query)

    if date_from:
        try:
            query = query.filter(Activity.activity_date >= datetime.strptime(date_from, "%Y-%m-%d").date())
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(Activity.activity_date <= datetime.strptime(date_to, "%Y-%m-%d").date())
        except ValueError:
            pass
    if station_id:
        query = query.filter(Activity.station_id == int(station_id))
    if engineer_id:
        query = query.filter(Activity.logged_by_id == int(engineer_id))
    if aircraft_id:
        query = query.filter(Activity.aircraft_id == int(aircraft_id))
    if airline_id:
        query = query.join(Aircraft, Activity.aircraft_id == Aircraft.id).filter(Aircraft.airline_id == int(airline_id))
    if activity_type:
        try:
            query = query.filter(Activity.activity_type == ActivityType(activity_type))
        except ValueError:
            pass
    if status_filter:
        try:
            query = query.filter(Activity.approval_status == ApprovalStatus(status_filter))
        except ValueError:
            pass
    category_cfg = CATEGORIES.get(category)
    if category_cfg:
        query = query.filter(Activity.activity_type.in_(category_cfg["types"]))

    query = query.order_by(Activity.activity_date.desc(), Activity.created_at.desc())
    pagination = query.paginate(page=page, per_page=20, error_out=False)

    stations = Station.query.filter_by(is_active=True).order_by(Station.name).all()
    engineers = User.query.filter_by(role=UserRole.ENGINEER, is_active_flag=True).order_by(User.full_name).all()
    aircraft_list = Aircraft.query.filter_by(is_active=True).order_by(Aircraft.registration).all()
    airlines = Airline.query.filter_by(is_active=True).order_by(Airline.name).all()

    return render_template(
        "shift_incharge/shift_monitoring.html",
        summary=summary,
        pagination=pagination,
        activities=pagination.items,
        stations=stations,
        engineers=engineers,
        aircraft_list=aircraft_list,
        airlines=airlines,
        activity_types=list(ActivityType),
        filters={
            "date_from": date_from, "date_to": date_to, "station_id": station_id,
            "engineer_id": engineer_id, "aircraft_id": aircraft_id, "airline_id": airline_id,
            "activity_type": activity_type, "status": status_filter, "category": category,
        },
        category=category,
        category_cfg=category_cfg,
    )


# --------------------------------------------------------------------------
# Engineer Inspection Form approvals - mirrors the Activity approval flow
# above, kept as its own tabbed screen since InspectionForm has a
# different shape (checklist + shared credit) from a plain Activity.
# --------------------------------------------------------------------------
def _inspection_scope_query(query):
    shift_ids = _my_shift_ids()
    if shift_ids is None:
        return query
    if not shift_ids:
        return query.filter(InspectionForm.id == -1)
    return query.filter(InspectionForm.shift_id.in_(shift_ids))


def _check_inspection_access(inspection):
    shift_ids = _my_shift_ids()
    if shift_ids is None:
        return
    if inspection.shift_id not in shift_ids:
        abort(403)


@shift_incharge_bp.route("/inspection-approvals")
@login_required
@roles_required(UserRole.SUPER_ADMIN)
def inspection_approval_center():
    tab = request.args.get("tab", "pending").strip()
    if tab not in STATUS_TABS:
        tab = "pending"
    status = STATUS_TABS[tab]

    query = _inspection_scope_query(InspectionForm.query).filter(InspectionForm.approval_status == status)
    if tab == "pending":
        query = query.order_by(InspectionForm.created_at.desc())
    else:
        query = query.order_by(InspectionForm.approved_at.desc())

    page = request.args.get("page", 1, type=int)
    pagination = query.paginate(page=page, per_page=20, error_out=False)

    counts = {
        key: _inspection_scope_query(InspectionForm.query).filter(InspectionForm.approval_status == val).count()
        for key, val in STATUS_TABS.items()
    }

    return render_template(
        "shift_incharge/inspection_approval_center.html",
        pagination=pagination,
        inspections=pagination.items,
        tab=tab,
        counts=counts,
    )


@shift_incharge_bp.route("/inspection-approvals/<int:inspection_id>")
@login_required
@roles_required(UserRole.SUPER_ADMIN)
def review_inspection(inspection_id):
    inspection = InspectionForm.query.get_or_404(inspection_id)
    _check_inspection_access(inspection)
    approve_form = ApproveForm()
    reject_form = RejectForm()
    back_tab = {
        ApprovalStatus.PENDING_APPROVAL: "pending",
        ApprovalStatus.APPROVED: "approved",
        ApprovalStatus.REJECTED: "rejected",
    }.get(inspection.approval_status, "pending")
    return render_template(
        "shift_incharge/inspection_review.html",
        inspection=inspection,
        approve_form=approve_form,
        reject_form=reject_form,
        back_tab=back_tab,
    )


@shift_incharge_bp.route("/inspection-approvals/<int:inspection_id>/approve", methods=["POST"])
@login_required
@roles_required(UserRole.SUPER_ADMIN)
def approve_inspection(inspection_id):
    inspection = InspectionForm.query.get_or_404(inspection_id)
    _check_inspection_access(inspection)

    if inspection.approval_status != ApprovalStatus.PENDING_APPROVAL:
        flash("Only inspections pending approval can be approved.", "warning")
        return redirect(url_for("shift_incharge.review_inspection", inspection_id=inspection.id))

    form = ApproveForm()
    if form.validate_on_submit():
        inspection.approval_status = ApprovalStatus.APPROVED
        inspection.approval_remarks = (form.remarks.data or "").strip() or None
        inspection.approved_by_id = current_user.id
        inspection.approved_at = datetime.utcnow()
        db.session.commit()

        log_action(
            "APPROVE", entity_type="InspectionForm", entity_id=inspection.id,
            description=f"Approved inspection logged by {inspection.primary_engineer.full_name if inspection.primary_engineer else 'engineer'}",
        )
        notify_user(
            inspection.primary_engineer_id,
            "Inspection Approved",
            f"Your inspection for {inspection.inspection_date.strftime('%d %b %Y')} was approved by {current_user.full_name}.",
            link=url_for("engineer.view_inspection", inspection_id=inspection.id),
        )
        if inspection.second_engineer_id:
            notify_user(
                inspection.second_engineer_id,
                "Inspection Approved",
                f"An inspection you were credited on for {inspection.inspection_date.strftime('%d %b %Y')} was approved by {current_user.full_name}.",
                link=url_for("engineer.view_inspection", inspection_id=inspection.id),
            )
        flash("Inspection approved.", "success")
    else:
        flash("Could not approve this inspection.", "danger")

    return redirect(url_for("shift_incharge.inspection_approval_center", tab="pending"))


@shift_incharge_bp.route("/inspection-approvals/<int:inspection_id>/reject", methods=["POST"])
@login_required
@roles_required(UserRole.SUPER_ADMIN)
def reject_inspection(inspection_id):
    inspection = InspectionForm.query.get_or_404(inspection_id)
    _check_inspection_access(inspection)

    if inspection.approval_status != ApprovalStatus.PENDING_APPROVAL:
        flash("Only inspections pending approval can be rejected.", "warning")
        return redirect(url_for("shift_incharge.review_inspection", inspection_id=inspection.id))

    form = RejectForm()
    if form.validate_on_submit():
        inspection.approval_status = ApprovalStatus.REJECTED
        inspection.approval_remarks = form.remarks.data.strip()
        inspection.approved_by_id = current_user.id
        inspection.approved_at = datetime.utcnow()
        db.session.commit()

        log_action(
            "REJECT", entity_type="InspectionForm", entity_id=inspection.id,
            description=f"Rejected inspection: {inspection.approval_remarks}",
        )
        notify_user(
            inspection.primary_engineer_id,
            "Inspection Rejected",
            f"Your inspection for {inspection.inspection_date.strftime('%d %b %Y')} was rejected: {inspection.approval_remarks}",
            link=url_for("engineer.view_inspection", inspection_id=inspection.id),
        )
        flash("Inspection rejected.", "success")
        return redirect(url_for("shift_incharge.inspection_approval_center", tab="pending"))

    flash("Rejection remarks are required to reject an inspection.", "danger")
    return redirect(url_for("shift_incharge.review_inspection", inspection_id=inspection.id))


# --------------------------------------------------------------------------
# Navigation hub pages (Activities / Inspection / Reports)
#
# These introduce NO new data or business logic - they are pure navigation
# menus of large, clear cards that link out to the exact same routes that
# already existed (plus the optional ?category= filter added to Shift
# Monitoring above, which behaves exactly as before when omitted). This
# keeps the Shift Incharge navbar short (Home / Activities / Inspection /
# Dashboard / Reports / Profile) while every existing page stays fully
# reachable, grouped by what it's for instead of listed flat.
# --------------------------------------------------------------------------
@shift_incharge_bp.route("/activities-menu")
@login_required
@roles_required(UserRole.SHIFT_INCHARGE, UserRole.SUPER_ADMIN)
def activities_menu():
    cards = []
    if current_user.is_super_admin:
        # Approval authority is Super Admin only now - Shift Incharge no
        # longer sees this card.
        cards.append({"label": "Approval Center", "desc": "Review pending, approved, and rejected activities.",
                      "icon": "bi-check2-square", "url": url_for("shift_incharge.approval_center")})
    cards.append({"label": "All Activities", "desc": "Full filterable log for your shift(s).",
                  "icon": "bi-list-check", "url": url_for("shift_incharge.shift_monitoring")})
    for key, cfg in CATEGORIES.items():
        cards.append({
            "label": cfg["label"], "desc": f"{cfg['label']} activity records for your shift(s).",
            "icon": cfg["icon"], "url": url_for("shift_incharge.shift_monitoring", category=key),
        })
    return render_template("shift_incharge/activities_menu.html", cards=cards)


@shift_incharge_bp.route("/inspection-menu")
@login_required
@roles_required(UserRole.SHIFT_INCHARGE, UserRole.SUPER_ADMIN)
def inspection_menu():
    cards = []
    if current_user.is_super_admin:
        # Approval authority is Super Admin only now - Shift Incharge no
        # longer sees this card.
        cards.append({"label": "Inspection Approvals", "desc": "Review pending, approved, and rejected inspection forms.",
                      "icon": "bi-clipboard2-pulse", "url": url_for("shift_incharge.inspection_approval_center")})
    cards.append({"label": "Maintenance Checks", "desc": "Individual maintenance check activity records.",
                  "icon": "bi-clipboard2-check", "url": url_for("shift_incharge.shift_monitoring", category="maintenance_check")})
    return render_template("shift_incharge/inspection_menu.html", cards=cards)


@shift_incharge_bp.route("/reports-menu")
@login_required
@roles_required(UserRole.SHIFT_INCHARGE, UserRole.SUPER_ADMIN)
def reports_menu():
    from datetime import date, timedelta
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    cards = [
        {"label": "Daily Report", "desc": "Activities logged today.",
         "icon": "bi-calendar-day",
         "url": url_for("shift_incharge.shift_monitoring", date_from=today.isoformat(), date_to=today.isoformat())},
        {"label": "Weekly Report", "desc": "Activities logged so far this week.",
         "icon": "bi-calendar-week",
         "url": url_for("shift_incharge.shift_monitoring", date_from=week_start.isoformat(), date_to=today.isoformat())},
        {"label": "Monthly Report", "desc": "Activities logged so far this month.",
         "icon": "bi-calendar-month",
         "url": url_for("shift_incharge.shift_monitoring", date_from=month_start.isoformat(), date_to=today.isoformat())},
        {"label": "Maintenance Check Report", "desc": "Maintenance check activity records.",
         "icon": "bi-clipboard2-check", "url": url_for("shift_incharge.shift_monitoring", category="maintenance_check")},
        {"label": "Replacement Report", "desc": "Replacement activity records.",
         "icon": "bi-arrow-repeat", "url": url_for("shift_incharge.shift_monitoring", category="replacement")},
        {"label": "Inspection Report", "desc": "Submitted inspection forms and approvals.",
         "icon": "bi-clipboard2-pulse",
         "url": url_for("shift_incharge.inspection_approval_center") if current_user.is_super_admin
         else url_for("shift_incharge.shift_monitoring", category="maintenance_check")},
    ]
    return render_template("shift_incharge/reports_menu.html", cards=cards)
