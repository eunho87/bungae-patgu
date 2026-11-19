"""알림함, 비밀번호 변경, 회원 탈퇴, 이용 안내."""

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, emailcode, notify
from sqlalchemy.exc import IntegrityError

from .auth import NICKNAME_RE, NICKNAME_RULE, login_required, nickname_taken
from .models import ChatUsage, Comment, LoginThrottle, Meeting, Notification, Participation, Report
from .timeutil import now_utc

bp = Blueprint("account", __name__)


@bp.get("/about")
def about():
    return render_template("about.html")


@bp.get("/notifications")
@login_required
def notifications():
    # 읽음 처리 뒤에도 '방금 처음 본 알림'을 표시할 수 있게 값을 먼저 꺼내 둔다.
    items = [
        {"message": n.message, "meeting_id": n.meeting_id, "created_at": n.created_at, "is_new": not n.is_read}
        for n in notify.recent(g.user)
    ]
    if any(item["is_new"] for item in items):
        Notification.query.filter_by(user_id=g.user.id, is_read=False).update(
            {"is_read": True}, synchronize_session=False
        )
        notify.prune(g.user)
        db.session.commit()
    return render_template("notifications.html", items=items)


@bp.post("/me/password")
@login_required
def change_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("new_password_confirm", "")
    if not check_password_hash(g.user.password_hash, current):
        flash("현재 비밀번호가 맞지 않아요.", "error")
    elif not 8 <= len(new) <= 64:
        flash("새 비밀번호는 8자 이상 64자 이하로 정해 주세요.", "error")
    elif not new.strip():
        flash("공백만으로 된 비밀번호는 쓸 수 없어요.", "error")
    elif new != confirm:
        flash("새 비밀번호가 서로 달라요.", "error")
    elif new == current:
        flash("지금 쓰는 비밀번호와 같아요. 다른 비밀번호를 정해 주세요.", "error")
    else:
        g.user.password_hash = generate_password_hash(new)
        db.session.commit()
        # 지금 쓰는 기기는 로그인 상태를 유지하고, 다른 기기의 로그인은 다음 요청에서 풀린다.
        session["pw"] = g.user.password_hash[-8:]
        flash("비밀번호를 바꿨어요. 다른 기기에서는 다시 로그인해야 해요.", "success")
    # 결과 문구는 화면 맨 위에 뜬다. 계정 위치(#account)로 내려보내면 문구가 보이지 않는다.
    return redirect(url_for("meetings.me"))


@bp.post("/me/nickname")
@login_required
def change_nickname():
    nickname = request.form.get("nickname", "").strip()
    if nickname == g.user.nickname:
        flash("지금 쓰는 닉네임과 같아요.", "info")
    elif not NICKNAME_RE.match(nickname):
        flash(NICKNAME_RULE, "error")
    elif nickname_taken(nickname, except_user_id=g.user.id):
        flash("이미 쓰이고 있는 닉네임이에요.", "error")
    else:
        g.user.nickname = nickname
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("이미 쓰이고 있는 닉네임이에요.", "error")
        else:
            flash("닉네임을 바꿨어요.", "success")
    return redirect(url_for("meetings.me"))


@bp.post("/me/delete")
@login_required
def delete_account():
    """회원 탈퇴. 내가 남긴 정보를 모두 지우고, 영향을 받는 사람에게는 알림을 남긴다."""
    user = g.user
    if not check_password_hash(user.password_hash, request.form.get("password", "")):
        flash("비밀번호가 맞지 않아 탈퇴하지 못했어요.", "error")
        return redirect(url_for("meetings.me"))

    now = now_utc()
    # 1) 내가 연 모임: 아직 안 끝난 모임의 참여자에게 알리고 모임을 지운다(참여 기록·댓글도 함께 지워진다).
    for meeting in Meeting.query.filter_by(host_id=user.id).all():
        if meeting.status != "cancelled" and meeting.end_at > now:
            notify.cancelled(meeting, user, link=False)
        Notification.query.filter_by(meeting_id=meeting.id).update({"meeting_id": None}, synchronize_session=False)
        db.session.delete(meeting)
    db.session.flush()

    # 2) 남의 모임에 참여한 기록: 아직 시작 전이면 호스트에게 알리고 자리를 비운다.
    for participation in Participation.query.filter_by(user_id=user.id).all():
        if not participation.meeting.has_started and participation.meeting.status != "cancelled":
            notify.left(participation.meeting, user, anonymous=True)
        db.session.delete(participation)

    # 3) 내가 쓴 댓글, 받은 알림, 사용 기록
    Comment.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    Report.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    Notification.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    # 다른 사람의 알림함에 남아 있는 "○○님이 참여했어요" 같은 알림도 닉네임이 드러나므로 지운다.
    Notification.query.filter_by(actor_id=user.id).delete(synchronize_session=False)
    ChatUsage.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    LoginThrottle.query.filter_by(email=user.email).delete(synchronize_session=False)
    emailcode.forget(user.email)
    db.session.delete(user)
    db.session.commit()

    session.clear()
    flash("탈퇴가 끝났어요. 계정과 내가 남긴 글·참여 기록·알림을 지웠어요.", "info")
    return redirect(url_for("auth.login"))
