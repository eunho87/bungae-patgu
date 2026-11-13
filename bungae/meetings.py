import re
from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import db, notify
from .auth import _clean_interests, login_required
from .constants import (
    CAPACITY_MAX,
    CAPACITY_MIN,
    CATEGORY_KEYS,
    CATEGORY_LABEL,
    DURATIONS,
    MAX_DAYS_AHEAD,
    MAX_TAGS,
    PLACE_SUGGESTIONS,
    REPORT_LIMIT,
    REPORT_REASONS,
)
from .models import Comment, Meeting, Participation, Report
from .recommend import my_upcoming, open_meetings, similar_to
from .timeutil import fmt_when, from_kst, now_kst, now_utc, to_kst

bp = Blueprint("meetings", __name__)

TAG_SPLIT_RE = re.compile(r"[,\s#]+")
TAG_RE = re.compile(r"^[0-9A-Za-z가-힣]{1,12}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CONTACT_PREFIX = "https://open.kakao.com/"
MAX_COMMENTS_PER_MEETING = 100
MAX_COMMENTS_PER_USER = 20  # 한 사람이 한 모임의 댓글 한도를 다 채우지 못하게
MAX_ACTIVE_HOSTED = 5  # 한 사람이 동시에 열어 둘 수 있는 모임 수(목록 도배 방지)

# 주소의 숫자가 DB 정수 범위를 넘으면 조회 자체가 오류를 낸다. 그런 주소는 처음부터 '없는 페이지'로 처리한다.
MEETING_ID = "<int(max=2147483647):meeting_id>"
COMMENT_ID = "<int(max=2147483647):comment_id>"


def parse_tags(raw):
    """(쓸 수 있는 태그, 형식이 맞지 않아 버려야 하는 토큰)"""
    tags, rejected = [], []
    for token in TAG_SPLIT_RE.split(raw or ""):
        if not token:
            continue
        if TAG_RE.match(token):
            if token not in tags:
                tags.append(token)
        else:
            rejected.append(token)
    return tags, rejected


def validate_meeting_form(form, existing=None):
    """(정리된 값, 오류 dict)를 돌려준다. 값 검사는 서버에서 한 번 더 한다(브라우저 검사는 우회할 수 있다)."""
    errors = {}
    data = {}

    category = form.get("category", "")
    if category not in CATEGORY_KEYS:
        errors["category"] = "활동 종류를 골라 주세요."
    data["category"] = category

    place = form.get("place", "").strip()
    if not 1 <= len(place) <= 60:
        errors["place"] = "만날 장소를 60자 안으로 적어 주세요."
    data["place"] = place

    title = form.get("title", "").strip()
    if not title and "category" not in errors and "place" not in errors:
        # 이름을 비워 두면 종류와 장소로 지어 준다. 번개를 여는 데 꼭 필요한 타이핑을 없애기 위해서다.
        title = f"{CATEGORY_LABEL[category]} 번개 · {place}"[:40]
    if not 2 <= len(title) <= 40:
        errors["title"] = "모임 이름은 2~40자로 적어 주세요."
    data["title"] = title

    start_utc = None
    try:
        start_kst = datetime.strptime(f"{form.get('date', '')} {form.get('time', '')}", "%Y-%m-%d %H:%M")
        start_utc = from_kst(start_kst)
    except (ValueError, OverflowError):
        # OverflowError: 0001-01-01처럼 형식은 맞지만 시간대 변환에서 범위를 벗어나는 날짜
        errors["start"] = "날짜와 시간을 입력해 주세요."
    if start_utc is not None:
        now = now_utc()
        if start_utc <= now:
            errors["start"] = "이미 지난 시간이에요. 앞으로의 시간을 골라 주세요."
            if start_kst.date() == now_kst().date():
                errors["start"] += " 자정을 넘긴 시간이라면 날짜를 내일로 바꿔 주세요."
        elif start_kst.date() > now_kst().date() + timedelta(days=MAX_DAYS_AHEAD):
            # 달력에서 고를 수 있는 마지막 날과 같은 기준(날짜)으로 본다.
            errors["start"] = f"번개 모임은 {MAX_DAYS_AHEAD}일 안의 일정만 열 수 있어요."
    data["start_at"] = start_utc

    try:
        duration = int(form.get("duration", ""))
    except ValueError:
        duration = 0
    if duration not in DURATIONS:
        errors["duration"] = "예상 소요 시간을 골라 주세요."
    data["duration_min"] = duration

    try:
        capacity = int(form.get("capacity", ""))
    except ValueError:
        capacity = 0
    if not CAPACITY_MIN <= capacity <= CAPACITY_MAX:
        errors["capacity"] = f"모집 인원은 나를 포함해 {CAPACITY_MIN}~{CAPACITY_MAX}명으로 정해 주세요."
    elif existing is not None and capacity < existing.joined_count:
        errors["capacity"] = f"이미 {existing.joined_count}명이 참여 중이라 그보다 적게 줄일 수 없어요."
    data["capacity"] = capacity

    description = form.get("description", "").strip()
    if len(description) > 500:
        errors["description"] = "소개는 500자 안으로 적어 주세요."
    data["description"] = description

    tags, rejected = parse_tags(form.get("tags", ""))
    if rejected:
        # 조용히 버리면 사용자는 태그가 왜 사라졌는지 알 수 없다.
        shown = ", ".join(token[:15] for token in rejected[:3])
        errors["tags"] = f"태그는 한글·영문·숫자 12자까지만 쓸 수 있어요. 고쳐 주세요: {shown}"
    elif len(tags) > MAX_TAGS:
        errors["tags"] = f"태그는 {MAX_TAGS}개까지 달 수 있어요."
    data["tags"] = ",".join(tags[:MAX_TAGS])

    contact = form.get("contact_link", "").strip()
    valid_contact = (
        contact.startswith(CONTACT_PREFIX)
        and len(CONTACT_PREFIX) < len(contact) <= 200
        and not re.search(r"\s", contact)
    )
    if contact and not valid_contact:
        # 아무 주소나 받으면 피싱 사이트로 보내는 통로가 된다. 이름 그대로 카카오 오픈채팅 주소만 받는다.
        errors["contact_link"] = f"오픈채팅 링크는 {CONTACT_PREFIX} 로 시작하는 주소만 넣을 수 있어요."
    data["contact_link"] = contact

    return data, errors


FORM_KEYS = ["title", "category", "date", "time", "duration", "place", "capacity", "description", "tags", "contact_link"]


def _form_values(source=None, meeting=None):
    """폼을 다시 그릴 때 쓸 문자열 값(수정 화면이거나, 오류가 나서 입력한 값을 되돌려 줄 때)."""
    if meeting is not None:
        start = to_kst(meeting.start_at)
        return {
            "title": meeting.title,
            "category": meeting.category,
            "date": start.strftime("%Y-%m-%d"),
            "time": start.strftime("%H:%M"),
            "duration": str(meeting.duration_min),
            "place": meeting.place,
            "capacity": str(meeting.capacity),
            "description": meeting.description,
            "tags": " ".join(meeting.tag_list),
            "contact_link": meeting.contact_link,
        }
    source = source or {}
    return {key: str(source.get(key, ""))[:500] for key in FORM_KEYS}


def _new_form_values(args, user):
    """새 모임 폼의 처음 값. 주소로 미리 채울 수 있는 것은 종류·날짜·시각·소요 시간뿐이고 형식이 맞을 때만 쓴다.

    제목이나 링크까지 주소로 채울 수 있게 두면, 남이 만든 주소 하나로 엉뚱한 내용이 미리 적힌 폼을 열게 할 수 있다.
    """
    values = dict.fromkeys(FORM_KEYS, "")
    values["duration"], values["capacity"] = "60", "4"

    category = args.get("category", "")
    if category in CATEGORY_KEYS:
        values["category"] = category
    elif user.interest_list:
        values["category"] = user.interest_list[0]

    # 번개는 대개 조금 뒤에 여므로 '30분 뒤'를 미리 넣어 둔다. 자정 직전에도 날짜가 알아서 내일로 넘어간다.
    _, values["date"], values["time"] = quick_times(now_kst())[0]
    date_arg, time_arg = args.get("date", ""), args.get("time", "")
    if DATE_RE.match(date_arg):
        values["date"] = date_arg
        values["time"] = ""
    if TIME_RE.match(time_arg):
        values["time"] = time_arg

    duration = args.get("duration", "")
    if duration.isdigit() and int(duration) in DURATIONS:
        values["duration"] = duration
    return values


def quick_times(now):
    """모임 폼의 '빠른 시간 선택' 버튼. (표시 이름, 날짜, 시각) 목록. now는 KST.

    번개 모임은 대부분 '조금 뒤'에 열리는데 날짜·시각 선택기를 두 번 여는 것은 번거롭다.
    서버 시각으로 계산해 내려보내므로 사용자 기기의 시계나 시간대가 달라도 어긋나지 않는다.
    """

    def later(minutes):
        moment = now + timedelta(minutes=minutes)
        moment += timedelta(minutes=(-moment.minute) % 10)  # 10분 단위로 올림
        return moment.replace(second=0, microsecond=0)

    picks = [("30분 뒤", later(30)), ("1시간 뒤", later(60)), ("2시간 뒤", later(120))]
    evening = datetime.combine(now.date(), datetime.min.time()).replace(hour=18)
    if evening >= now + timedelta(minutes=30):
        picks.append(("오늘 저녁 6시", evening))
    noon = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()).replace(hour=12)
    picks.append(("내일 낮 12시", noon))
    return [(label, moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M")) for label, moment in picks]


def _form_context():
    today = now_kst().date()
    return {
        "quick_times": quick_times(now_kst()),
        "durations": DURATIONS,
        "place_suggestions": PLACE_SUGGESTIONS,
        "min_date": today.isoformat(),
        "max_date": (today + timedelta(days=MAX_DAYS_AHEAD)).isoformat(),
        "capacity_min": CAPACITY_MIN,
        "capacity_max": CAPACITY_MAX,
    }


def _get_meeting_or_404(meeting_id):
    meeting = db.session.get(Meeting, meeting_id)
    if meeting is None:
        abort(404)
    return meeting


def _require_host(meeting):
    if meeting.host_id != g.user.id:
        abort(403)


def _block_if_reported(meeting):
    """셀 수 있는 신고가 기준을 넘었으면 모집을 멈춘다. 새로 멈췄으면 True.

    멈춘 사실은 status에 'blocked'로 저장한다. 신고 수를 볼 때마다 다시 세어 판단하면, 신고한 사람이
    모임에서 나가거나 탈퇴하는 순간 수가 줄어 차단이 풀려 버린다. 반대로 방금 가입한 계정의 신고는
    하루가 지나야 세므로, 신고가 들어온 순간이 아니라 모임을 열어 보거나 참여하려는 순간에도 확인한다.
    """
    if meeting.status not in ("open", "closed") or meeting.has_started:
        return False
    if meeting.counted_reports < REPORT_LIMIT:
        return False
    meeting.status = "blocked"
    notify.push(meeting.host_id, f"모임 ‘{notify.short(meeting.title)}’에 신고가 여러 건 접수되어 모집이 중단됐어요.", meeting)
    return True


LIMIT_MESSAGE = (
    f"한 번에 열어 둘 수 있는 번개는 {MAX_ACTIVE_HOSTED}개까지예요. "
    "열어 둔 번개가 시작했거나 취소한 뒤에 새로 열 수 있어요."
)


@bp.route("/meetings/new", methods=["GET", "POST"])
@login_required
def create():
    active = Meeting.query.filter(
        Meeting.host_id == g.user.id, Meeting.status != "cancelled", Meeting.start_at > now_utc()
    ).count()
    limit_reached = active >= MAX_ACTIVE_HOSTED
    errors = {}
    if request.method == "POST":
        data, errors = validate_meeting_form(request.form)
        if limit_reached:
            # 다른 칸을 다 고친 뒤에야 한도를 알려 주면 두 번 헛걸음하게 된다. 한도부터 알린다.
            errors = {"limit": LIMIT_MESSAGE}
        if not errors:
            meeting = Meeting(host_id=g.user.id, **data)
            # 호스트도 참여자 한 명으로 센다. 모집 인원은 호스트를 포함한 총원이다.
            meeting.participations.append(Participation(user_id=g.user.id))
            clash = next((m for m in my_upcoming(g.user) if m.overlaps(meeting)), None)
            db.session.add(meeting)
            db.session.commit()
            flash("번개를 열었어요! 링크를 공유해서 사람을 모아 보세요.", "success")
            if clash is not None:
                flash(f"참여 중인 ‘{clash.title}’({fmt_when(clash.start_at, clash.duration_min)})와 시간이 겹쳐요.", "info")
            return redirect(url_for("meetings.detail", meeting_id=meeting.id))
        values = _form_values(request.form)
    else:
        values = _new_form_values(request.args, g.user)
    return (
        render_template(
            "meeting_form.html",
            mode="create",
            values=values,
            errors=errors,
            limit_reached=limit_reached,
            limit_message=LIMIT_MESSAGE,
            **_form_context(),
        ),
        400 if errors else 200,
    )


def _host_stats(host_id):
    """호스트가 지금까지 실제로 치른 모임 수. 취소한 것과 아직 시작하지 않은 것은 세지 않는다."""
    now = now_utc()
    return {
        "hosted": Meeting.query.filter(
            Meeting.host_id == host_id, Meeting.status != "cancelled", Meeting.start_at <= now
        ).count(),
        "joined": Participation.query.join(Meeting)
        .filter(
            Participation.user_id == host_id,
            Meeting.host_id != host_id,
            Meeting.status != "cancelled",
            Meeting.start_at <= now,
        )
        .count(),
    }


@bp.get(f"/meetings/{MEETING_ID}")
@login_required
def detail(meeting_id):
    meeting = _get_meeting_or_404(meeting_id)
    if _block_if_reported(meeting):
        db.session.commit()
    is_host = meeting.host_id == g.user.id
    is_member = meeting.has_participant(g.user)
    conflict = None
    similar = []
    if not is_member:
        # 내 일정은 한 번만 읽어 겹침 경고와 비슷한 모임 추천에 함께 쓴다.
        mine = my_upcoming(g.user)
        if meeting.joinable:
            conflict = next((m for m in mine if m.overlaps(meeting)), None)
        similar = similar_to(meeting, g.user, pool=open_meetings(g.user, mine=mine))
    return render_template(
        "meeting_detail.html",
        meeting=meeting,
        is_host=is_host,
        is_member=is_member,
        conflict=conflict,
        host_stats=_host_stats(meeting.host_id),
        similar=similar,
        share_url=url_for("meetings.detail", meeting_id=meeting.id, _external=True),
        report_reasons=REPORT_REASONS,
        reported_by_me=any(r.user_id == g.user.id for r in meeting.reports),
        report_blocked=meeting.status == "blocked",
    )


@bp.post(f"/meetings/{MEETING_ID}/report")
@login_required
def report(meeting_id):
    """모임 신고. 운영자가 상주하지 않으므로 신고가 여러 사람에게서 쌓이면 모집을 자동으로 멈춘다."""
    meeting = _get_meeting_or_404(meeting_id)
    reason = request.form.get("reason", "")
    if meeting.host_id == g.user.id:
        flash("내가 연 모임은 신고할 수 없어요.", "error")
    elif reason not in dict(REPORT_REASONS):
        flash("신고 사유를 골라 주세요.", "error")
    elif any(r.user_id == g.user.id for r in meeting.reports):
        flash("이미 신고한 모임이에요.", "info")
    else:
        meeting.reports.append(Report(user=g.user, reason=reason))
        _block_if_reported(meeting)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("이미 신고한 모임이에요.", "info")
        else:
            flash("신고가 접수됐어요. 알려 주셔서 고마워요.", "success")
    return redirect(url_for("meetings.detail", meeting_id=meeting.id))


@bp.post(f"/meetings/{MEETING_ID}/join")
@login_required
def join(meeting_id):
    # 마지막 한 자리를 두 사람이 동시에 누르는 경우를 막기 위해 모임 행을 잠근 뒤 인원을 센다.
    # (SQLite는 FOR UPDATE를 무시하지만 쓰기 자체가 한 번에 하나씩만 실행된다.)
    # of=Meeting: 호스트 정보를 LEFT JOIN으로 같이 읽는데, PostgreSQL은 그런 조인 전체에는 잠금을 걸 수 없다.
    meeting = db.session.execute(
        select(Meeting).where(Meeting.id == meeting_id).with_for_update(of=Meeting)
    ).scalar_one_or_none()
    if meeting is None:
        abort(404)
    if meeting.has_participant(g.user):
        flash("이미 참여 중인 모임이에요.", "info")
    elif _block_if_reported(meeting):
        db.session.commit()
        flash("신고가 여러 건 접수되어 모집이 중단된 모임이에요.", "error")
    elif not meeting.joinable:
        flash("지금은 참여할 수 없는 모임이에요. (마감됐거나 이미 시작했어요)", "error")
    else:
        meeting.participations.append(Participation(user_id=g.user.id))
        notify.joined(meeting, g.user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("이미 참여 중인 모임이에요.", "info")
        else:
            if meeting.contact_link:
                flash("참여가 확정됐어요! 아래 오픈채팅 링크에서 인사를 나눠 보세요.", "success")
            else:
                flash("참여가 확정됐어요! 시간 맞춰 만나요.", "success")
    return redirect(url_for("meetings.detail", meeting_id=meeting_id))


@bp.post(f"/meetings/{MEETING_ID}/leave")
@login_required
def leave(meeting_id):
    meeting = _get_meeting_or_404(meeting_id)
    if meeting.host_id == g.user.id:
        flash("호스트는 나갈 수 없어요. 모임을 취소해 주세요.", "error")
    elif not meeting.has_participant(g.user):
        flash("참여하고 있지 않은 모임이에요.", "info")
    elif meeting.status == "cancelled":
        flash("이미 취소된 모임이에요.", "info")
    elif meeting.has_started:
        flash("이미 시작한 모임은 취소할 수 없어요.", "error")
    else:
        Participation.query.filter_by(meeting_id=meeting.id, user_id=g.user.id).delete()
        notify.left(meeting, g.user)
        db.session.commit()
        flash("참여를 취소했어요.", "info")
    return redirect(url_for("meetings.detail", meeting_id=meeting_id))


@bp.route(f"/meetings/{MEETING_ID}/edit", methods=["GET", "POST"])
@login_required
def edit(meeting_id):
    meeting = _get_meeting_or_404(meeting_id)
    _require_host(meeting)
    if meeting.status == "cancelled" or meeting.has_started:
        flash("이미 시작했거나 취소된 모임은 고칠 수 없어요.", "error")
        return redirect(url_for("meetings.detail", meeting_id=meeting.id))
    errors = {}
    values = _form_values(meeting=meeting)
    if request.method == "POST":
        data, errors = validate_meeting_form(request.form, existing=meeting)
        if not errors:
            moved = (meeting.title, meeting.start_at, meeting.duration_min, meeting.place) != (
                data["title"],
                data["start_at"],
                data["duration_min"],
                data["place"],
            )
            link_added = bool(data["contact_link"]) and not meeting.contact_link
            for key, value in data.items():
                setattr(meeting, key, value)
            # 이미 참여한 사람은 바뀐 줄 모르고 예전 시간에 예전 장소로 나올 수 있다.
            if moved:
                notify.changed(meeting, g.user)
            elif link_added:
                notify.link_added(meeting, g.user)
            db.session.commit()
            flash("모임 정보를 고쳤어요." + (" 참여자에게 알림이 가요." if moved or link_added else ""), "success")
            return redirect(url_for("meetings.detail", meeting_id=meeting.id))
        values = _form_values(request.form)
    return (
        render_template(
            "meeting_form.html",
            mode="edit",
            meeting=meeting,
            values=values,
            errors=errors,
            limit_reached=False,
            **_form_context(),
        ),
        400 if errors else 200,
    )


@bp.post(f"/meetings/{MEETING_ID}/status")
@login_required
def set_status(meeting_id):
    """호스트가 모집을 직접 마감하거나, 다시 열거나, 모임을 취소한다."""
    meeting = _get_meeting_or_404(meeting_id)
    _require_host(meeting)
    action = request.form.get("action")
    if action not in ("close", "reopen", "cancel"):
        abort(400)
    if meeting.status == "cancelled":
        flash("이미 취소된 모임이에요.", "error")
    elif meeting.has_started:
        flash("이미 시작한 모임은 바꿀 수 없어요.", "error")
    elif action == "cancel":
        meeting.status = "cancelled"
        notify.cancelled(meeting, g.user)
        flash("모임을 취소했어요. 참여자에게 알림이 가요.", "info")
    elif meeting.status == "blocked":
        flash("신고가 여러 건 접수되어 모집이 중단된 모임이에요. 다시 열 수 없어요.", "error")
    elif action == "close":
        meeting.status = "closed"
        flash("모집을 마감했어요.", "info")
    else:
        meeting.status = "open"
        flash("모집을 다시 열었어요.", "success")
    db.session.commit()
    return redirect(url_for("meetings.detail", meeting_id=meeting.id))


@bp.post(f"/meetings/{MEETING_ID}/comments")
@login_required
def add_comment(meeting_id):
    meeting = _get_meeting_or_404(meeting_id)
    body = " ".join(request.form.get("body", "").split())
    is_host = meeting.host_id == g.user.id
    error = None
    if not 1 <= len(body) <= 200:
        error = "댓글은 1~200자로 적어 주세요."
    elif meeting.status == "cancelled":
        error = "취소된 모임에는 댓글을 달 수 없어요."
    elif (
        not is_host
        and Comment.query.filter_by(meeting_id=meeting.id, user_id=g.user.id).count() >= MAX_COMMENTS_PER_USER
    ):
        error = f"한 모임에는 댓글을 {MAX_COMMENTS_PER_USER}개까지 달 수 있어요."
    elif Comment.query.filter_by(meeting_id=meeting.id).count() >= MAX_COMMENTS_PER_MEETING:
        error = f"이 모임은 댓글이 {MAX_COMMENTS_PER_MEETING}개를 넘어 더 달 수 없어요."
    if error:
        # 알림 문구는 화면 맨 위에 뜨므로, 오류일 때는 댓글 위치로 내려보내지 않는다(내려가면 문구가 안 보인다).
        flash(error, "error")
        return redirect(url_for("meetings.detail", meeting_id=meeting.id))
    db.session.add(Comment(meeting_id=meeting.id, user_id=g.user.id, body=body))
    notify.commented(meeting, g.user)
    db.session.commit()
    return redirect(url_for("meetings.detail", meeting_id=meeting.id) + "#comments")


@bp.post(f"/comments/{COMMENT_ID}/delete")
@login_required
def delete_comment(comment_id):
    comment = db.session.get(Comment, comment_id)
    if comment is None:
        abort(404)
    # 댓글은 쓴 사람과 그 모임의 호스트만 지울 수 있다.
    if g.user.id not in (comment.user_id, comment.meeting.host_id):
        abort(403)
    meeting_id = comment.meeting_id
    db.session.delete(comment)
    db.session.commit()
    return redirect(url_for("meetings.detail", meeting_id=meeting_id) + "#comments")


@bp.get("/me")
@login_required
def me():
    now = now_utc()
    joined = (
        Meeting.query.join(Participation)
        .filter(Participation.user_id == g.user.id)
        .order_by(Meeting.start_at.desc())
        .limit(100)
        .all()
    )
    upcoming = sorted(
        (m for m in joined if m.end_at > now and m.status != "cancelled"), key=lambda m: m.start_at
    )
    past = [m for m in joined if m.end_at <= now or m.status == "cancelled"][:20]
    return render_template(
        "me.html",
        hosting=[m for m in upcoming if m.host_id == g.user.id],
        joining=[m for m in upcoming if m.host_id != g.user.id],
        past=past,
    )


@bp.post("/me/interests")
@login_required
def update_interests():
    g.user.interests = _clean_interests(request.form.getlist("interests"))
    db.session.commit()
    flash("관심사를 저장했어요. 홈의 추천에 반영돼요.", "success")
    return redirect(url_for("meetings.me"))
