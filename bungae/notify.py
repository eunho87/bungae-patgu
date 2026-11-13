"""사이트 안 알림을 만드는 함수 모음.

알림은 "누가 내 모임에 들어왔다", "참여한 모임이 취소됐다"처럼 다른 사람의 행동 때문에
내가 알아야 할 일이 생겼을 때 만든다. 자기 행동에 대해서는 만들지 않는다.
여기 함수들은 세션에 추가만 하고 commit은 부르는 쪽에서 한 번에 한다.
"""

from . import db
from .models import Comment, Notification
from .timeutil import fmt_when

MESSAGE_MAX = 200
KEEP_PER_USER = 50


def short(title, limit=24):
    return title if len(title) <= limit else title[: limit - 1] + "…"


def push(user_id, message, meeting=None, actor=None):
    message = message[:MESSAGE_MAX]
    meeting_id = meeting.id if meeting else None
    # 참여와 취소를 되풀이해 남의 알림함을 채우는 일을 막는다: 아직 읽지 않은 같은 알림이 있으면 새로 만들지 않는다.
    duplicate = Notification.query.filter_by(
        user_id=user_id, meeting_id=meeting_id, message=message, is_read=False
    ).first()
    if duplicate is not None:
        # 대신 예전 것을 지우고 새로 넣어 맨 위로 보낸다(건수는 그대로다). 그대로 두면
        # "참여 → 취소 → 다시 참여"했을 때 알림함 맨 위가 '취소했어요'로 남아 실제 상태와 반대가 된다.
        db.session.delete(duplicate)
    db.session.add(
        Notification(
            user_id=user_id,
            meeting_id=meeting_id,
            message=message,
            actor_id=actor.id if actor else None,
        )
    )


def to_host(meeting, actor, message, named=True):
    """모임 호스트에게 알린다. 호스트 본인의 행동이면 알리지 않는다."""
    if actor.id != meeting.host_id:
        push(meeting.host_id, message, meeting, actor if named else None)


def to_participants(meeting, actor, message, link=True, also=()):
    """호스트를 뺀 참여자 전원에게 알린다(행동한 사람 본인은 제외). 문구에 닉네임을 넣지 않는다.

    also: 참여자는 아니지만 함께 알려야 하는 사용자 id들.
    """
    targets = {p.user_id for p in meeting.participations} | set(also)
    for user_id in sorted(targets - {actor.id, meeting.host_id}):
        push(user_id, message, meeting if link else None)


def joined(meeting, actor):
    count = f"{meeting.joined_count}/{meeting.capacity}명"
    text = f"{actor.nickname}님이 ‘{short(meeting.title)}’에 참여했어요. ({count})"
    if meeting.is_full:
        text += " 정원이 다 찼어요!"
    to_host(meeting, actor, text)


def left(meeting, actor, anonymous=False):
    """anonymous: 탈퇴 때문에 빠지는 경우. 지워질 닉네임을 알림에 남기지 않는다."""
    if anonymous:
        to_host(meeting, actor, f"참여자 한 명이 탈퇴해 ‘{short(meeting.title)}’에 자리가 났어요.", named=False)
    else:
        to_host(meeting, actor, f"{actor.nickname}님이 ‘{short(meeting.title)}’ 참여를 취소했어요.")


def commented(meeting, actor):
    if actor.id == meeting.host_id:
        # 호스트의 댓글은 공지이자 답변이다. 참여자뿐 아니라 그 모임에 질문을 남긴 사람에게도 알린다.
        askers = {
            user_id
            for (user_id,) in db.session.query(Comment.user_id).filter_by(meeting_id=meeting.id).distinct()
        }
        to_participants(meeting, actor, f"호스트가 ‘{short(meeting.title)}’에 댓글을 남겼어요.", also=askers)
    else:
        to_host(meeting, actor, f"{actor.nickname}님이 ‘{short(meeting.title)}’에 댓글을 남겼어요.")


def changed(meeting, actor):
    """모임 이름·시간·장소가 바뀌었을 때. 무엇으로 바뀌었는지 알림만 봐도 알 수 있게 적는다."""
    when = fmt_when(meeting.start_at, meeting.duration_min)
    to_participants(
        meeting,
        actor,
        f"참여한 모임의 정보가 바뀌었어요: ‘{short(meeting.title)}’ · {when} · {short(meeting.place, 20)}",
    )


def link_added(meeting, actor):
    to_participants(meeting, actor, f"모임 ‘{short(meeting.title)}’에 오픈채팅 링크가 올라왔어요.")


def cancelled(meeting, actor, link=True):
    to_participants(meeting, actor, f"참여하려던 모임 ‘{short(meeting.title)}’이 취소됐어요.", link=link)


def unread_count(user):
    return Notification.query.filter_by(user_id=user.id, is_read=False).count()


def recent(user, limit=KEEP_PER_USER):
    return (
        Notification.query.filter_by(user_id=user.id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
        .all()
    )


def prune(user):
    """오래된 알림을 지워 한 사람당 최근 것만 남긴다."""
    keep = [n.id for n in recent(user)]
    if len(keep) == KEEP_PER_USER:
        Notification.query.filter(Notification.user_id == user.id, Notification.id.notin_(keep)).delete(
            synchronize_session=False
        )
