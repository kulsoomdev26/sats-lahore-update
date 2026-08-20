"""Module 4 — shared filtering & aggregation helpers for the DCE Dashboard,
Analytics and Reporting system.

Every number shown as an "official statistic" is computed live from the
`activities` table (plus its lookups) using SQLAlchemy — nothing here is
hardcoded or faked. If there is no data for a given filter, callers get
back 0 / empty lists and templates render "No data available yet."
"""
from datetime import date, datetime, timedelta

from sqlalchemy import func

from app import db
from app.models.activity import (
    Activity, ActivityType, ApprovalStatus, MaintenanceType,
    INSPECTION_TYPES, FLIGHT_COVERAGE_TYPES, MAINTENANCE_TYPES,
    TSR_TYPES, MIC_TYPES, QUALITY_TYPES,
    TRANSIT_CHECK_TYPES, CARRY_FORWARD_TYPES, DAILY_CHECK_TYPES, WEEKLY_CHECK_TYPES, DEFECT_TYPES,
    REPLACEMENT_TYPES, CF_REMOVAL_TYPES,
)
from app.models.station import Station
from app.models.shift import Shift, ShiftName
from app.models.aircraft import Aircraft, AircraftCategory
from app.models.airline import Airline
from app.models.user import User, UserRole


# --------------------------------------------------------------------------
# Filter parsing
# --------------------------------------------------------------------------
PERIOD_CHOICES = [
    ("today", "Today"),
    ("daily", "Today"),
    ("weekly", "This Week"),
    ("monthly", "This Month"),
    ("custom", "Custom Range"),
    ("all", "All Time"),
]


def parse_filters(args):
    """Read filters from a Flask `request.args`-like mapping into a plain
    dict, with sane defaults (this month) so every DCE screen opens with a
    populated, meaningful view."""
    period = (args.get("period") or "monthly").strip().lower()
    today = date.today()

    date_from = args.get("date_from", "").strip()
    date_to = args.get("date_to", "").strip()

    if period == "custom" and date_from and date_to:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d").date()
            end = datetime.strptime(date_to, "%Y-%m-%d").date()
        except ValueError:
            start, end = today.replace(day=1), today
    elif period in ("today", "daily"):
        start, end = today, today
    elif period == "weekly":
        start, end = today - timedelta(days=today.weekday()), today
    elif period == "all":
        start, end = None, None
    else:  # monthly (default)
        start, end = today.replace(day=1), today

    def _int_or_none(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "period": period if period in dict(PERIOD_CHOICES) else "monthly",
        "date_from": start,
        "date_to": end,
        "station_id": _int_or_none(args.get("station_id")),
        "shift_name": args.get("shift_name") or None,  # alpha/beta/charlie/delta
        "engineer_id": _int_or_none(args.get("engineer_id")),
        "aircraft_id": _int_or_none(args.get("aircraft_id")),
        "airline_id": _int_or_none(args.get("airline_id")),
        "activity_type": args.get("activity_type") or None,
        "status": args.get("status") or None,  # approved/pending_approval/rejected
        "category": args.get("category") or None,  # pia/third_party (Aircraft.category)
    }


def apply_filters(query, filters, *, force_approved=False):
    """Apply the parsed filter dict onto an Activity query.

    force_approved=True is used for "official statistics" tiles/charts,
    which per spec must only reflect APPROVED records regardless of any
    status filter the user has picked.
    """
    if filters.get("date_from") and filters.get("date_to"):
        query = query.filter(Activity.activity_date.between(filters["date_from"], filters["date_to"]))

    if filters.get("station_id"):
        query = query.filter(Activity.station_id == filters["station_id"])

    if filters.get("shift_name"):
        shift_ids = [sid for (sid,) in db.session.query(Shift.id).filter(Shift.name == ShiftName(filters["shift_name"])).all()]
        query = query.filter(Activity.shift_id.in_(shift_ids) if shift_ids else Activity.id == -1)

    if filters.get("engineer_id"):
        query = query.filter(Activity.logged_by_id == filters["engineer_id"])

    if filters.get("aircraft_id"):
        query = query.filter(Activity.aircraft_id == filters["aircraft_id"])

    if filters.get("airline_id"):
        ac_ids = [aid for (aid,) in db.session.query(Aircraft.id).filter(Aircraft.airline_id == filters["airline_id"]).all()]
        query = query.filter(Activity.aircraft_id.in_(ac_ids) if ac_ids else Activity.id == -1)

    if filters.get("category"):
        try:
            cat = AircraftCategory(filters["category"])
            ac_ids = [aid for (aid,) in db.session.query(Aircraft.id).filter(Aircraft.category == cat).all()]
            query = query.filter(Activity.aircraft_id.in_(ac_ids) if ac_ids else Activity.id == -1)
        except ValueError:
            pass

    if filters.get("activity_type"):
        try:
            query = query.filter(Activity.activity_type == ActivityType(filters["activity_type"]))
        except ValueError:
            pass

    if force_approved:
        query = query.filter(Activity.approval_status == ApprovalStatus.APPROVED)
    elif filters.get("status"):
        try:
            query = query.filter(Activity.approval_status == ApprovalStatus(filters["status"]))
        except ValueError:
            pass

    return query


def base_query():
    return Activity.query


# --------------------------------------------------------------------------
# Sorting helper — shared by every DCE activity-history screen so records
# can be sorted by date, station, shift, engineer, aircraft or status.
# --------------------------------------------------------------------------
SORT_CHOICES = [
    ("date_desc", "Date (Newest)"),
    ("date_asc", "Date (Oldest)"),
    ("station", "Station"),
    ("shift", "Shift"),
    ("engineer", "Engineer"),
    ("aircraft", "Aircraft"),
    ("status", "Status"),
]


def apply_sort(query, sort):
    if sort == "date_asc":
        return query.order_by(Activity.activity_date.asc())
    if sort == "station":
        return query.join(Station, Activity.station_id == Station.id, isouter=True).order_by(
            Station.code.asc(), Activity.activity_date.desc()
        )
    if sort == "shift":
        return query.join(Shift, Activity.shift_id == Shift.id, isouter=True).order_by(
            Shift.name.asc(), Activity.activity_date.desc()
        )
    if sort == "engineer":
        return query.join(User, Activity.logged_by_id == User.id, isouter=True).order_by(
            User.full_name.asc(), Activity.activity_date.desc()
        )
    if sort == "aircraft":
        return query.join(Aircraft, Activity.aircraft_id == Aircraft.id, isouter=True).order_by(
            Aircraft.registration.asc(), Activity.activity_date.desc()
        )
    if sort == "status":
        return query.order_by(Activity.approval_status.asc(), Activity.activity_date.desc())
    # default: date_desc
    return query.order_by(Activity.activity_date.desc())


# --------------------------------------------------------------------------
# Last-24-hour card analytics — powers the DCE Dashboard activity cards.
# Always computed across the last rolling 24 hours (by submission time),
# across ALL stations unless a specific station is requested.
# --------------------------------------------------------------------------
def last_24h_card_stats(types, station_id=None):
    since = datetime.utcnow() - timedelta(hours=24)

    def q():
        query = Activity.query.filter(
            Activity.activity_type.in_(types), Activity.created_at >= since
        )
        if station_id:
            query = query.filter(Activity.station_id == station_id)
        return query

    total = q().count()
    approved = q().filter(Activity.approval_status == ApprovalStatus.APPROVED).count()
    pending = q().filter(Activity.approval_status == ApprovalStatus.PENDING_APPROVAL).count()
    rejected = q().filter(Activity.approval_status == ApprovalStatus.REJECTED).count()

    stations = q().filter(Activity.station_id.isnot(None)).with_entities(
        func.count(func.distinct(Activity.station_id))
    ).scalar() or 0
    shifts = q().filter(Activity.shift_id.isnot(None)).with_entities(
        func.count(func.distinct(Activity.shift_id))
    ).scalar() or 0
    aircraft = q().filter(Activity.aircraft_id.isnot(None)).with_entities(
        func.count(func.distinct(Activity.aircraft_id))
    ).scalar() or 0
    flights = q().filter(Activity.flight_number.isnot(None)).with_entities(
        func.count(func.distinct(Activity.flight_number))
    ).scalar() or 0

    return {
        "total": total,
        "approved": approved,
        "pending": pending,
        "rejected": rejected,
        "stations": stations,
        "shifts": shifts,
        "aircraft": aircraft,
        "flights": flights,
    }


def filter_options():
    """Dropdown option lists shared by every DCE filter bar."""
    return {
        "stations": Station.query.filter_by(is_active=True).order_by(Station.name).all(),
        "shifts": list(ShiftName),
        "engineers": User.query.filter_by(role=UserRole.ENGINEER, is_active_flag=True).order_by(User.full_name).all(),
        "aircraft": Aircraft.query.filter_by(is_active=True).order_by(Aircraft.registration).all(),
        "airlines": Airline.query.filter_by(is_active=True).order_by(Airline.name).all(),
        "activity_types": list(ActivityType),
        "statuses": list(ApprovalStatus),
    }


# --------------------------------------------------------------------------
# KPI computation
# --------------------------------------------------------------------------
def compute_kpis(filters):
    """All headline KPI tiles for the DCE Dashboard. Approved-only per the
    'official statistics' rule; pending is the one exception (it is the
    approval queue, which is inherently non-approved)."""
    approved_q = apply_filters(base_query(), filters, force_approved=True)
    pending_q = apply_filters(base_query(), {**filters, "status": None}, force_approved=False).filter(
        Activity.approval_status == ApprovalStatus.PENDING_APPROVAL
    )

    def count_in(types):
        return approved_q.filter(Activity.activity_type.in_(types)).count()

    aircraft_inspected = (
        db.session.query(func.count(func.distinct(Activity.aircraft_id)))
        .select_from(Activity)
    )
    aircraft_inspected = apply_filters(aircraft_inspected, filters, force_approved=True).filter(
        Activity.aircraft_id.isnot(None)
    ).scalar() or 0

    pia_inspections = approved_q.join(Aircraft, Activity.aircraft_id == Aircraft.id).filter(
        Activity.activity_type.in_(INSPECTION_TYPES), Aircraft.category == AircraftCategory.PIA
    ).count()
    third_party_inspections = approved_q.join(Aircraft, Activity.aircraft_id == Aircraft.id).filter(
        Activity.activity_type.in_(INSPECTION_TYPES), Aircraft.category == AircraftCategory.THIRD_PARTY
    ).count()

    kpis = {
        "pending_approvals": pending_q.count(),
        "aircraft_inspected": aircraft_inspected,
        "pia_inspections": pia_inspections,
        "third_party_inspections": third_party_inspections,
        "maintenance": count_in(MAINTENANCE_TYPES),
        "maintenance_check": count_in((ActivityType.MAINTENANCE_CHECK,)),
        "flight_coverage": count_in(FLIGHT_COVERAGE_TYPES),
        "tsr": count_in(TSR_TYPES),
        "mic": count_in(MIC_TYPES),
        "replacement": count_in(REPLACEMENT_TYPES),
        "quality_ri": count_in(QUALITY_TYPES),
        "qari": count_in(QUALITY_TYPES),
        "cf_removal": count_in(CF_REMOVAL_TYPES),
        "cf": count_in((ActivityType.CF,)),
        "pirep_unscheduled": count_in(DEFECT_TYPES),
        "carry_forward": count_in(CARRY_FORWARD_TYPES) or approved_q.filter(
            Activity.maintenance_type == MaintenanceType.CARRY_FORWARD
        ).count(),
        "unscheduled_maintenance": count_in(DEFECT_TYPES) or approved_q.filter(
            Activity.maintenance_type == MaintenanceType.UNSCHEDULED
        ).count(),
    }
    return kpis


# --------------------------------------------------------------------------
# Chart datasets (Chart.js friendly {labels, data} / {labels, datasets})
# --------------------------------------------------------------------------
def daily_trend(filters, days=30):
    end = filters.get("date_to") or date.today()
    start = filters.get("date_from") or (end - timedelta(days=days - 1))
    q = apply_filters(base_query(), {**filters, "date_from": start, "date_to": end}, force_approved=True)
    rows = (
        q.with_entities(Activity.activity_date, func.count(Activity.id))
        .group_by(Activity.activity_date)
        .order_by(Activity.activity_date)
        .all()
    )
    counts = {d: c for d, c in rows}
    labels, data = [], []
    cur = start
    while cur <= end:
        labels.append(cur.strftime("%d %b"))
        data.append(counts.get(cur, 0))
        cur += timedelta(days=1)
    return {"labels": labels, "data": data}


def _group_by_month(dates, months):
    """Dialect-agnostic month bucketing (avoids DB-specific date functions
    so this works identically on SQLite dev and PostgreSQL production)."""
    counts = {}
    for d in dates:
        if d is None:
            continue
        key = d.strftime("%Y-%m")
        counts[key] = counts.get(key, 0) + 1
    ordered_keys = sorted(counts.keys())[-months:]
    labels = [datetime.strptime(k, "%Y-%m").strftime("%b %Y") for k in ordered_keys]
    return {"labels": labels, "data": [counts[k] for k in ordered_keys]}


def monthly_trend(filters, months=12):
    q = apply_filters(base_query(), {**filters, "date_from": None, "date_to": None}, force_approved=True)
    dates = [d for (d,) in q.with_entities(Activity.activity_date).all()]
    return _group_by_month(dates, months)


def shift_comparison(filters):
    q = apply_filters(base_query(), {**filters, "shift_name": None}, force_approved=True)
    rows = (
        q.join(Shift, Activity.shift_id == Shift.id)
        .with_entities(Shift.name, func.count(Activity.id))
        .group_by(Shift.name)
        .all()
    )
    counts = {name: c for name, c in rows}
    order = list(ShiftName)
    return {
        "labels": [n.label for n in order],
        "data": [counts.get(n, 0) for n in order],
    }


def station_comparison(filters):
    q = apply_filters(base_query(), {**filters, "station_id": None}, force_approved=True)
    rows = (
        q.join(Station, Activity.station_id == Station.id)
        .with_entities(Station.code, func.count(Activity.id))
        .group_by(Station.code)
        .order_by(Station.code)
        .all()
    )
    return {"labels": [r[0] for r in rows], "data": [r[1] for r in rows]}


def aircraft_activity(filters, limit=10):
    q = apply_filters(base_query(), {**filters, "aircraft_id": None}, force_approved=True)
    rows = (
        q.join(Aircraft, Activity.aircraft_id == Aircraft.id)
        .with_entities(Aircraft.registration, func.count(Activity.id))
        .group_by(Aircraft.registration)
        .order_by(func.count(Activity.id).desc())
        .limit(limit)
        .all()
    )
    return {"labels": [r[0] for r in rows], "data": [r[1] for r in rows]}


def airline_coverage(filters):
    q = apply_filters(base_query(), {**filters, "airline_id": None}, force_approved=True)
    rows = (
        q.join(Aircraft, Activity.aircraft_id == Aircraft.id)
        .join(Airline, Aircraft.airline_id == Airline.id)
        .with_entities(Airline.name, func.count(Activity.id))
        .group_by(Airline.name)
        .order_by(func.count(Activity.id).desc())
        .all()
    )
    return {"labels": [r[0] for r in rows], "data": [r[1] for r in rows]}


def pia_vs_third_party(filters):
    q = apply_filters(base_query(), filters, force_approved=True).join(
        Aircraft, Activity.aircraft_id == Aircraft.id
    ).filter(Activity.activity_type.in_(INSPECTION_TYPES))
    rows = q.with_entities(Aircraft.category, func.count(Activity.id)).group_by(Aircraft.category).all()
    counts = {c: n for c, n in rows}
    return {
        "labels": ["PIA", "Third Party"],
        "data": [counts.get(AircraftCategory.PIA, 0), counts.get(AircraftCategory.THIRD_PARTY, 0)],
    }


def maintenance_breakdown(filters):
    q = apply_filters(base_query(), filters, force_approved=True).filter(
        Activity.activity_type.in_(MAINTENANCE_TYPES)
    )
    rows = q.with_entities(Activity.activity_type, func.count(Activity.id)).group_by(Activity.activity_type).all()
    return {"labels": [t.label for t, _ in rows], "data": [c for _, c in rows]}


def scheduled_unscheduled_carry_forward(filters):
    q = apply_filters(base_query(), filters, force_approved=True).filter(Activity.maintenance_type.isnot(None))
    rows = q.with_entities(Activity.maintenance_type, func.count(Activity.id)).group_by(Activity.maintenance_type).all()
    counts = {t: c for t, c in rows}
    order = [MaintenanceType.SCHEDULED, MaintenanceType.UNSCHEDULED, MaintenanceType.CARRY_FORWARD]
    return {"labels": [t.label for t in order], "data": [counts.get(t, 0) for t in order]}


def engineer_performance_chart(filters, limit=10):
    q = apply_filters(base_query(), {**filters, "engineer_id": None}, force_approved=True)
    rows = (
        q.join(User, Activity.logged_by_id == User.id)
        .with_entities(User.full_name, func.count(Activity.id))
        .group_by(User.full_name)
        .order_by(func.count(Activity.id).desc())
        .limit(limit)
        .all()
    )
    return {"labels": [r[0] for r in rows], "data": [r[1] for r in rows]}


def quality_ri_trend(filters, months=6):
    q = apply_filters(base_query(), {**filters, "date_from": None, "date_to": None}, force_approved=True).filter(
        Activity.activity_type.in_(QUALITY_TYPES)
    )
    dates = [d for (d,) in q.with_entities(Activity.activity_date).all()]
    return _group_by_month(dates, months)


# --------------------------------------------------------------------------
# Tabular report builders
# --------------------------------------------------------------------------
def station_summary(filters):
    stations = Station.query.filter_by(is_active=True).order_by(Station.name).all()
    out = []
    for st in stations:
        f = {**filters, "station_id": st.id}
        approved_q = apply_filters(base_query(), f, force_approved=True)
        all_q = apply_filters(base_query(), {**f, "status": None}, force_approved=False)
        pending = all_q.filter(Activity.approval_status == ApprovalStatus.PENDING_APPROVAL).count()
        out.append({
            "station": st,
            "activities": approved_q.count(),
            "aircraft_inspected": apply_filters(base_query(), f, force_approved=True).filter(
                Activity.aircraft_id.isnot(None)
            ).with_entities(func.count(func.distinct(Activity.aircraft_id))).scalar() or 0,
            "maintenance_check": approved_q.filter(Activity.activity_type == ActivityType.MAINTENANCE_CHECK).count(),
            "tsr": approved_q.filter(Activity.activity_type.in_(TSR_TYPES)).count(),
            "mic": approved_q.filter(Activity.activity_type.in_(MIC_TYPES)).count(),
            "replacement": approved_q.filter(Activity.activity_type.in_(REPLACEMENT_TYPES)).count(),
            "ri": approved_q.filter(Activity.activity_type.in_(QUALITY_TYPES)).count(),
            "unscheduled": approved_q.filter(Activity.activity_type.in_(DEFECT_TYPES)).count(),
            "cf_removal": approved_q.filter(Activity.activity_type == ActivityType.CF_REMOVAL).count(),
            "cf": approved_q.filter(Activity.activity_type == ActivityType.CF).count(),
            "approved": approved_q.count(),
            "pending": pending,
        })
    return out


def shift_summary(filters):
    out = []
    for name in ShiftName:
        f = {**filters, "shift_name": name.value}
        approved_q = apply_filters(base_query(), f, force_approved=True)
        all_q = apply_filters(base_query(), {**f, "status": None}, force_approved=False)
        out.append({
            "shift": name,
            "activities": all_q.count(),
            "maintenance_check": approved_q.filter(Activity.activity_type == ActivityType.MAINTENANCE_CHECK).count(),
            "tsr": approved_q.filter(Activity.activity_type.in_(TSR_TYPES)).count(),
            "mic": approved_q.filter(Activity.activity_type.in_(MIC_TYPES)).count(),
            "ri": approved_q.filter(Activity.activity_type.in_(QUALITY_TYPES)).count(),
            "approved": approved_q.count(),
            "rejected": all_q.filter(Activity.approval_status == ApprovalStatus.REJECTED).count(),
            "pending": all_q.filter(Activity.approval_status == ApprovalStatus.PENDING_APPROVAL).count(),
        })
    return out


def engineer_performance(filters):
    engineers = User.query.filter_by(role=UserRole.ENGINEER, is_active_flag=True).order_by(User.full_name).all()
    out = []
    for eng in engineers:
        f = {**filters, "engineer_id": eng.id}
        all_q = apply_filters(base_query(), {**f, "status": None}, force_approved=False)
        approved_q = apply_filters(base_query(), f, force_approved=True)
        total = all_q.count()
        if total == 0:
            continue
        out.append({
            "engineer": eng,
            "activities": total,
            "maintenance_check": approved_q.filter(Activity.activity_type == ActivityType.MAINTENANCE_CHECK).count(),
            "tsr": approved_q.filter(Activity.activity_type.in_(TSR_TYPES)).count(),
            "mic": approved_q.filter(Activity.activity_type.in_(MIC_TYPES)).count(),
            "replacement": approved_q.filter(Activity.activity_type.in_(REPLACEMENT_TYPES)).count(),
            "ri": approved_q.filter(Activity.activity_type.in_(QUALITY_TYPES)).count(),
            "approved": approved_q.count(),
            "rejected": all_q.filter(Activity.approval_status == ApprovalStatus.REJECTED).count(),
            "pending": all_q.filter(Activity.approval_status == ApprovalStatus.PENDING_APPROVAL).count(),
        })
    out.sort(key=lambda r: r["activities"], reverse=True)
    return out


def aircraft_report(filters):
    aircraft = Aircraft.query.filter_by(is_active=True).order_by(Aircraft.registration).all()
    out = []
    for ac in aircraft:
        f = {**filters, "aircraft_id": ac.id}
        approved_q = apply_filters(base_query(), f, force_approved=True)
        total = approved_q.count()
        if total == 0:
            continue
        out.append({
            "aircraft": ac,
            "maintenance_check": approved_q.filter(Activity.activity_type == ActivityType.MAINTENANCE_CHECK).count(),
            "tsr": approved_q.filter(Activity.activity_type.in_(TSR_TYPES)).count(),
            "mic": approved_q.filter(Activity.activity_type.in_(MIC_TYPES)).count(),
            "replacement": approved_q.filter(Activity.activity_type.in_(REPLACEMENT_TYPES)).count(),
            "ri": approved_q.filter(Activity.activity_type.in_(QUALITY_TYPES)).count(),
        })
    out.sort(key=lambda r: r["maintenance_check"], reverse=True)
    return out


def flight_report(filters, sort="date_desc"):
    q = apply_filters(base_query(), {**filters, "status": filters.get("status")}, force_approved=False).filter(
        Activity.activity_type.in_(FLIGHT_COVERAGE_TYPES)
    )
    rows = apply_sort(q, sort).limit(1000).all()
    return rows


def maintenance_report(filters, sort="date_desc"):
    q = apply_filters(base_query(), {**filters, "status": filters.get("status")}, force_approved=False).filter(
        Activity.activity_type.in_(MAINTENANCE_TYPES)
    )
    rows = apply_sort(q, sort).limit(1000).all()
    return rows


OTHER_REPORT_TYPES = {
    "daily_activity": {"label": "Daily Activity", "types": None},
    "monthly_activity": {"label": "Monthly Activity", "types": None},
    "inspections": {"label": "Aircraft Inspection", "types": INSPECTION_TYPES},
    # "aircraft_inspected" mirrors compute_kpis()'s aircraft_inspected KPI:
    # any activity type, as long as an aircraft is attached -- NOT limited
    # to INSPECTION_TYPES like the "inspections" key above.
    "aircraft_inspected": {"label": "Aircraft Inspected", "types": None, "require_aircraft": True},
    "maintenance_check": {"label": "Maintenance Check", "types": (ActivityType.MAINTENANCE_CHECK,)},
    "tsr": {"label": "TSR", "types": TSR_TYPES},
    "mic": {"label": "MIC / Scheduled Maintenance", "types": MIC_TYPES},
    "replacement": {"label": "Replacement", "types": REPLACEMENT_TYPES},
    "quality_ri": {"label": "QARI", "types": QUALITY_TYPES},
    "cf": {"label": "CF", "types": (ActivityType.CF,)},
    "cf_removal": {"label": "CF Removal", "types": CF_REMOVAL_TYPES},
    "pirep_unscheduled_maintenance": {"label": "PIREP / Unscheduled Maintenance", "types": DEFECT_TYPES},
}


def other_report(report_key, filters, sort="date_desc"):
    cfg = OTHER_REPORT_TYPES.get(report_key)
    if not cfg:
        return [], cfg
    q = apply_filters(base_query(), {**filters, "status": filters.get("status")}, force_approved=False)
    if cfg["types"]:
        q = q.filter(Activity.activity_type.in_(cfg["types"]))
    if cfg.get("require_aircraft"):
        q = q.filter(Activity.aircraft_id.isnot(None))
    rows = apply_sort(q, sort).limit(1000).all()
    return rows, cfg
