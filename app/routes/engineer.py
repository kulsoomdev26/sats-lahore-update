from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, flash, request, abort
from flask_login import login_required, current_user

from app import db
from app.models.activity import (
    Activity, ActivityType, ApprovalStatus, QariEntry, MaintenanceStatus,
    TSR_TYPES, MIC_TYPES, QUALITY_TYPES, DEFECT_TYPES, REPLACEMENT_TYPES, CF_REMOVAL_TYPES,
)
from app.models.station import Station
from app.models.shift import Shift
from app.models.aircraft import Aircraft
from app.models.airline import Airline
from app.models.user import User, UserRole
from app.models.inspection import InspectionForm, InspectionEntry, InspectionCredit
from app.forms.engineer_forms import ActivityForm, InspectionFormForm
from app.utils.decorators import roles_required
from app.utils.audit import log_action
from app.utils.notify import notify_user, shift_incharge_ids_for, notify_users
from app.utils import analytics as A
from flask import jsonify

engineer_bp = Blueprint("engineer", __name__, url_prefix="/engineer")

INSPECTION_STATION_NAME = "Lahore"


# --------------------------------------------------------------------------
# Category configuration - drives the shared list view + dashboard tiles.
# --------------------------------------------------------------------------
CATEGORIES = {
    "maintenance_check": {
        "label": "Maintenance Check",
        "icon": "bi-clipboard2-check",
        "types": (ActivityType.MAINTENANCE_CHECK,),
    },
    "mic": {
        "label": "MIC / Scheduled Maintenance",
        "icon": "bi-clipboard2-pulse",
        "types": MIC_TYPES,
    },
    "quality": {
        "label": "QARI",
        "icon": "bi-patch-check",
        "types": QUALITY_TYPES,
    },
    "tsr": {
        "label": "TSR",
        "icon": "bi-exclamation-triangle",
        "types": TSR_TYPES,
    },
    "pirep_unscheduled": {
        "label": "PIREP / Unscheduled Maintenance",
        "icon": "bi-wrench-adjustable",
        "types": DEFECT_TYPES,
    },
    "replacement": {
        "label": "Replacement",
        "icon": "bi-arrow-repeat",
        "types": REPLACEMENT_TYPES,
    },
    "cf_removal": {
        "label": "CF Removal",
        "icon": "bi-dash-circle",
        "types": CF_REMOVAL_TYPES,
    },
    "cf": {
        "label": "CF",
        "icon": "bi-arrow-repeat",
        "types": (ActivityType.CF,),
    },
}


def _scope_query(query):
    """Engineers only ever see their own activities in this module.
    Shift Incharge is an Engineer for the purposes of this personal
    logging module too, so they're scoped to their own activities here
    exactly like an Engineer (their separate, broader shift-wide *view*
    still lives in the Shift Incharge module - this module never grants
    approval authority). DCE / Super Admin see everything (for oversight)."""
    if current_user.is_engineer or current_user.is_shift_incharge:
        return query.filter(Activity.logged_by_id == current_user.id)
    return query


def _populate_choices(form, airline_id=None):
    """Populate all dropdown choices for the Engineer Activity Form.

    Station is intentionally still populated (kept for backward
    compatibility with `_apply_form`, which always sets `station_id`) even
    though the field is no longer rendered - the value is forced to the
    Lahore station in the route itself.
    """
    form.station_id.choices = [(s.id, f"{s.code} - {s.name}") for s in Station.query.filter_by(is_active=True).order_by(Station.name).all()]
    form.shift_id.choices = [(0, "-- Select --")] + [
        (sh.id, f"{sh.name.label} ({sh.station.code})") for sh in Shift.query.filter_by(is_active=True).order_by(Shift.name).all()
    ]

    form.airline_id.choices = [(0, "-- Select Airline --")] + [
        (al.id, al.name) for al in Airline.query.filter_by(is_active=True).order_by(Airline.name).all()
    ]

    aircraft_q = Aircraft.query.filter_by(is_active=True)
    if airline_id:
        aircraft_q = aircraft_q.filter_by(airline_id=airline_id)
    form.aircraft_id.choices = [(0, "-- Select Aircraft --")] + [
        (a.id, a.registration) for a in aircraft_q.order_by(Aircraft.registration).all()
    ]

    engineers = User.query.filter_by(role=UserRole.ENGINEER, is_active_flag=True).order_by(User.full_name).all()
    form.engineer_id.choices = [(0, "-- Select --")] + [(e.id, e.full_name) for e in engineers]
    form.crs_engineer_id.choices = [(0, "-- Select --")] + [(e.id, e.full_name) for e in engineers]
    form.second_engineer_id.choices = [(0, "-- None --")] + [(e.id, e.full_name) for e in engineers]


def _apply_form(activity, form):
    from app.models.activity import (
        MaintenanceType, MaintenanceStatus, TsrMicStatus, QualityStatus,
        CoverageType, MaintenanceCheckType,
    )

    activity.activity_date = form.activity_date.data
    activity.station_id = form.station_id.data
    activity.shift_id = form.shift_id.data or None
    activity.flight_number = (form.flight_number.data or "").strip() or None
    activity.coverage_type = CoverageType(form.coverage_type.data) if form.coverage_type.data else None

    # Airline / aircraft: PIA uses the existing dropdown-linked Aircraft
    # row, anything else is entered manually and aircraft_id is left null.
    activity.airline_id = form.airline_id.data or None
    if form._is_pia_selected():
        activity.aircraft_id = form.aircraft_id.data or None
        activity.aircraft_registration_manual = None
        activity.aircraft_model_manual = None
    else:
        activity.aircraft_id = None
        activity.aircraft_registration_manual = (form.aircraft_registration_manual.data or "").strip() or None
        activity.aircraft_model_manual = (form.aircraft_model_manual.data or "").strip() or None

    activity_type = ActivityType(form.activity_type.data)

    # Reset every activity-specific column before repopulating - keeps
    # edits (e.g. switching from TSR to Replacement) from leaving stale
    # data behind in unrelated columns.
    activity.maintenance_check_type = None
    activity.is_crs = None
    activity.crs_engineer_id = None
    activity.second_engineer_id = None
    activity.inspection_details = None
    activity.remarks = None
    activity.component = None
    activity.maintenance_details = None
    activity.tsr_number = None
    activity.tsr_status = None
    activity.maintenance_status = None
    activity.quality_status = None

    if activity_type == ActivityType.MAINTENANCE_CHECK:
        activity.maintenance_check_type = MaintenanceCheckType(form.maintenance_check_type.data) if form.maintenance_check_type.data else None
        activity.remarks = (form.mc_remarks.data or "").strip() or None
        activity.inspection_details = (form.mc_details.data or "").strip() or None
        activity.is_crs = form.is_crs.data == "yes"
        if activity.is_crs:
            activity.crs_engineer_id = activity.logged_by_id
            activity.second_engineer_id = form.second_engineer_id.data or None
        else:
            activity.crs_engineer_id = form.crs_engineer_id.data or None
            activity.second_engineer_id = activity.logged_by_id

    elif activity_type == ActivityType.MIC_SCHEDULED_MAINTENANCE:
        activity.component = (form.mic_type.data or "").strip() or None
        activity.maintenance_details = (form.mic_description.data or "").strip() or None

    elif activity_type == ActivityType.TSR:
        activity.tsr_number = (form.tsr_number.data or "").strip() or None
        activity.tsr_status = TsrMicStatus(form.tsr_status.data) if form.tsr_status.data else None
        activity.maintenance_details = (form.tsr_description.data or "").strip() or None

    elif activity_type in (ActivityType.PIREP_UNSCHEDULED_MAINTENANCE, ActivityType.CF):
        activity.inspection_details = (form.pirep_short_description.data or "").strip() or None
        if form.pirep_status.data == "cf":
            activity_type = ActivityType.CF
            activity.maintenance_status = MaintenanceStatus.CARRY_FORWARD
        else:
            activity_type = ActivityType.PIREP_UNSCHEDULED_MAINTENANCE
            activity.maintenance_status = MaintenanceStatus.COMPLETED

    elif activity_type == ActivityType.CF_REMOVAL:
        activity.maintenance_status = MaintenanceStatus.COMPLETED if form.cf_removed.data == "yes" else MaintenanceStatus.IN_PROGRESS
        activity.maintenance_details = (form.cf_removal_details.data or "").strip() or None

    elif activity_type == ActivityType.REPLACEMENT:
        activity.component = (form.replacement_component.data or "").strip() or None
        activity.maintenance_details = (form.replacement_details.data or "").strip() or None

    activity.activity_type = activity_type


def _apply_qari_entries(activity, form):
    """Fully replace the QARI child rows (max 2) - same delete+recreate
    pattern already used elsewhere in this codebase (see
    InspectionForm._save_entries) so edits never leave stale rows."""
    activity.qari_entries = []
    if activity.activity_type != ActivityType.QARI:
        return
    from app.models.activity import QariSeverity, QariEntryStatus, QualityStatus
    for entry_form in form.qari_entries.entries[:2]:
        f = entry_form.form
        if not (f.qari_number.data or "").strip() and not (f.short_description.data or "").strip():
            continue
        activity.qari_entries.append(QariEntry(
            severity=QariSeverity(f.severity.data) if f.severity.data else QariSeverity.MINOR,
            qari_number=(f.qari_number.data or "").strip() or None,
            sari_closed_count=f.sari_closed_count.data,
            short_description=(f.short_description.data or "").strip() or None,
            status=QariEntryStatus(f.status.data) if f.status.data else None,
        ))
        # Keep the flat columns on Activity itself in sync with the first
        # QARI entry so existing dashboards/reports that read
        # quality_status/quality_finding directly keep working unchanged.
        if len(activity.qari_entries) == 1:
            activity.quality_inspection_type = f.severity.data or None
            activity.quality_finding = (f.short_description.data or "").strip() or None
            activity.quality_status = (
                QualityStatus.PASSED if f.status.data == "closed"
                else QualityStatus.PENDING if f.status.data == "open"
                else None
            )


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@engineer_bp.route("/dashboard")
@login_required
def dashboard():
    base = _scope_query(Activity.query)

    approved = base.filter(Activity.approval_status == ApprovalStatus.APPROVED).count()
    pending = base.filter(Activity.approval_status == ApprovalStatus.PENDING_APPROVAL).count()
    rejected = base.filter(Activity.approval_status == ApprovalStatus.REJECTED).count()

    category_counts = {}
    for key, cfg in CATEGORIES.items():
        category_counts[key] = base.filter(Activity.activity_type.in_(cfg["types"])).count()

    wheel_changes = base.filter(Activity.activity_type == ActivityType.REPLACEMENT).count()

    # Inspection Form credit summary (approved inspections only, same
    # "approved only counts" convention as the rest of the dashboard).
    inspection_credit_total = 0.0
    inspection_forms_count = 0
    if current_user.is_engineer or current_user.is_shift_incharge:
        my_credit_rows = (
            InspectionCredit.query.join(InspectionForm)
            .filter(
                InspectionCredit.engineer_id == current_user.id,
                InspectionCredit.credit_type == "inspection",
                InspectionForm.approval_status == ApprovalStatus.APPROVED,
            )
            .all()
        )
        inspection_credit_total = sum(float(c.credit_value) for c in my_credit_rows)
        inspection_forms_count = InspectionForm.query.filter(
            db.or_(
                InspectionForm.primary_engineer_id == current_user.id,
                InspectionForm.second_engineer_id == current_user.id,
            )
        ).count()
    else:
        inspection_forms_count = InspectionForm.query.count()

    stats = {
        "approved": approved,
        "pending": pending,
        "rejected": rejected,
        "total": approved + pending + rejected,
        "categories": category_counts,
        "wheel_changes": wheel_changes,
        "inspection_forms_count": inspection_forms_count,
        "inspection_credit_total": inspection_credit_total,
    }

    recent = base.order_by(Activity.created_at.desc()).limit(8).all()

    # ----------------------------------------------------------------
    # Graphs / trends - built from real database records via the same
    # analytics helpers that power the DCE Dashboard, scoped to this
    # engineer's own activities (or unscoped for DCE/Super Admin, who
    # see everything on this module for oversight - same scoping rule
    # as the rest of this dashboard).
    # ----------------------------------------------------------------
    scoped_engineer_id = current_user.id if (current_user.is_engineer or current_user.is_shift_incharge) else None
    chart_filters = {
        "date_from": None, "date_to": None, "station_id": None, "shift_name": None,
        "engineer_id": scoped_engineer_id, "aircraft_id": None, "airline_id": None,
        "activity_type": None, "status": None,
    }
    charts = {
        "daily_trend": A.daily_trend(chart_filters),
        "monthly_trend": A.monthly_trend(chart_filters),
        "status_breakdown": {
            "labels": ["Approved", "Pending", "Rejected"],
            "data": [approved, pending, rejected],
        },
        "category_breakdown": {
            "labels": [cfg["label"] for cfg in CATEGORIES.values()],
            "data": [category_counts.get(key, 0) for key in CATEGORIES],
        },
    }

    return render_template("engineer/dashboard.html", stats=stats, categories=CATEGORIES, recent=recent, charts=charts)


# --------------------------------------------------------------------------
# Navigation hub pages (Activities / Reports)
#
# These introduce NO new data or business logic - they are pure navigation
# menus of large, clear cards that link out to the exact same routes that
# already existed. This keeps the Engineer navbar short (Dashboard /
# Activities / Reports / Profile) while every existing page stays fully
# reachable, grouped by what it's for instead of listed flat. Activities
# and the former Inspection hub are combined into a single "Activities"
# entry/page.
# --------------------------------------------------------------------------
@engineer_bp.route("/activities/menu")
@login_required
def activities_menu():
    # Activities hub: focused purely on recording and viewing activities
    # through the existing activity system. The Inspection Form entry
    # points ("New Inspection Form" / "Inspection Forms") have been
    # removed from this page per the simplified Activities design - the
    # underlying InspectionForm routes, data, and functionality are all
    # untouched and remain reachable from the Dashboard's Inspection
    # Forms tile.
    cards = [
        {"label": "Log New Activity", "desc": "Record a new activity you performed.",
         "icon": "bi-plus-square", "url": url_for("engineer.create_activity")},
        {"label": "All My Activities", "desc": "View and search everything you've logged.",
         "icon": "bi-list-check", "url": url_for("engineer.list_activities")},
    ] if (current_user.is_engineer or current_user.is_shift_incharge) else [
        {"label": "All Activities", "desc": "View and search logged activities.",
         "icon": "bi-list-check", "url": url_for("engineer.list_activities")},
    ]

    for key, cfg in CATEGORIES.items():
        cards.append({
            "label": cfg["label"], "desc": f"{cfg['label']} activity records.",
            "icon": cfg["icon"], "url": url_for("engineer.list_activities", category=key),
        })
    return render_template("engineer/activities_menu.html", cards=cards)


@engineer_bp.route("/inspection")
@login_required
def inspection_menu():
    # Kept as a redirect (route not removed, so no old link/bookmark
    # breaks) - its cards now live on the combined Activities hub above.
    return redirect(url_for("engineer.activities_menu"))


@engineer_bp.route("/reports")
@login_required
def reports_menu():
    from datetime import date, timedelta
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    # Reorganized per the simplified Reports design: Daily / Weekly /
    # Monthly / Activity Report / All Activities. Every card links to the
    # same existing list_activities view (with its existing filtering and
    # searching) - no new report routes or duplicate logic were added.
    cards = [
        {"label": "Daily Report", "desc": "Activities logged today.",
         "icon": "bi-calendar-day",
         "url": url_for("engineer.list_activities", date_from=today.isoformat(), date_to=today.isoformat())},
        {"label": "Weekly Report", "desc": "Activities logged so far this week.",
         "icon": "bi-calendar-week",
         "url": url_for("engineer.list_activities", date_from=week_start.isoformat(), date_to=today.isoformat())},
        {"label": "Monthly Report", "desc": "Activities logged so far this month.",
         "icon": "bi-calendar-month",
         "url": url_for("engineer.list_activities", date_from=month_start.isoformat(), date_to=today.isoformat())},
        {"label": "Activity Report", "desc": "Search and filter all your logged activities by date, status, or shift.",
         "icon": "bi-file-earmark-bar-graph", "url": url_for("engineer.list_activities")},
        {"label": "All Activities", "desc": "Browse records by activity type - all 8 activity types.",
         "icon": "bi-list-check", "url": url_for("engineer.all_activities_menu")},
    ]
    return render_template("engineer/reports_menu.html", cards=cards)


@engineer_bp.route("/reports/all-activities")
@login_required
def all_activities_menu():
    # "All Activities" report: one card per activity type (all 8 canonical
    # ActivityType values, via the existing CATEGORIES config), each
    # linking to the existing, fully-functional list_activities view
    # filtered to that category.
    cards = []
    for key, cfg in CATEGORIES.items():
        cards.append({
            "label": cfg["label"], "desc": f"{cfg['label']} activity records.",
            "icon": cfg["icon"], "url": url_for("engineer.list_activities", category=key),
        })
    return render_template("engineer/all_activities_menu.html", cards=cards)


# --------------------------------------------------------------------------
# Shared list view (used for "My Activities" and each category screen)
# --------------------------------------------------------------------------
@engineer_bp.route("/activities")
@login_required
def list_activities():
    category = request.args.get("category", "").strip()
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    shift_filter = request.args.get("shift_id", "").strip()
    sort = request.args.get("sort", "date_desc")
    page = request.args.get("page", 1, type=int)

    query = _scope_query(Activity.query)

    cfg = CATEGORIES.get(category)
    if cfg:
        query = query.filter(Activity.activity_type.in_(cfg["types"]))

    if q:
        like = f"%{q}%"
        query = query.join(Station, Activity.station_id == Station.id, isouter=True).join(
            Aircraft, Activity.aircraft_id == Aircraft.id, isouter=True
        ).join(User, Activity.logged_by_id == User.id, isouter=True).filter(
            db.or_(
                Aircraft.registration.ilike(like),
                Station.name.ilike(like),
                Station.code.ilike(like),
                User.full_name.ilike(like),
                Activity.flight_number.ilike(like),
                Activity.tsr_number.ilike(like),
                Activity.mic_number.ilike(like),
                Activity.remarks.ilike(like),
            )
        )

    if status_filter:
        try:
            query = query.filter(Activity.approval_status == ApprovalStatus(status_filter))
        except ValueError:
            pass

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

    if shift_filter:
        try:
            query = query.filter(Activity.shift_id == int(shift_filter))
        except ValueError:
            pass

    sort_map = {
        "date_desc": Activity.activity_date.desc(),
        "date_asc": Activity.activity_date.asc(),
        "created_desc": Activity.created_at.desc(),
        "status": Activity.approval_status.asc(),
    }
    query = query.order_by(sort_map.get(sort, Activity.activity_date.desc()))

    pagination = query.paginate(page=page, per_page=20, error_out=False)

    shifts = Shift.query.filter_by(is_active=True).order_by(Shift.name).all()

    return render_template(
        "engineer/activities_list.html",
        pagination=pagination,
        activities=pagination.items,
        category=category,
        category_cfg=cfg,
        categories=CATEGORIES,
        q=q,
        status_filter=status_filter,
        date_from=date_from,
        date_to=date_to,
        shift_filter=shift_filter,
        shifts=shifts,
        sort=sort,
    )


# --------------------------------------------------------------------------
# Create / Edit / Delete
# --------------------------------------------------------------------------
@engineer_bp.route("/activities/new", methods=["GET", "POST"])
@login_required
@roles_required(UserRole.ENGINEER, UserRole.SHIFT_INCHARGE, UserRole.SUPER_ADMIN)
def create_activity():
    form = ActivityForm()
    airline_id = form.airline_id.data or request.args.get("airline_id", type=int)
    _populate_choices(form, airline_id=airline_id)

    # Lahore is now the fixed, non-editable station for this form.
    station = _inspection_station()
    if request.method == "GET" and station:
        form.station_id.data = station.id

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    if form.validate_on_submit():
        if station:
            form.station_id.data = station.id

        activity = Activity()
        if current_user.is_super_admin and form.engineer_id.data:
            activity.logged_by_id = form.engineer_id.data
        else:
            activity.logged_by_id = current_user.id

        _apply_form(activity, form)
        _apply_qari_entries(activity, form)

        activity.approval_status = ApprovalStatus.PENDING_APPROVAL
        db.session.add(activity)
        db.session.commit()
        log_action("CREATE", entity_type="Activity", entity_id=activity.id, description=f"Logged {activity.activity_type.label} activity")

        notify_users(
            shift_incharge_ids_for(activity.shift),
            "New Activity Awaiting Approval",
            f"{activity.logged_by.full_name if activity.logged_by else 'An engineer'} submitted "
            f"a {activity.activity_type.label} activity for {activity.activity_date.strftime('%d %b %Y')}.",
            link=url_for("shift_incharge.review_activity", activity_id=activity.id),
        )
        notify_user(
            activity.logged_by_id,
            "Activity Submitted",
            f"Your {activity.activity_type.label} activity for {activity.activity_date.strftime('%d %b %Y')} has been submitted for approval.",
            link=url_for("engineer.view_activity", activity_id=activity.id),
        )

        if is_ajax:
            # Powers the "Save Activity -> Add Another Activity" flow: the
            # page stays put and JS appends this to the in-session list
            # instead of a full redirect.
            return jsonify({
                "success": True,
                "activity_id": activity.id,
                "activity_type_label": activity.activity_type.label,
                "detail_summary": activity.detail_summary,
                "view_url": url_for("engineer.view_activity", activity_id=activity.id),
            })

        flash("Activity submitted for approval.", "success")
        return redirect(url_for("engineer.list_activities", category=activity.category))

    if is_ajax:
        errors = {name: field.errors for name, field in form._fields.items() if field.errors}
        return jsonify({"success": False, "errors": errors}), 400

    return render_template("engineer/activity_form.html", form=form, mode="create")


@engineer_bp.route("/activities/<int:activity_id>")
@login_required
def view_activity(activity_id):
    activity = Activity.query.get_or_404(activity_id)
    # Plain Engineers may only view their own activity detail pages. Shift
    # Incharge is intentionally NOT restricted here (unlike edit/delete
    # below) because they still need to view any activity within their
    # shift for monitoring purposes - they just can't approve/reject it.
    if current_user.is_engineer and activity.logged_by_id != current_user.id:
        abort(403)
    return render_template("engineer/activity_detail.html", activity=activity)


@engineer_bp.route("/activities/<int:activity_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required(UserRole.ENGINEER, UserRole.SHIFT_INCHARGE, UserRole.SUPER_ADMIN)
def edit_activity(activity_id):
    activity = Activity.query.get_or_404(activity_id)

    if (current_user.is_engineer or current_user.is_shift_incharge) and activity.logged_by_id != current_user.id:
        abort(403)
    if not current_user.is_super_admin and not activity.is_editable:
        flash("This activity has already been reviewed and can no longer be edited.", "warning")
        return redirect(url_for("engineer.view_activity", activity_id=activity.id))

    form = ActivityForm(obj=activity)
    _populate_choices(form, airline_id=activity.airline_id)

    if request.method == "GET":
        activity_type = activity.activity_type
        form.activity_type.data = activity_type.value if activity_type else None
        form.shift_id.data = activity.shift_id or 0
        form.airline_id.data = activity.airline_id or 0
        form.aircraft_id.data = activity.aircraft_id or 0
        form.engineer_id.data = activity.logged_by_id or 0
        form.coverage_type.data = activity.coverage_type.value if activity.coverage_type else ""

        if activity_type == ActivityType.MAINTENANCE_CHECK:
            form.maintenance_check_type.data = activity.maintenance_check_type.value if activity.maintenance_check_type else ""
            form.mc_remarks.data = activity.remarks
            form.mc_details.data = activity.inspection_details
            form.is_crs.data = "yes" if activity.is_crs else ("no" if activity.is_crs is not None else "")
            form.crs_engineer_id.data = activity.crs_engineer_id or 0
            form.second_engineer_id.data = activity.second_engineer_id or 0
        elif activity_type == ActivityType.MIC_SCHEDULED_MAINTENANCE:
            form.mic_type.data = activity.component
            form.mic_description.data = activity.maintenance_details
        elif activity_type == ActivityType.QARI:
            form.qari_entries.entries = []
            for qe in activity.qari_entries[:2]:
                form.qari_entries.append_entry({
                    "severity": qe.severity.value if qe.severity else "",
                    "qari_number": qe.qari_number,
                    "sari_closed_count": qe.sari_closed_count,
                    "short_description": qe.short_description,
                    "status": qe.status.value if qe.status else "",
                })
        elif activity_type == ActivityType.TSR:
            form.tsr_status.data = activity.tsr_status.value if activity.tsr_status else ""
            form.tsr_description.data = activity.maintenance_details
        elif activity_type in (ActivityType.PIREP_UNSCHEDULED_MAINTENANCE, ActivityType.CF):
            form.pirep_short_description.data = activity.inspection_details
            form.pirep_status.data = "cf" if activity_type == ActivityType.CF else "closed"
            form.activity_type.data = ActivityType.PIREP_UNSCHEDULED_MAINTENANCE.value
        elif activity_type == ActivityType.CF_REMOVAL:
            form.cf_removed.data = "yes" if activity.maintenance_status == MaintenanceStatus.COMPLETED else "no"
            form.cf_removal_details.data = activity.maintenance_details
        elif activity_type == ActivityType.REPLACEMENT:
            form.replacement_component.data = activity.component
            form.replacement_details.data = activity.maintenance_details

    if form.validate_on_submit():
        was_rejected = activity.approval_status == ApprovalStatus.REJECTED
        if current_user.is_super_admin and form.engineer_id.data:
            activity.logged_by_id = form.engineer_id.data
        _apply_form(activity, form)
        _apply_qari_entries(activity, form)

        if was_rejected:
            # Resubmission: clear the previous review decision and send it
            # back into the Shift Incharge's Approval Center.
            activity.approval_status = ApprovalStatus.PENDING_APPROVAL
            activity.approval_remarks = None
            activity.approved_by_id = None
            activity.approved_at = None

        db.session.commit()

        if was_rejected:
            log_action("RESUBMIT", entity_type="Activity", entity_id=activity.id, description=f"Resubmitted {activity.activity_type.label} activity after rejection")
            notify_users(
                shift_incharge_ids_for(activity.shift),
                "Activity Resubmitted",
                f"{activity.logged_by.full_name if activity.logged_by else 'An engineer'} resubmitted "
                f"a {activity.activity_type.label} activity for {activity.activity_date.strftime('%d %b %Y')}.",
                link=url_for("shift_incharge.review_activity", activity_id=activity.id),
            )
            flash("Activity resubmitted for approval.", "success")
        else:
            log_action("UPDATE", entity_type="Activity", entity_id=activity.id, description=f"Updated {activity.activity_type.label} activity")
            flash("Activity updated successfully.", "success")

        return redirect(url_for("engineer.view_activity", activity_id=activity.id))

    return render_template("engineer/activity_form.html", form=form, mode="edit", activity=activity)


@engineer_bp.route("/activities/<int:activity_id>/delete", methods=["POST"])
@login_required
@roles_required(UserRole.ENGINEER, UserRole.SHIFT_INCHARGE, UserRole.SUPER_ADMIN)
def delete_activity(activity_id):
    activity = Activity.query.get_or_404(activity_id)

    if (current_user.is_engineer or current_user.is_shift_incharge) and activity.logged_by_id != current_user.id:
        abort(403)
    if not current_user.is_super_admin and not activity.is_editable:
        flash("This activity has already been reviewed and can no longer be deleted.", "warning")
        return redirect(url_for("engineer.view_activity", activity_id=activity.id))

    category = activity.category
    db.session.delete(activity)
    db.session.commit()
    log_action("DELETE", entity_type="Activity", entity_id=activity_id, description="Deleted activity")
    flash("Activity deleted.", "success")
    return redirect(url_for("engineer.list_activities", category=category))


# ==========================================================================
# Engineer Inspection Form (Module 5)
#
# Distinct from the generic Activity form above: this is a single
# submission that carries a full Yes/No + remarks checklist across every
# activity type, plus a shared-credit model between a primary and an
# optional second engineer. Credits are stored in InspectionCredit and are
# always fully rebuilt (never appended to) on save - see
# InspectionForm.rebuild_credits() - so an inspection or an activity
# within it can never be double counted, even across repeated edits.
# ==========================================================================

def _inspection_station():
    """The Lahore station this form is auto-locked to."""
    station = Station.query.filter(
        db.or_(Station.name.ilike(f"%{INSPECTION_STATION_NAME}%"), Station.code == "LHE")
    ).filter_by(is_active=True).first()
    if not station:
        station = Station.query.filter(
            db.or_(Station.name.ilike(f"%{INSPECTION_STATION_NAME}%"), Station.code == "LHE")
        ).first()
    return station


def _active_engineers(exclude_id=None):
    q = User.query.filter_by(role=UserRole.ENGINEER, is_active_flag=True)
    if exclude_id:
        q = q.filter(User.id != exclude_id)
    return q.order_by(User.full_name).all()


def _populate_inspection_choices(form, primary_exclude_id=None):
    station = _inspection_station()
    form.station_id.choices = [(station.id, f"{station.code} - {station.name}")] if station else []

    form.shift_id.choices = [(0, "-- Select --")] + [
        (sh.id, f"{sh.name.label}") for sh in Shift.query.filter_by(is_active=True).order_by(Shift.name).all()
    ]

    form.airline_id.choices = [(0, "-- Select Airline --")] + [
        (al.id, al.name) for al in Airline.query.filter_by(is_active=True).order_by(Airline.name).all()
    ]

    # Aircraft choices start scoped to the currently posted/selected airline
    # (if any) so validation works without JS; the searchable dropdown is
    # re-populated client-side via /engineer/inspections/aircraft-options.
    airline_id = form.airline_id.data or None
    aircraft_q = Aircraft.query.filter_by(is_active=True)
    if airline_id:
        aircraft_q = aircraft_q.filter_by(airline_id=airline_id)
    form.aircraft_id.choices = [(0, "-- Select Aircraft --")] + [
        (a.id, a.registration) for a in aircraft_q.order_by(Aircraft.registration).all()
    ]

    engineers = User.query.filter_by(role=UserRole.ENGINEER, is_active_flag=True).order_by(User.full_name).all()
    form.primary_engineer_id.choices = [(0, "-- Select --")] + [(e.id, e.full_name) for e in engineers]

    # Second engineer must always exclude whichever engineer is currently
    # selected as primary.
    form.second_engineer_id.choices = [(0, "-- None --")] + [
        (e.id, e.full_name) for e in _active_engineers(exclude_id=primary_exclude_id)
    ]


def _set_activity_selection(form, inspection=None):
    """Seed form.activity_type from an existing inspection's saved entry
    when editing. New (create) forms are left to their default choice."""
    if inspection is None:
        return
    entry = next((e for e in inspection.entries if e.performed), None)
    if entry is not None:
        form.activity_type.data = entry.activity_type.value


@engineer_bp.route("/inspections/aircraft-options")
@login_required
def inspection_aircraft_options():
    """AJAX endpoint powering the airline -> aircraft searchable dropdown."""
    airline_id = request.args.get("airline_id", type=int)
    q = Aircraft.query.filter_by(is_active=True)
    if airline_id:
        q = q.filter_by(airline_id=airline_id)
    aircraft = q.order_by(Aircraft.registration).all()
    return jsonify([{"id": a.id, "registration": a.registration, "aircraft_type": a.aircraft_type} for a in aircraft])


@engineer_bp.route("/inspections")
@login_required
def list_inspections():
    query = InspectionForm.query
    if current_user.is_engineer or current_user.is_shift_incharge:
        query = query.filter(
            db.or_(
                InspectionForm.primary_engineer_id == current_user.id,
                InspectionForm.second_engineer_id == current_user.id,
            )
        )
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(InspectionForm.inspection_date.desc(), InspectionForm.id.desc()).paginate(
        page=page, per_page=20, error_out=False
    )
    return render_template("engineer/inspection_list.html", pagination=pagination, inspections=pagination.items)


@engineer_bp.route("/inspections/new", methods=["GET", "POST"])
@login_required
@roles_required(UserRole.ENGINEER, UserRole.SHIFT_INCHARGE, UserRole.SUPER_ADMIN)
def create_inspection():
    form = InspectionFormForm()
    primary_exclude = form.primary_engineer_id.data or (current_user.id if (current_user.is_engineer or current_user.is_shift_incharge) else None)
    _populate_inspection_choices(form, primary_exclude_id=primary_exclude)

    if request.method == "GET":
        station = _inspection_station()
        if station:
            form.station_id.data = station.id
        if current_user.is_engineer or current_user.is_shift_incharge:
            form.primary_engineer_id.data = current_user.id
        _set_activity_selection(form)

    if form.validate_on_submit():
        station = _inspection_station()
        if not station:
            flash("The Lahore station has not been configured yet. Ask an admin to add it before logging inspections.", "danger")
            return render_template("engineer/inspection_form.html", form=form, mode="create")

        inspection = InspectionForm()
        _apply_inspection_form(inspection, form, station)

        db.session.add(inspection)
        db.session.flush()  # get inspection.id for the entries/credits below

        _save_entries(inspection, form)
        inspection.rebuild_credits()

        db.session.commit()
        log_action("CREATE", entity_type="InspectionForm", entity_id=inspection.id, description="Logged Engineer Inspection Form")

        notify_users(
            shift_incharge_ids_for(inspection.shift),
            "New Inspection Awaiting Approval",
            f"{inspection.primary_engineer.full_name if inspection.primary_engineer else 'An engineer'} submitted "
            f"an inspection for {inspection.inspection_date.strftime('%d %b %Y')}.",
            link=url_for("engineer.view_inspection", inspection_id=inspection.id),
        )

        flash("Inspection submitted for approval.", "success")
        return redirect(url_for("engineer.view_inspection", inspection_id=inspection.id))

    return render_template("engineer/inspection_form.html", form=form, mode="create")


@engineer_bp.route("/inspections/<int:inspection_id>")
@login_required
def view_inspection(inspection_id):
    inspection = InspectionForm.query.get_or_404(inspection_id)
    if current_user.is_engineer and current_user.id not in (inspection.primary_engineer_id, inspection.second_engineer_id):
        abort(403)
    return render_template("engineer/inspection_detail.html", inspection=inspection)


@engineer_bp.route("/inspections/<int:inspection_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required(UserRole.ENGINEER, UserRole.SHIFT_INCHARGE, UserRole.SUPER_ADMIN)
def edit_inspection(inspection_id):
    inspection = InspectionForm.query.get_or_404(inspection_id)

    if (current_user.is_engineer or current_user.is_shift_incharge) and inspection.primary_engineer_id != current_user.id:
        abort(403)
    if not current_user.is_super_admin and not inspection.is_editable:
        flash("This inspection has already been reviewed and can no longer be edited.", "warning")
        return redirect(url_for("engineer.view_inspection", inspection_id=inspection.id))

    form = InspectionFormForm()
    primary_exclude = inspection.primary_engineer_id
    _populate_inspection_choices(form, primary_exclude_id=primary_exclude)

    if request.method == "GET":
        form.inspection_date.data = inspection.inspection_date
        form.station_id.data = inspection.station_id
        form.shift_id.data = inspection.shift_id or 0
        form.airline_id.data = inspection.airline_id
        form.aircraft_id.data = inspection.aircraft_id
        form.primary_engineer_id.data = inspection.primary_engineer_id
        form.second_engineer_id.data = inspection.second_engineer_id or 0
        form.overall_remarks.data = inspection.overall_remarks
        # re-populate aircraft choices for the already-selected airline
        _populate_inspection_choices(form, primary_exclude_id=primary_exclude)
        aircraft_q = Aircraft.query.filter_by(is_active=True, airline_id=inspection.airline_id)
        form.aircraft_id.choices = [(0, "-- Select Aircraft --")] + [
            (a.id, a.registration) for a in aircraft_q.order_by(Aircraft.registration).all()
        ]
        _set_activity_selection(form, inspection=inspection)

    if form.validate_on_submit():
        was_rejected = inspection.approval_status == inspection.ApprovalStatus.REJECTED
        station = inspection.station or _inspection_station()
        _apply_inspection_form(inspection, form, station)

        # Fully replace the checklist and rebuild credits from scratch -
        # this is what makes edits safe against double counting.
        _save_entries(inspection, form)
        inspection.rebuild_credits()

        if was_rejected:
            inspection.approval_status = inspection.ApprovalStatus.PENDING_APPROVAL
            inspection.approval_remarks = None
            inspection.approved_by_id = None
            inspection.approved_at = None

        db.session.commit()

        if was_rejected:
            log_action("RESUBMIT", entity_type="InspectionForm", entity_id=inspection.id, description="Resubmitted inspection after rejection")
            notify_users(
                shift_incharge_ids_for(inspection.shift),
                "Inspection Resubmitted",
                f"{inspection.primary_engineer.full_name if inspection.primary_engineer else 'An engineer'} resubmitted "
                f"an inspection for {inspection.inspection_date.strftime('%d %b %Y')}.",
                link=url_for("engineer.view_inspection", inspection_id=inspection.id),
            )
            flash("Inspection resubmitted for approval.", "success")
        else:
            log_action("UPDATE", entity_type="InspectionForm", entity_id=inspection.id, description="Updated Engineer Inspection Form")
            flash("Inspection updated successfully.", "success")

        return redirect(url_for("engineer.view_inspection", inspection_id=inspection.id))

    return render_template("engineer/inspection_form.html", form=form, mode="edit", inspection=inspection)


@engineer_bp.route("/inspections/<int:inspection_id>/delete", methods=["POST"])
@login_required
@roles_required(UserRole.ENGINEER, UserRole.SHIFT_INCHARGE, UserRole.SUPER_ADMIN)
def delete_inspection(inspection_id):
    inspection = InspectionForm.query.get_or_404(inspection_id)

    if (current_user.is_engineer or current_user.is_shift_incharge) and inspection.primary_engineer_id != current_user.id:
        abort(403)
    if not current_user.is_super_admin and not inspection.is_editable:
        flash("This inspection has already been reviewed and can no longer be deleted.", "warning")
        return redirect(url_for("engineer.view_inspection", inspection_id=inspection.id))

    # Cascade deletes entries + credits (see relationship cascade config).
    db.session.delete(inspection)
    db.session.commit()
    log_action("DELETE", entity_type="InspectionForm", entity_id=inspection_id, description="Deleted Engineer Inspection Form")
    flash("Inspection deleted.", "success")
    return redirect(url_for("engineer.list_inspections"))


def _apply_inspection_form(inspection, form, station):
    inspection.inspection_date = form.inspection_date.data
    inspection.station_id = station.id
    inspection.shift_id = form.shift_id.data or None
    inspection.airline_id = form.airline_id.data
    inspection.aircraft_id = form.aircraft_id.data

    if current_user.is_super_admin and form.primary_engineer_id.data:
        inspection.primary_engineer_id = form.primary_engineer_id.data
    elif not inspection.primary_engineer_id:
        inspection.primary_engineer_id = current_user.id

    second_id = form.second_engineer_id.data or None
    inspection.second_engineer_id = second_id if second_id != inspection.primary_engineer_id else None

    inspection.overall_remarks = (form.overall_remarks.data or "").strip() or None


def _save_entries(inspection, form):
    """Replace the checklist wholesale (delete + recreate) so a form
    resubmission can never leave stale/duplicate rows behind. The redesigned
    form only ever submits a single selected activity."""
    inspection.entries = []
    db.session.flush()

    at = ActivityType(form.activity_type.data)
    inspection.entries.append(InspectionEntry(
        activity_type=at,
        performed=True,
        remarks=(form.overall_remarks.data or "").strip() or None,
    ))
