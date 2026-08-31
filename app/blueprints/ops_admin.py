"""Ops depth (Pass 4): schedule builder + instance overrides, trainer
management with conflicts & substitutions, member directory + notes + flags,
reporting + CSV exports, announcements, review CRUD.

Mounted at /ops alongside the Today view. Dense and fast over decorative,
per the design brief.
"""
import csv
import io
import logging
from collections import defaultdict
from datetime import date, time, timedelta
from functools import wraps

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (
    Announcement,
    AttendeeProfile,
    Booking,
    BookingStatus,
    Call,
    ClassInstance,
    ClassType,
    ClosureDate,
    InstanceStatus,
    Lead,
    MemberNote,
    Payment,
    Review,
    Role,
    ScheduleTemplate,
    SiteSetting,
    Subscription,
    SubscriptionStatus,
    Trainer,
    User,
    WaitlistEntry,
    utcnow,
)
from ..services.class_admin import (
    cancel_class as _cancel_class,
    prune_inactive_template_instances,
    remove_future_instances as _remove_future_instances,
)
from ..services.messaging import send_email, send_sms
from ..services.scheduling import booked_counts, generate_instances
from ..services.tzutil import fmt_local, now_utc, today_local
from .ops import STAFF_ROLES, desk_required as staff_required

bp = Blueprint("ops_admin", __name__)
log = logging.getLogger(__name__)

ADMIN_ROLES = {Role.gym_admin.value, Role.agency_admin.value}


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if current_user.role not in ADMIN_ROLES:
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


def _cid() -> int:
    return current_user.client_account_id


# ----------------------------------------------------------- live schedule ---
@bp.get("/schedule")
@staff_required
def live_schedule():
    """The ACTUAL schedule: active classes only (active class types, active
    templates, scheduled status), active coaches only. Building/editing lives
    in the separate Builder tab."""
    from ..services.scheduling import upcoming_instances

    occurrences = upcoming_instances(_cid(), days=14)
    by_day: dict = {}
    for o in occurrences:
        by_day.setdefault(o["instance"].local_date, []).append(o)
    trainers = (
        db.session.query(Trainer)
        .filter_by(client_account_id=_cid(), active=True)
        .all()
    )
    return render_template(
        "ops/live_schedule.html", by_day=by_day, trainers=trainers
    )


# ------------------------------------------------------- schedule builder ---
@bp.route("/schedule-builder", methods=["GET", "POST"])
@staff_required
def schedule_builder():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_template":
            trainer_id = request.form.get("trainer_id", type=int) or None
            # Multi-day add: checkboxes send weekdays[]; single "weekday"
            # kept as a fallback.
            weekdays = request.form.getlist("weekdays", type=int)
            if not weekdays:
                single = request.form.get("weekday", type=int)
                weekdays = [single] if single is not None else []
            start = time.fromisoformat(request.form.get("start_time"))
            day_names = [
                "Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday",
            ]
            if not weekdays:
                flash("Pick at least one day of the week.", "error")
                return redirect(url_for("ops_admin.schedule_builder"))

            new_ids, skipped = [], []
            for wd in sorted(set(weekdays)):
                conflict = _trainer_conflict(trainer_id, wd, start)
                if conflict:
                    skipped.append(f"{day_names[wd]} (coach already leads {conflict})")
                    continue
                tpl = ScheduleTemplate(
                    client_account_id=_cid(),
                    class_type_id=request.form.get("class_type_id", type=int),
                    cohort_label=(request.form.get("cohort") or "").strip() or None,
                    weekday=wd,
                    start_time_local=start,
                    capacity=request.form.get("capacity", type=int) or None,
                    trainer_id=trainer_id,
                    active=True,
                )
                db.session.add(tpl)
                db.session.flush()
                new_ids.append(tpl.id)
            db.session.commit()
            if new_ids:
                generate_instances(_cid())
                db.session.commit()
                placed = (
                    db.session.query(ClassInstance)
                    .filter(ClassInstance.template_id.in_(new_ids))
                    .order_by(ClassInstance.local_date)
                    .all()
                )
                first = db.session.get(ScheduleTemplate, new_ids[0])
                if trainer_id:
                    _notify_coach_assignment(
                        trainer_id,
                        [
                            f"{first.class_type.name}: every "
                            f"{day_names[db.session.get(ScheduleTemplate, i).weekday]}"
                            f" at {start.strftime('%I:%M %p').lstrip('0')}"
                            for i in new_ids
                        ],
                    )
                    db.session.commit()
                added_days = ", ".join(
                    day_names[db.session.get(ScheduleTemplate, i).weekday]
                    for i in new_ids
                )
                msg = (
                    f"{first.class_type.name} now repeats every {added_days} at "
                    f"{start.strftime('%I:%M %p').lstrip('0')} — {len(placed)} "
                    "classes placed on the calendar, and future weeks keep "
                    "generating automatically."
                )
                if skipped:
                    msg += " Skipped: " + "; ".join(skipped) + "."
                flash(msg, "success")
            elif skipped:
                flash("Nothing added — " + "; ".join(skipped) + ".", "error")
        elif action == "bulk_assign":
            _bulk_assign_coach()
        elif action == "save_type":
            _save_class_type()
        elif action == "save_template":
            _save_template()
        elif action == "toggle_template":
            tpl = db.session.get(
                ScheduleTemplate, request.form.get("template_id", type=int)
            )
            if tpl and tpl.client_account_id == _cid():
                tpl.active = not tpl.active
                if tpl.active:
                    db.session.commit()
                    generate_instances(_cid())
                    db.session.commit()
                    flash("Template reactivated — classes are back on the schedule.", "success")
                else:
                    removed, cancelled = _remove_future_instances(tpl.id)
                    db.session.commit()
                    msg = f"Template deactivated — {removed} upcoming classes removed from the schedule."
                    if cancelled:
                        msg += f" {cancelled} had bookings: members were notified and offered the nearest class."
                    flash(msg, "success")
        elif action == "add_closure":
            db.session.add(
                ClosureDate(
                    client_account_id=_cid(),
                    closed_on=date.fromisoformat(request.form.get("closed_on")),
                    reason=(request.form.get("reason") or "").strip() or None,
                )
            )
            # remove already-generated instances on that day (no bookings assumed
            # for future closures; booked ones must be cancelled explicitly)
            db.session.commit()
            flash("Closure added — future generation skips this date.", "success")
        elif action == "generate":
            created = generate_instances(_cid())
            pruned = prune_inactive_template_instances(_cid())
            db.session.commit()
            msg = f"{created} instances generated."
            if pruned:
                msg += f" {pruned} stale classes of deactivated templates removed."
            flash(msg, "success")
        return redirect(url_for("ops_admin.schedule_builder"))

    templates = (
        db.session.query(ScheduleTemplate)
        .filter_by(client_account_id=_cid())
        .order_by(ScheduleTemplate.weekday, ScheduleTemplate.start_time_local)
        .all()
    )
    class_types = (
        db.session.query(ClassType).filter_by(client_account_id=_cid(), active=True).all()
    )
    all_class_types = (
        db.session.query(ClassType)
        .filter_by(client_account_id=_cid())
        .order_by(ClassType.name)
        .all()
    )
    trainers = (
        db.session.query(Trainer).filter_by(client_account_id=_cid(), active=True).all()
    )
    closures = (
        db.session.query(ClosureDate)
        .filter(ClosureDate.client_account_id == _cid(), ClosureDate.closed_on >= today_local())
        .order_by(ClosureDate.closed_on)
        .all()
    )
    from sqlalchemy import or_

    instances = (
        db.session.query(ClassInstance)
        .outerjoin(ScheduleTemplate, ClassInstance.template_id == ScheduleTemplate.id)
        .filter(
            ClassInstance.client_account_id == _cid(),
            ClassInstance.local_date >= today_local(),
            ClassInstance.local_date <= today_local() + timedelta(days=14),
            ClassInstance.status == InstanceStatus.scheduled.value,
            or_(
                ClassInstance.template_id.is_(None),
                ScheduleTemplate.active.is_(True),
            ),
        )
        .order_by(ClassInstance.local_date, ClassInstance.local_time)
        .all()
    )
    counts = booked_counts([i.id for i in instances])
    return render_template(
        "ops/schedule_builder.html",
        templates=templates,
        class_types=class_types,
        all_class_types=all_class_types,
        trainers=trainers,
        closures=closures,
        instances=instances,
        counts=counts,
        weekdays=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        segment_choices=[
            ("", "None (portal/schedule only)"),
            ("kids", "Kids 6-10 (/kids)"),
            ("youth", "Youth 11-18 (/youth)"),
            ("technical", "Technical Boxing (/technical)"),
            ("bootcamp", "Boxing Bootcamp (/bootcamp)"),
            ("shehits", "She Hits (/shehits)"),
            ("beast", "Beast Camp (/beast)"),
        ],
    )


def _notify_coach_assignment(trainer_id: int | None, lines: list[str]) -> None:
    """Email a coach their new assignment(s). Silently skips coaches without
    an email on file."""
    trainer = db.session.get(Trainer, trainer_id) if trainer_id else None
    if trainer is None or not trainer.email or not lines:
        return
    html = render_template(
        "emails/coach_assignment.html", trainer=trainer, lines=lines
    )
    send_email(
        None, trainer.email,
        f"You're on the schedule — {len(lines)} assignment"
        + ("s" if len(lines) != 1 else ""),
        html, "coach_assignment", _cid(),
    )


def _bulk_assign_coach() -> None:
    """Assign one coach across many weekly classes at once. Scope: every
    class, only unassigned classes, or all rows of one class type. Overlap
    conflicts are skipped and reported. Future occurrences follow; booked
    members are notified when their day's coach actually changes."""
    trainer_id = request.form.get("bulk_trainer_id", type=int)
    scope = request.form.get("scope", "unassigned")
    trainer = db.session.get(Trainer, trainer_id) if trainer_id else None
    if trainer is None or trainer.client_account_id != _cid() or not trainer.active:
        flash("Pick an active coach to assign.", "error")
        return

    q = db.session.query(ScheduleTemplate).filter_by(
        client_account_id=_cid(), active=True
    )
    if scope == "unassigned":
        q = q.filter(ScheduleTemplate.trainer_id.is_(None))
    elif scope.startswith("type:"):
        q = q.filter(ScheduleTemplate.class_type_id == int(scope.split(":")[1]))
    # scope == "all": no extra filter

    assigned, skipped, _bulk_lines = 0, [], []
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for tpl in q.all():
        if tpl.trainer_id == trainer.id:
            continue
        conflict = _trainer_conflict(
            trainer.id, tpl.weekday, tpl.start_time_local, exclude_template_id=tpl.id
        )
        if conflict:
            skipped.append(
                f"{tpl.class_type.name} {day_names[tpl.weekday]} "
                f"{tpl.start_time_local.strftime('%I:%M %p').lstrip('0')} "
                f"(overlaps {conflict})"
            )
            continue
        tpl.trainer_id = trainer.id
        db.session.flush()
        for inst in db.session.query(ClassInstance).filter(
            ClassInstance.template_id == tpl.id,
            ClassInstance.local_date >= today_local(),
            ClassInstance.status == InstanceStatus.scheduled.value,
        ):
            old_id = inst.trainer_id
            inst.trainer_id = trainer.id
            if old_id not in (None, trainer.id) and any(
                b.status == BookingStatus.booked.value for b in inst.bookings
            ):
                _log_substitution(inst, old_id, trainer.id)
                _notify_substitution(inst)
        assigned += 1
        _bulk_lines.append(
            f"{tpl.class_type.name}: every "
            f"{['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][tpl.weekday]}"
            f" at {tpl.start_time_local.strftime('%I:%M %p').lstrip('0')}"
        )
    _notify_coach_assignment(trainer.id, _bulk_lines)
    db.session.commit()
    msg = f"{trainer.name} assigned to {assigned} weekly classes."
    if skipped:
        msg += " Skipped (time overlaps): " + "; ".join(skipped) + "."
    flash(msg, "success" if assigned else "error")


def _save_class_type() -> None:
    """Create or edit a class in the catalog. Deactivating a class also
    deactivates its weekly slots and clears its future occurrences."""
    import re as _re

    tid = request.form.get("type_id", type=int)
    ct = db.session.get(ClassType, tid) if tid else None
    if ct is not None and ct.client_account_id != _cid():
        abort(404)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("The class needs a name.", "error")
        return
    if ct is None:
        base = _re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "class"
        key, n = base, 2
        while (
            db.session.query(ClassType)
            .filter_by(client_account_id=_cid(), key=key)
            .first()
        ):
            key, n = f"{base}_{n}", n + 1
        ct = ClassType(client_account_id=_cid(), key=key)
        db.session.add(ct)
    ct.name = name
    ct.age_min = request.form.get("age_min", type=int)
    ct.age_max = request.form.get("age_max", type=int)
    ct.duration_min = request.form.get("duration_min", type=int) or 45
    ct.default_capacity = request.form.get("default_capacity", type=int) or 12
    ct.segment_tag = (request.form.get("segment_tag") or "").strip() or None
    ct.accepts_trials = request.form.get("accepts_trials") == "on"
    was_active = ct.active if tid else True
    ct.active = request.form.get("active") == "on"
    db.session.flush()

    removed = 0
    if was_active and not ct.active:
        for tpl in db.session.query(ScheduleTemplate).filter_by(
            client_account_id=_cid(), class_type_id=ct.id, active=True
        ):
            tpl.active = False
            r, _c = _remove_future_instances(tpl.id)
            removed += r
    db.session.commit()
    if removed:
        flash(
            f"{ct.name} saved and deactivated — {removed} upcoming classes "
            "removed from the schedule.",
            "success",
        )
    elif tid is None:
        flash(
            f'"{ct.name}" added to the class catalog — now place it on the '
            "schedule below.",
            "success",
        )
    else:
        flash(f"{ct.name} saved.", "success")


def _save_template() -> None:
    """Edit a weekly class: day, time, group, capacity, coach. Moving day or
    time MOVES future occurrences with bookings intact and notifies booked
    members; a coach change sends the substitution notice."""
    from datetime import time as _time

    from ..services.class_admin import reschedule_template

    tpl = db.session.get(ScheduleTemplate, request.form.get("template_id", type=int))
    if tpl is None or tpl.client_account_id != _cid():
        abort(404)
    new_weekday = request.form.get("weekday", type=int)
    new_start = _time.fromisoformat(request.form.get("start_time"))
    new_trainer_id = request.form.get("trainer_id", type=int) or None
    new_cohort = (request.form.get("cohort") or "").strip() or None
    new_capacity = request.form.get("capacity", type=int) or None

    conflict = _trainer_conflict(
        new_trainer_id, new_weekday, new_start, exclude_template_id=tpl.id
    )
    if conflict:
        flash(
            f"Not saved — that coach already leads {conflict} at an "
            "overlapping time.",
            "error",
        )
        return

    moved = reschedule_template(tpl, new_weekday, new_start)

    trainer_changed = new_trainer_id != tpl.trainer_id
    if trainer_changed and new_trainer_id:
        _notify_coach_assignment(
            new_trainer_id,
            [
                f"{tpl.class_type.name}: every "
                f"{['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][new_weekday]}"
                f" at {new_start.strftime('%I:%M %p').lstrip('0')}"
            ],
        )
    tpl.cohort_label = new_cohort
    tpl.capacity = new_capacity
    tpl.trainer_id = new_trainer_id
    future = (
        db.session.query(ClassInstance)
        .filter(
            ClassInstance.template_id == tpl.id,
            ClassInstance.local_date >= today_local(),
            ClassInstance.status == InstanceStatus.scheduled.value,
        )
        .all()
    )
    for inst in future:
        inst.cohort_label = new_cohort
        if new_capacity:
            inst.capacity = new_capacity
        if trainer_changed:
            old_id = inst.trainer_id
            inst.trainer_id = new_trainer_id
            if any(b.status == BookingStatus.booked.value for b in inst.bookings):
                _log_substitution(inst, old_id, new_trainer_id)
                _notify_substitution(inst)
    db.session.commit()
    generate_instances(_cid())
    db.session.commit()
    msg = f"{tpl.class_type.name} saved."
    if moved:
        msg += (
            f" {moved} upcoming classes moved to the new day/time — booked "
            "members were notified and their bookings moved with them."
        )
    flash(msg, "success")


def _trainer_conflict(
    trainer_id: int | None, weekday: int, start: time, exclude_template_id: int | None = None
) -> str | None:
    if trainer_id is None:
        return None
    for tpl in (
        db.session.query(ScheduleTemplate)
        .filter_by(client_account_id=_cid(), trainer_id=trainer_id, weekday=weekday, active=True)
        .all()
    ):
        if tpl.id == exclude_template_id:
            continue
        dur = tpl.duration_min or tpl.class_type.duration_min
        t0 = tpl.start_time_local
        end_min = t0.hour * 60 + t0.minute + dur
        new_min = start.hour * 60 + start.minute
        if t0.hour * 60 + t0.minute <= new_min < end_min or (
            new_min <= t0.hour * 60 + t0.minute < new_min + dur
        ):
            return tpl.class_type.name
    return None


@bp.post("/instances/<int:instance_id>/coach")
@staff_required
def instance_coach(instance_id: int):
    """Coach swap from the Schedule tab. scope=one changes THIS occurrence
    only (the weekly default stays); scope=all makes the coach the weekly
    default — this weekday from this date forward, template included, so
    newly generated weeks inherit it. Coach TBA removes with either scope.
    Booked members of every changed occurrence are notified."""
    inst = db.session.get(ClassInstance, instance_id) or abort(404)
    if inst.client_account_id != _cid():
        abort(404)
    new_id = request.form.get("trainer_id", type=int) or None
    scope_all = request.form.get("scope") == "all" and inst.template_id
    if new_id == inst.trainer_id and not scope_all:
        return redirect(url_for("ops_admin.live_schedule"))

    targets = [inst]
    if scope_all:
        tpl = db.session.get(ScheduleTemplate, inst.template_id)
        tpl.trainer_id = new_id
        targets = (
            db.session.query(ClassInstance)
            .filter(
                ClassInstance.template_id == inst.template_id,
                ClassInstance.local_date >= inst.local_date,
                ClassInstance.status == InstanceStatus.scheduled.value,
            )
            .order_by(ClassInstance.local_date)
            .all()
        ) or [inst]

    notified = 0
    for t in targets:
        if t.trainer_id == new_id:
            continue
        old_id = t.trainer_id
        t.trainer_id = new_id
        _log_substitution(t, old_id, new_id)
        if any(b.status == BookingStatus.booked.value for b in t.bookings):
            _notify_substitution(t)
            notified += 1

    day = inst.local_date.strftime("%A")
    when = (
        f"every {day} at {inst.local_time.strftime('%I:%M %p').lstrip('0')} "
        f"from {inst.local_date.strftime('%b %d')} onward"
        if scope_all
        else f"{inst.local_date.strftime('%A %b %d')} at "
        f"{inst.local_time.strftime('%I:%M %p').lstrip('0')} (this day only)"
    )
    if new_id:
        _notify_coach_assignment(new_id, [f"{inst.class_type.name} — {when}"])
    db.session.commit()
    coach = db.session.get(Trainer, new_id).name if new_id else "TBA"
    flash(
        f"{inst.class_type.name}: coach set to {coach} for {when}"
        + (f" — booked members of {notified} session(s) notified." if notified else "."),
        "success",
    )
    return redirect(url_for("ops_admin.live_schedule"))


@bp.route("/instances/<int:instance_id>", methods=["GET", "POST"])
@staff_required
def instance_edit(instance_id: int):
    inst = db.session.get(ClassInstance, instance_id) or abort(404)
    if inst.client_account_id != _cid():
        abort(404)
    trainers = (
        db.session.query(Trainer).filter_by(client_account_id=_cid(), active=True).all()
    )
    if request.method == "POST":
        action = request.form.get("action")
        if action == "override":
            old_trainer_id = inst.trainer_id
            inst.capacity = request.form.get("capacity", type=int) or inst.capacity
            inst.room = (request.form.get("room") or "").strip() or None
            new_trainer_id = request.form.get("trainer_id", type=int) or None
            if new_trainer_id != old_trainer_id:
                inst.trainer_id = new_trainer_id
                _log_substitution(inst, old_trainer_id, new_trainer_id)
                _notify_substitution(inst)
            db.session.commit()
            flash("Instance updated.", "success")
        elif action == "cancel_class":
            _cancel_class(inst)
            db.session.commit()
            flash("Class cancelled — everyone booked has been notified.", "success")
            return redirect(url_for("ops_admin.schedule_builder"))
        return redirect(url_for("ops_admin.instance_edit", instance_id=inst.id))
    roster = [
        b
        for b in inst.bookings
        if b.status in (BookingStatus.booked.value, BookingStatus.attended.value)
    ]
    return render_template(
        "ops/instance_edit.html", inst=inst, trainers=trainers, roster=roster
    )


def _log_substitution(inst: ClassInstance, old_id: int | None, new_id: int | None):
    old = db.session.get(Trainer, old_id).name if old_id else "unassigned"
    new = db.session.get(Trainer, new_id).name if new_id else "unassigned"
    stamp = f"[{utcnow():%Y-%m-%d %H:%M}] sub: {old} → {new} by {current_user.email}"
    inst.notes = f"{inst.notes}\n{stamp}" if inst.notes else stamp


def _notify_substitution(inst: ClassInstance) -> None:
    coach = inst.trainer.name if inst.trainer else "a new coach"
    when = fmt_local(inst.starts_at_utc)
    for b in inst.bookings:
        if b.status != BookingStatus.booked.value:
            continue
        guardian = b.attendee.guardian
        send_email(
            guardian, guardian.email,
            f"Coach update for {inst.class_type.name} ({when})",
            render_template(
                "emails/sub_notice.html",
                guardian=guardian,
                attendee=b.attendee,
                class_name=inst.class_type.name,
                when=when,
                coach=coach,
            ),
            "sub_notice", inst.client_account_id, attendee_id=b.attendee_id,
        )


# ---------------------------------------------------------------- trainers ---
@bp.route("/trainers", methods=["GET", "POST"])
@staff_required
def trainers():
    if request.method == "POST" and request.form.get("action") == "invite":
        t = db.session.get(Trainer, request.form.get("trainer_id", type=int))
        if t is None or t.client_account_id != _cid():
            abort(404)
        if not t.email:
            flash("Add an email to this trainer first, then invite.", "error")
            return redirect(url_for("ops_admin.trainers"))
        from ..services.signed_links import SALT_SET_PASSWORD, make_token
        from ..services.urls import absolute_url

        u = (
            db.session.query(User)
            .filter_by(client_account_id=_cid(), email=t.email.lower())
            .one_or_none()
        )
        if u is None:
            u = User(
                client_account_id=_cid(),
                email=t.email.lower(),
                name=t.name,
                role=Role.trainer.value,
            )
            db.session.add(u)
            db.session.flush()
        if u.role == Role.member.value:
            u.role = Role.trainer.value  # promote a coach who was a member
        t.user_id = u.id
        u.invited_at = utcnow()
        url = absolute_url(
            "portal.set_password", token=make_token(u.id, SALT_SET_PASSWORD)
        )
        send_email(
            None, t.email, "Your Box2Fit coach login",
            render_template("emails/magic_link.html", user=u, url=url),
            "coach_invite", _cid(),
        )
        db.session.commit()
        flash(
            f"Login invite sent to {t.email} — after setting a password they "
            "sign in at /ops/login and land on their coach view.",
            "success",
        )
        return redirect(url_for("ops_admin.trainers"))
    if request.method == "POST" and request.form.get("action") == "delete":
        t = db.session.get(Trainer, request.form.get("trainer_id", type=int))
        if t is None or t.client_account_id != _cid():
            abort(404)
        # Deleting removes them everywhere: unassign from all weekly slots
        # and every class occurrence (past ones show "—" in reports).
        for tpl in db.session.query(ScheduleTemplate).filter_by(trainer_id=t.id):
            tpl.trainer_id = None
        for inst in db.session.query(ClassInstance).filter_by(trainer_id=t.id):
            inst.trainer_id = None
        name = t.name
        db.session.delete(t)
        db.session.commit()
        flash(f"{name} deleted and unassigned from all classes.", "success")
        return redirect(url_for("ops_admin.trainers"))
    if request.method == "POST":
        tid = request.form.get("trainer_id", type=int)
        t = db.session.get(Trainer, tid) if tid else Trainer(client_account_id=_cid())
        if t.client_account_id != _cid():
            abort(404)
        t.name = request.form.get("name", "").strip()
        t.email = request.form.get("email", "").strip() or None
        t.role_title = request.form.get("role_title", "").strip() or None
        t.bio = request.form.get("bio", "").strip() or None
        t.certifications = [
            c.strip() for c in (request.form.get("certs") or "").split(",") if c.strip()
        ]
        t.pay_rate_cents = request.form.get("pay_rate", type=int)  # v1.1-ready
        was_active = t.active if tid else True
        t.active = request.form.get("active") == "on"
        if tid is None:
            db.session.add(t)
        unassigned = 0
        if was_active and not t.active and tid:
            # Inactive coaches never show anywhere: unassign from templates
            # and all future classes.
            for tpl in db.session.query(ScheduleTemplate).filter_by(
                client_account_id=_cid(), trainer_id=t.id
            ):
                tpl.trainer_id = None
                unassigned += 1
            for inst in db.session.query(ClassInstance).filter(
                ClassInstance.client_account_id == _cid(),
                ClassInstance.trainer_id == t.id,
                ClassInstance.local_date >= today_local(),
            ):
                inst.trainer_id = None
        db.session.commit()
        if unassigned:
            flash(
                f"Trainer saved and unassigned from {unassigned} weekly slots "
                "and all upcoming classes.",
                "success",
            )
        else:
            flash("Trainer saved.", "success")
        return redirect(url_for("ops_admin.trainers"))
    rows = db.session.query(Trainer).filter_by(client_account_id=_cid()).all()
    return render_template("ops/trainers.html", trainers=rows)


# ------------------------------------------------------- member directory ---
NOSHOW_FLAG_THRESHOLD = 3  # no-shows in the last 60 days → "high no-show"


@bp.get("/members")
@staff_required
def members():
    q = (request.args.get("q") or "").strip().lower()
    users = (
        db.session.query(User)
        .filter_by(client_account_id=_cid(), role=Role.member.value)
        .order_by(User.created_at.desc())
        .all()
    )
    if q:
        users = [
            u
            for u in users
            if q in u.name.lower()
            or q in u.email.lower()
            or q in (u.phone or "")
            or any(q in a.first_name.lower() for a in u.attendees)
        ]
    rows = [_member_row(u) for u in users[:200]]
    return render_template("ops/members.html", rows=rows, q=q)


def _member_row(u: User) -> dict:
    subs = db.session.query(Subscription).filter_by(user_id=u.id).all()
    status = "trial"
    for s in subs:
        if s.status in (SubscriptionStatus.active.value, SubscriptionStatus.pending.value):
            status = s.status
            break
        if s.status == SubscriptionStatus.past_due.value:
            status = "past_due"
            break
        if s.status == SubscriptionStatus.cancelled.value:
            status = "cancelled"
    ids = [a.id for a in u.attendees] or [0]
    attended = (
        db.session.query(Booking)
        .join(ClassInstance)
        .filter(
            Booking.attendee_id.in_(ids),
            Booking.status == BookingStatus.attended.value,
        )
        .order_by(ClassInstance.starts_at_utc.desc())
        .all()
    )
    last_visit = attended[0].class_instance.local_date if attended else None
    noshows_60d = (
        db.session.query(Booking)
        .join(ClassInstance)
        .filter(
            Booking.attendee_id.in_(ids),
            Booking.status == BookingStatus.no_show.value,
            ClassInstance.local_date >= today_local() - timedelta(days=60),
        )
        .count()
    )
    lead = db.session.query(Lead).filter_by(user_id=u.id).order_by(Lead.id).first()
    flags = []
    if not attended:
        flags.append("first-timer")
    elif last_visit and last_visit < today_local() - timedelta(days=14) and status in (
        "active",
        "pending",
    ):
        flags.append("at-risk")
    if noshows_60d >= NOSHOW_FLAG_THRESHOLD:
        flags.append("high no-show")
    return {
        "user": u,
        "status": status,
        "visits": len(attended),
        "last_visit": last_visit,
        "flags": flags,
        "source": (lead.utm_source or lead.landing_variant) if lead else None,
        "cohorts": [s.cohort_label for s in subs if s.cohort_label],
    }


@bp.route("/members/<int:user_id>", methods=["GET", "POST"])
@staff_required
def member_detail(user_id: int):
    u = db.session.get(User, user_id) or abort(404)
    if u.client_account_id != _cid():
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "note":
            body = (request.form.get("body") or "").strip()
            if body:
                db.session.add(
                    MemberNote(
                        client_account_id=_cid(),
                        user_id=u.id,
                        attendee_id=request.form.get("attendee_id", type=int),
                        author_user_id=current_user.id,
                        body=body,
                    )
                )
                db.session.commit()
                flash("Note added.", "success")
        elif action == "resend_invite":
            from ..services.signed_links import SALT_SET_PASSWORD, make_token
            from ..services.urls import absolute_url

            url = absolute_url(
                "portal.set_password", token=make_token(u.id, SALT_SET_PASSWORD)
            )
            send_email(
                u, u.email, "Your Box2Fit member account",
                render_template("emails/magic_link.html", user=u, url=url),
                "invite", _cid(),
            )
            u.invited_at = utcnow()
            db.session.commit()
            flash("Invite re-sent.", "success")
        elif action == "password_reset":
            from .portal import send_password_reset

            send_password_reset(u)
            db.session.commit()
            flash(f"Password reset link emailed to {u.email}.", "success")
        elif action == "cancel_booking":
            from ..services import waitlist

            bk = db.session.get(Booking, request.form.get("booking_id", type=int))
            if bk and bk.attendee.user_id == u.id and (
                bk.status == BookingStatus.booked.value
            ):
                bk.status = BookingStatus.cancelled.value
                bk.cancelled_at = utcnow()
                waitlist.promote_next(bk.class_instance)
                db.session.commit()
                flash("Booking cancelled — the spot has been released.", "success")
            else:
                flash("That booking cannot be cancelled.", "error")
        elif action == "cancel_sub":
            from ..services import billing

            sub = db.session.get(Subscription, request.form.get("sub_id", type=int))
            if sub and sub.user_id == u.id:
                billing.cancel_subscription(
                    sub,
                    reason=request.form.get("reason") or "staff_initiated",
                    note=f"by {current_user.email}",
                )
                db.session.commit()
                flash("Membership cancelled.", "success")
        return redirect(url_for("ops_admin.member_detail", user_id=u.id))

    row = _member_row(u)
    notes = (
        db.session.query(MemberNote)
        .filter_by(user_id=u.id)
        .order_by(MemberNote.created_at.desc())
        .all()
    )
    authors = {
        n.author_user_id: db.session.get(User, n.author_user_id).name for n in notes
    }
    bookings = (
        db.session.query(Booking)
        .join(ClassInstance)
        .filter(Booking.attendee_id.in_([a.id for a in u.attendees] or [0]))
        .order_by(ClassInstance.starts_at_utc.desc())
        .limit(20)
        .all()
    )
    subs = db.session.query(Subscription).filter_by(user_id=u.id).all()
    lead = db.session.query(Lead).filter_by(user_id=u.id).order_by(Lead.id).first()
    return render_template(
        "ops/member_detail.html",
        u=u,
        row=row,
        notes=notes,
        authors=authors,
        bookings=bookings,
        subs=subs,
        lead=lead,
    )


# ------------------------------------------------------------- marketing ---
@bp.route("/marketing", methods=["GET", "POST"])
@admin_required
def marketing():
    """Ad-campaign funnel: human traffic from the access logs (Meta's bot
    fleet filtered out) joined with lead/booking/activation truth from the
    database, per ad creative. Spend is entered manually from Ads Manager."""
    from ..models import SiteSetting
    from ..services.marketing_report import db_funnel, traffic_funnel

    CAMPAIGNS = ("kids", "youth", "shehits", "beast")
    campaign = request.args.get("c", "kids")
    if campaign not in CAMPAIGNS:
        campaign = "kids"
    if request.method == "POST" and request.form.get("action") == "spend":
        raw = (request.form.get("spend") or "").replace("$", "").strip()
        try:
            SiteSetting.set(f"marketing_spend_{campaign}", f"{float(raw):.2f}")
            db.session.commit()
            flash("Spend updated.", "success")
        except ValueError:
            flash("Enter spend as a number, e.g. 11.60", "error")
        return redirect(url_for("ops_admin.marketing", c=campaign))

    spend = float(SiteSetting.get(f"marketing_spend_{campaign}", "0") or 0)
    traffic = traffic_funnel(campaign)
    funnel = db_funnel(_cid(), campaign)
    leads_n = funnel["totals"]["leads"]
    return render_template(
        "ops/marketing.html",
        campaign=campaign,
        campaigns=CAMPAIGNS,
        traffic=traffic,
        funnel=funnel,
        spend=spend,
        cpl=(spend / leads_n) if leads_n else None,
    )


# ---------------------------------------------------------------- reports ---
@bp.get("/reports")
@admin_required
def reports():
    start = today_local() - timedelta(days=28)
    instances = (
        db.session.query(ClassInstance)
        .filter(
            ClassInstance.client_account_id == _cid(),
            ClassInstance.local_date >= start,
            ClassInstance.local_date <= today_local(),
            ClassInstance.status == InstanceStatus.scheduled.value,
        )
        .all()
    )
    counts = booked_counts([i.id for i in instances])

    # fill heatmap: weekday × hour → avg fill %
    heat: dict = defaultdict(list)
    for i in instances:
        heat[(i.local_date.weekday(), i.local_time.hour)].append(
            min(1.0, counts.get(i.id, 0) / i.capacity) if i.capacity else 0
        )
    heatmap = {k: round(100 * sum(v) / len(v)) for k, v in heat.items()}
    hours = sorted({h for (_, h) in heatmap})

    # attendance trend, 8 weeks
    attended = (
        db.session.query(Booking)
        .join(ClassInstance)
        .filter(
            Booking.client_account_id == _cid(),
            Booking.status == BookingStatus.attended.value,
            ClassInstance.local_date >= today_local() - timedelta(weeks=8),
        )
        .all()
    )
    weekly: dict = defaultdict(int)
    for b in attended:
        y, w, _ = b.class_instance.local_date.isocalendar()
        weekly[f"{y}-W{w:02d}"] += 1
    trend = sorted(weekly.items())

    # churn by reason
    churn: dict = defaultdict(int)
    for s in (
        db.session.query(Subscription)
        .filter_by(client_account_id=_cid(), status=SubscriptionStatus.cancelled.value)
        .all()
    ):
        churn[s.cancel_reason or "unknown"] += 1

    # trial funnel by class type and trainer
    trials = (
        db.session.query(Booking)
        .join(ClassInstance)
        .filter(Booking.client_account_id == _cid(), Booking.kind.in_(["trial", "walkin"]))
        .all()
    )
    activated_attendees = {
        s.attendee_id
        for s in db.session.query(Subscription)
        .filter(Subscription.client_account_id == _cid(), Subscription.activated_at.isnot(None))
        .all()
    }
    tf: dict = defaultdict(lambda: {"booked": 0, "showed": 0, "converted": 0})
    tf_trainer: dict = defaultdict(lambda: {"booked": 0, "showed": 0, "converted": 0})
    for b in trials:
        keys = [b.class_instance.class_type.name]
        tkeys = [b.class_instance.trainer.name if b.class_instance.trainer else "—"]
        for k in keys:
            tf[k]["booked"] += 1
            if b.status == BookingStatus.attended.value:
                tf[k]["showed"] += 1
            if b.attendee_id in activated_attendees:
                tf[k]["converted"] += 1
        for k in tkeys:
            tf_trainer[k]["booked"] += 1
            if b.status == BookingStatus.attended.value:
                tf_trainer[k]["showed"] += 1
            if b.attendee_id in activated_attendees:
                tf_trainer[k]["converted"] += 1

    # no-show / late-cancel leaders (staff-only view, never member-facing)
    ns_rows = (
        db.session.query(Booking)
        .filter(
            Booking.client_account_id == _cid(),
            Booking.status.in_([BookingStatus.no_show.value]),
        )
        .all()
    )
    lc_rows = (
        db.session.query(Booking)
        .filter(Booking.client_account_id == _cid(), Booking.late_cancel.is_(True))
        .all()
    )
    per_member: dict = defaultdict(lambda: {"no_shows": 0, "late_cancels": 0})
    for b in ns_rows:
        per_member[b.attendee.display_name]["no_shows"] += 1
    for b in lc_rows:
        per_member[b.attendee.display_name]["late_cancels"] += 1
    ns_report = sorted(
        per_member.items(),
        key=lambda kv: -(kv[1]["no_shows"] + kv[1]["late_cancels"]),
    )[:15]

    mrr = sum(
        s.mrr_cents
        for s in db.session.query(Subscription)
        .filter_by(client_account_id=_cid(), status=SubscriptionStatus.active.value)
        .all()
    )
    return render_template(
        "ops/reports.html",
        heatmap=heatmap,
        hours=hours,
        trend=trend,
        churn=sorted(churn.items(), key=lambda kv: -kv[1]),
        trial_by_class=dict(tf),
        trial_by_trainer=dict(tf_trainer),
        ns_report=ns_report,
        mrr=mrr,
        weekdays=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    )


EXPORTS = {
    "members": (
        User,
        ["id", "name", "email", "phone", "role", "consent_email", "consent_sms", "created_at"],
    ),
    "leads": (
        Lead,
        ["id", "name", "email", "phone", "segment", "status", "utm_source",
         "utm_medium", "utm_campaign", "utm_content", "landing_variant",
         "referral_code", "created_at"],
    ),
    "bookings": (
        Booking,
        ["id", "attendee_id", "class_instance_id", "kind", "status", "late_cancel",
         "booked_at", "cancelled_at", "checked_in_at"],
    ),
    "payments": (
        Payment,
        ["id", "user_id", "subscription_id", "stripe_invoice_id", "amount_cents",
         "agency_share_cents", "refunded_cents", "status", "paid_at"],
    ),
    "subscriptions": (
        Subscription,
        ["id", "user_id", "attendee_id", "cohort_label", "status", "mrr_cents",
         "activated_at", "cancelled_at", "cancel_reason"],
    ),
    "calls": (
        Call,
        ["id", "tracking_number", "caller_number", "duration_sec", "started_at",
         "matched_lead_id", "outcome"],
    ),
}


@bp.get("/export/<name>.csv")
@admin_required
def export_csv(name: str):
    if name not in EXPORTS:
        abort(404)
    model, cols = EXPORTS[name]
    rows = db.session.query(model).filter_by(client_account_id=_cid()).all() \
        if hasattr(model, "client_account_id") else db.session.query(model).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(cols)
    for r in rows:
        writer.writerow([getattr(r, c) for c in cols])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}.csv"},
    )


# ----------------------------------------------------------- announcements ---
@bp.route("/announcements", methods=["GET", "POST"])
@staff_required
def announcements():
    from ..services.booking_flow import admin_alert_recipients

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_alert_email":
            email = (request.form.get("alert_email") or "").strip().lower()
            if "@" not in email or "." not in email.split("@")[-1]:
                flash("Enter a valid email address.", "error")
            else:
                emails = admin_alert_recipients()
                if email not in emails:
                    emails.append(email)
                    SiteSetting.set("admin_notify_emails", ",".join(emails))
                    db.session.commit()
                flash(f"{email} now receives new sign-up alerts.", "success")
            return redirect(url_for("ops_admin.announcements"))
        elif action == "remove_alert_email":
            email = (request.form.get("alert_email") or "").strip().lower()
            emails = [e for e in admin_alert_recipients() if e != email]
            SiteSetting.set("admin_notify_emails", ",".join(emails))
            db.session.commit()
            flash(
                f"{email} removed"
                + (" — no alert recipients remain!" if not emails else "."),
                "success" if emails else "error",
            )
            return redirect(url_for("ops_admin.announcements"))
        if action == "create":
            db.session.add(
                Announcement(
                    client_account_id=_cid(),
                    title=request.form.get("title", "").strip(),
                    body=request.form.get("body", "").strip(),
                    class_type_id=request.form.get("class_type_id", type=int) or None,
                    created_by=current_user.email,
                )
            )
            db.session.commit()
            flash("Announcement live — it now shows in the portal.", "success")
        elif action == "toggle":
            a = db.session.get(Announcement, request.form.get("id", type=int))
            if a and a.client_account_id == _cid():
                a.active = not a.active
                db.session.commit()
        elif action == "email":
            a = db.session.get(Announcement, request.form.get("id", type=int))
            if a and a.client_account_id == _cid():
                sent = _email_announcement(a)
                db.session.commit()
                flash(f"Announcement emailed to {sent} members (consent + news pref respected).", "success")
        return redirect(url_for("ops_admin.announcements"))
    rows = (
        db.session.query(Announcement)
        .filter_by(client_account_id=_cid())
        .order_by(Announcement.created_at.desc())
        .all()
    )
    class_types = db.session.query(ClassType).filter_by(client_account_id=_cid()).all()
    type_names = {c.id: c.name for c in class_types}
    return render_template(
        "ops/announcements.html",
        rows=rows,
        class_types=class_types,
        type_names=type_names,
        alert_emails=admin_alert_recipients(),
    )


def _email_announcement(a: Announcement) -> int:
    members = (
        db.session.query(User)
        .filter_by(client_account_id=_cid(), role=Role.member.value, active=True)
        .all()
    )
    sent = 0
    for m in members:
        prefs = {"news": True, **(m.notify_prefs or {})}
        if not prefs.get("news"):
            continue
        msg = send_email(
            m, m.email, a.title,
            render_template("emails/announcement.html", user=m, a=a),
            "announcement", _cid(), transactional=False,  # respects CASL consent
        )
        if msg.delivery_status != "suppressed_no_consent":
            sent += 1
    a.emailed_at = utcnow()
    return sent


# ----------------------------------------------------------------- reviews ---
@bp.route("/reviews", methods=["GET", "POST"])
@staff_required
def reviews():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "save":
            rid = request.form.get("id", type=int)
            r = db.session.get(Review, rid) if rid else Review(client_account_id=_cid())
            if r.client_account_id != _cid():
                abort(404)
            r.reviewer_name = request.form.get("reviewer_name", "").strip()
            r.quote_text = request.form.get("quote_text", "").strip()  # verbatim!
            r.rating = request.form.get("rating", type=int) or 5
            r.segment_tags = request.form.get("segment_tags", "").strip()
            r.display_order = request.form.get("display_order", type=int) or 0
            r.active = request.form.get("active") == "on"
            if rid is None:
                db.session.add(r)
            db.session.commit()
            flash("Review saved.", "success")
        elif action == "badge":
            SiteSetting.set("google_rating", request.form.get("google_rating", "5.0"))
            SiteSetting.set(
                "google_review_count", request.form.get("google_review_count", "0")
            )
            db.session.commit()
            flash("Aggregate badge updated.", "success")
        return redirect(url_for("ops_admin.reviews"))
    rows = (
        db.session.query(Review)
        .filter_by(client_account_id=_cid())
        .order_by(Review.display_order, Review.id)
        .all()
    )
    return render_template(
        "ops/reviews.html",
        rows=rows,
        google_rating=SiteSetting.get("google_rating", "5.0"),
        google_review_count=SiteSetting.get("google_review_count", "0"),
    )
