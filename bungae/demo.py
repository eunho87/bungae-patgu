"""시연용 예시 데이터.

두 가지 경로로 쓰인다.
  - seed.py: 내 컴퓨터에서 직접 실행해 넣는다(데모 계정으로 로그인할 수 있게 비밀번호를 정한다).
  - auto_seed(): 배포 환경에서 SEED_DEMO=1 이면 서버가 스스로 채운다. 예시 모임은 "1시간 뒤", "내일 점심"처럼
    만든 시각 기준이라 하루 이틀이면 모두 지나간다. 사람이 그때마다 명령을 돌리지 않아도 되게 한 것이다.
    이 경로로 만든 데모 계정은 아무도 모르는 무작위 비밀번호를 가지므로 로그인할 수 없다(모임을 연 사람 역할만 한다).
"""

import secrets
from datetime import datetime, time, timedelta

from sqlalchemy import text
from werkzeug.security import generate_password_hash

from . import db
from .models import Comment, Meeting, Notification, Participation, User
from .timeutil import from_kst, now_kst, now_utc

DEMO_USERS = [
    ("demo1@ssu.ac.kr", "번개장인", "game,meal"),
    ("demo2@ssu.ac.kr", "도서관지박령", "study,cafe"),
    ("demo3@ssu.ac.kr", "풋살왕", "sports,drink"),
    ("demo4@ssu.ac.kr", "고학번선배", "meal,culture"),
    ("demo5@ssu.ac.kr", "새내기", "cafe,game"),
]
# 가입 주소를 @ssu.ac.kr 로 바꾸기 전에 만든 데모 계정의 주소. 이미 있는 DB에서는 이 주소의 계정을 그대로 이어 쓴다.
LEGACY_DOMAIN = "@soongsil.ac.kr"
MIN_UPCOMING = 3  # 앞으로 열릴 예시 모임이 이보다 적으면 새로 채운다
ADVISORY_LOCK_ID = 74120261  # 여러 서버 인스턴스가 동시에 채우지 않게 하는 잠금 번호(임의의 고정 값)


def plan(now):
    """(호스트 번호, 시작 시각(KST), 모임 정보, 참여자 번호들) 목록. 지금 시각에 맞춰 매번 새로 계산한다."""
    base = now.replace(second=0, microsecond=0) + timedelta(minutes=(-now.minute) % 30 or 30)
    today, tomorrow = now.date(), now.date() + timedelta(days=1)

    def at(day, hour, minute=0):
        when = datetime.combine(day, time(hour, minute))
        return when if when > now + timedelta(minutes=20) else when + timedelta(days=1)

    return [
        (4, base + timedelta(minutes=30), dict(title="공강에 커피 한 잔 하실 분", category="cafe", duration_min=60,
            place="학생회관", capacity=2, tags="가볍게,초면환영", description="수업 사이에 시간이 떠서요. 가볍게 커피 마셔요."), []),
        (0, base + timedelta(hours=1), dict(title="학생회관에서 보드게임 한 판", category="game", duration_min=120,
            place="학생회관", capacity=4, tags="초면환영,가볍게", description="스플렌더랑 루미큐브 가져갑니다. 처음 하는 분도 알려 드려요."), [2]),
        (3, base + timedelta(hours=1, minutes=30), dict(title="학식 같이 먹어요", category="meal", duration_min=60,
            place="학생회관", capacity=3, tags="혼자와도OK,고학번환영", description="혼밥이 지겨운 분들 모여요. 학번 안 따집니다."), []),
        (1, base + timedelta(hours=2), dict(title="중앙도서관 모각공 2시간", category="study", duration_min=120,
            place="중앙도서관", capacity=6, tags="조용히,진지하게", description="각자 할 일 하고 끝나면 인사만 해요. 말 걸지 않기!"), [4]),
        (2, base + timedelta(hours=3), dict(title="풋살 한 게임 하실 분", category="sports", duration_min=90,
            place="운동장", capacity=10, tags="초면환영", description="인원 모이면 5대5로 가볍게 뜁니다. 운동화만 챙겨 오세요."), [0, 3]),
        (3, at(today, 18, 30), dict(title="저녁 마라탕 번개", category="meal", duration_min=90,
            place="숭실대입구역 근처", capacity=4, tags="수다,고학번환영", description="맵기는 각자 고르는 걸로. 다 먹고 헤어져요."), [1]),
        (2, at(today, 19, 30), dict(title="과제 끝! 치맥 한잔", category="drink", duration_min=120,
            place="상도역 근처", capacity=6, tags="수다", description="과제 낸 사람만 올 수 있음(농담). 1차만 하고 갑니다."), [3]),
        (0, at(tomorrow, 12, 0), dict(title="점심 국밥 같이 드실 분", category="meal", duration_min=60,
            place="숭실대입구역 근처", capacity=4, tags="혼자와도OK", description="12시에 역 3번 출구 앞에서 만나요."), []),
        (4, at(tomorrow, 15, 0), dict(title="카페에서 수다 떨어요", category="cafe", duration_min=90,
            place="숭실대입구역 근처", capacity=4, tags="새내기환영,수다", description="학교 생활 얘기 나눠요. 선배님들 환영합니다."), [3]),
        (1, at(tomorrow, 17, 0), dict(title="코노 1시간만", category="culture", duration_min=60,
            place="상도역 근처", capacity=3, tags="가볍게", description="스트레스 풀러 가요. 딱 한 시간만!"), []),
        (1, at(tomorrow + timedelta(days=1), 14, 0), dict(title="알고리즘 스터디 맛보기", category="study", duration_min=120,
            place="정보과학관", capacity=5, tags="진지하게", description="쉬운 문제 두 개를 같이 풀고 풀이를 비교해 봐요."), [4]),
    ]


def ensure_users(password=None):
    """데모 계정이 없으면 만든다. (계정 목록, 새로 만든 계정 수)를 돌려준다.

    password가 None이면 계정마다 아무도 모르는 무작위 비밀번호를 준다(로그인 불가).
    """
    users = []
    for email, _, _ in DEMO_USERS:
        user = User.query.filter_by(email=email).first()
        if user is None:
            user = User.query.filter_by(email=_legacy(email)).first()
            if user is not None:
                user.email = email
        users.append(user)
    created = 0
    for index, (email, nickname, interests) in enumerate(DEMO_USERS):
        if users[index] is None:
            if User.query.filter_by(nickname=nickname).first() is not None:
                # 실제 가입자가 같은 닉네임을 먼저 쓰고 있다. 닉네임은 겹칠 수 없으므로 번호를 붙인다.
                nickname = f"{nickname}{index + 1}"
            secret = password if password is not None else secrets.token_urlsafe(24)
            users[index] = User(
                email=email, nickname=nickname, interests=interests, password_hash=generate_password_hash(secret)
            )
            db.session.add(users[index])
            created += 1
    db.session.flush()
    return users, created


def _legacy(email):
    return email.split("@")[0] + LEGACY_DOMAIN


def demo_meetings(users):
    return Meeting.query.filter(Meeting.host_id.in_([user.id for user in users])).all()


def clear_meetings(users):
    """데모 계정이 연 모임을 지운다. 그 모임을 가리키던 알림은 남기되 연결만 끊는다."""
    existing = demo_meetings(users)
    if existing:
        Notification.query.filter(Notification.meeting_id.in_([m.id for m in existing])).update(
            {"meeting_id": None}, synchronize_session=False
        )
    for meeting in existing:
        db.session.delete(meeting)
    db.session.flush()
    return len(existing)


def create_meetings(users):
    created = []
    for host, start_kst, info, joiners in plan(now_kst()):
        meeting = Meeting(host_id=users[host].id, start_at=from_kst(start_kst), **info)
        meeting.participations.append(Participation(user_id=users[host].id))
        for joiner in joiners:
            meeting.participations.append(Participation(user_id=users[joiner].id))
        db.session.add(meeting)
        created.append(meeting)
    db.session.flush()
    db.session.add(Comment(meeting_id=created[1].id, user_id=users[4].id, body="보드게임 처음인데 가도 되나요?"))
    db.session.add(Comment(meeting_id=created[1].id, user_id=users[0].id, body="그럼요! 규칙부터 알려 드릴게요."))
    return created


def auto_seed():
    """앞으로 열릴 예시 모임이 모자라면 새로 채운다. 만든 모임 수를 돌려준다(채우지 않았으면 0).

    지난 예시 모임은 지우지 않는다. 실제 사용자가 참여했거나 댓글을 남겼을 수 있고, 지난 모임은 목록에 나오지 않는다.
    """
    # 서버가 새로 뜰 때마다 불리므로, 대부분의 경우(이미 충분함)에는 조회 한 번으로 끝낸다.
    if upcoming_count() >= MIN_UPCOMING:
        db.session.rollback()
        return 0
    if db.engine.dialect.name == "postgresql":
        # 서버 인스턴스 둘이 동시에 뜨면 같은 모임을 두 벌 만들 수 있다. 먼저 잡은 쪽만 진행한다(잠금은 commit 때 풀린다).
        locked = db.session.execute(text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": ADVISORY_LOCK_ID}).scalar()
        if not locked or upcoming_count() >= MIN_UPCOMING:
            db.session.rollback()
            return 0
    users, _ = ensure_users()
    created = create_meetings(users)
    db.session.commit()
    return len(created)


def upcoming_count():
    """데모 계정이 연 모임 가운데 30분 넘게 남은, 모집 중인 것의 수."""
    emails = [email for email, _, _ in DEMO_USERS]
    return (
        Meeting.query.join(User, Meeting.host_id == User.id)
        .filter(
            User.email.in_(emails + [_legacy(email) for email in emails]),
            Meeting.status == "open",
            Meeting.start_at > now_utc() + timedelta(minutes=30),
        )
        .count()
    )
