from datetime import datetime
from flask_login import current_user

from app import db


def register_template_helpers(app):
    @app.context_processor
    def inject_globals():
        unread_count = 0
        pending_approval_count = 0
        pending_inspection_approval_count = 0
        if current_user and getattr(current_user, "is_authenticated", False):
            try:
                from app.models.notification import Notification
                unread_count = Notification.query.filter_by(
                    user_id=current_user.id, is_read=False
                ).count()
            except Exception:
                # A failed statement aborts the whole Postgres transaction --
                # every later query on this pooled connection (including the
                # view's own queries and any lazy-loads Jinja triggers while
                # rendering) would otherwise raise InFailedSqlTransaction
                # until this is rolled back.
                db.session.rollback()
                unread_count = 0

            try:
                # Approval counters are only meaningful for roles that can
                # actually approve/reject (Super Admin). Shift Incharge no
                # longer has approval authority, so this is intentionally
                # not computed for them any more.
                if getattr(current_user, "is_super_admin", False):
                    from app.models.activity import Activity, ApprovalStatus
                    q = Activity.query.filter(Activity.approval_status == ApprovalStatus.PENDING_APPROVAL)
                    pending_approval_count = q.count()

                    from app.models.inspection import InspectionForm
                    iq = InspectionForm.query.filter(InspectionForm.approval_status == ApprovalStatus.PENDING_APPROVAL)
                    pending_inspection_approval_count = iq.count()
            except Exception:
                # Same reasoning as above: roll back so this failure can't
                # abort every other query for the rest of the request.
                db.session.rollback()
                pending_approval_count = 0
                pending_inspection_approval_count = 0

        return {
            "unread_notification_count": unread_count,
            "pending_approval_count": pending_approval_count,
            "pending_inspection_approval_count": pending_inspection_approval_count,
            "app_name": "SATS",
            "app_full_name": "Station Activity Tracking System",
            "org_name": "PIA Engineering",
            "current_year": datetime.utcnow().year,
        }

    @app.template_filter("dt")
    def format_datetime(value, fmt="%d %b %Y, %I:%M %p"):
        if not value:
            return "-"
        return value.strftime(fmt)

    @app.template_filter("role_label")
    def role_label(role):
        if role is None:
            return "-"
        return role.label if hasattr(role, "label") else str(role)

    @app.template_filter("enum_label")
    def enum_label(value):
        if value is None:
            return "-"
        return value.label if hasattr(value, "label") else str(value)

    @app.template_filter("activity_type_label")
    def activity_type_label(value):
        """Accepts either an ActivityType enum member or its raw string
        value (as used by the inspection checklist's HiddenField)."""
        if value is None:
            return "-"
        from app.models.activity import ActivityType
        if hasattr(value, "label"):
            return value.label
        try:
            return ActivityType(value).label
        except ValueError:
            return str(value)

    @app.template_filter("badge_class")
    def badge_class(value):
        if value is None:
            return "badge-role"
        return value.badge_class if hasattr(value, "badge_class") else "badge-role"

    @app.template_filter("time12")
    def time12(value):
        if not value:
            return "-"
        return value.strftime("%I:%M %p")

    @app.template_filter("date_fmt")
    def date_fmt(value, fmt="%d %b %Y"):
        if not value:
            return "-"
        return value.strftime(fmt)
