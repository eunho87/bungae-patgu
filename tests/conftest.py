"""공통 픽스처.

- 테스트마다 새 앱과 새 메모리 DB를 만든다.
- 현재 시각은 2026-11-12 05:00 UTC(= 목요일 14:00 KST)로 고정한다.
- bungae.llm 안의 requests.post는 기본적으로 막아 둔다(실제 네트워크 호출 금지).
"""

import smtplib
from datetime import datetime, timedelta

import pytest
from flask import template_rendered
from sqlalchemy import event
from werkzeug.security import generate_password_hash

from bungae import create_app, db, llm, timeutil
from bungae.models import Comment, Meeting, Participation, User

NOW_UTC = datetime(2026, 11, 12, 5, 0)
NOW_KST = datetime(2026, 11, 12, 14, 0)
TODAY = NOW_KST.date()
PASSWORD = "correct-horse-1"

BASE_CONFIG = {
    "SECRET_KEY": "test",
    "SQLALCHEMY_DATABASE_URI": "sqlite://",
    "TESTING": True,
    "WTF_CSRF_ENABLED": False,
    "OPENROUTER_API_KEY": "",
    "OPENROUTER_MODEL": "test/model:free",
    "OPENROUTER_FALLBACK_MODELS": "none",
    "CHAT_AI_DAILY_LIMIT": 15,
    # 로컬 .env에 메일 설정이 있어도 테스트에서는 이메일 인증을 끄고 메일을 보내지 않는다(인증 테스트는 따로 켠다).
    "EMAIL_VERIFICATION": False,
    "MAIL_USERNAME": "",
    "MAIL_PASSWORD": "",
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_template: 응답이 bungae/templates 의 템플릿을 그리는 테스트 (템플릿이 없으면 TemplateNotFound)",
    )


def kst(year, month, day, hour=0, minute=0):
    """KST 시각을 DB에 넣는 UTC 값으로 바꾼다."""
    return datetime(year, month, day, hour, minute) - timedelta(hours=9)


# --- SQLite를 배포 DB(PostgreSQL)만큼 엄격하게 만들기 ----------------------------
# 테스트는 SQLite로 돌지만 실제 서비스는 PostgreSQL을 쓴다. SQLite는 두 가지를 눈감아 준다.
#   1) 외래 키: 가리키는 행이 없어도, 가리킴을 받는 행을 지워도 통과한다.
#   2) 글자 수: String(40) 칸에 100자를 넣어도 통과한다.
# PostgreSQL은 둘 다 오류를 낸다. 그래서 테스트에서는 두 검사를 강제로 켠다.


def _enforce_foreign_keys(app):
    with app.app_context():
        with db.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        db.session.remove()


@event.listens_for(db.session, "before_flush")
def _enforce_string_lengths(session, flush_context, instances):
    for obj in list(session.new) + list(session.dirty):
        for column in obj.__table__.columns:
            limit = getattr(column.type, "length", None)
            value = getattr(obj, column.key, None)
            if limit and isinstance(value, str) and len(value) > limit:
                raise AssertionError(
                    f"{type(obj).__name__}.{column.key}에 {len(value)}자를 넣으려 했다(한도 {limit}자). PostgreSQL에서는 오류다."
                )


@pytest.fixture(autouse=True)
def fixed_now():
    timeutil.set_fixed_now(NOW_UTC)
    yield
    timeutil.set_fixed_now(None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("테스트에서 실제 네트워크 호출이 일어났다")

    monkeypatch.setattr(llm.requests, "post", blocked)
    monkeypatch.setattr(smtplib, "SMTP", blocked)


@pytest.fixture
def make_app():
    created = []

    def factory(**overrides):
        app = create_app({**BASE_CONFIG, **overrides})
        _enforce_foreign_keys(app)
        created.append(app)
        return app

    yield factory
    for app in created:
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


class Data:
    """DB에 직접 테스트 데이터를 넣고 읽는다. 객체 대신 id를 돌려줘 세션 경계 문제를 피한다."""

    def __init__(self, app):
        self.app = app
        self._n = 0

    def user(self, nickname=None, interests="", email=None, password=PASSWORD):
        self._n += 1
        nickname = nickname or f"user{self._n}"
        email = email or f"{nickname.lower()}@soongsil.ac.kr"
        with self.app.app_context():
            user = User(
                email=email,
                nickname=nickname,
                # 반복 횟수를 낮춰 테스트를 빠르게 한다(검증 방식은 같다).
                password_hash=generate_password_hash(password, method="pbkdf2:sha256:1000"),
                interests=interests,
            )
            db.session.add(user)
            db.session.commit()
            return user.id

    def meeting(
        self,
        host_id,
        title="보드게임 한 판",
        category="game",
        start_at=None,
        duration_min=60,
        place="학생회관",
        capacity=4,
        description="",
        tags="",
        contact_link="",
        status="open",
        created_at=None,
        members=(),
    ):
        with self.app.app_context():
            meeting = Meeting(
                host_id=host_id,
                title=title,
                category=category,
                start_at=start_at or NOW_UTC + timedelta(hours=2),
                duration_min=duration_min,
                place=place,
                capacity=capacity,
                description=description,
                tags=tags,
                contact_link=contact_link,
                status=status,
            )
            if created_at is not None:
                meeting.created_at = created_at
            meeting.participations.append(Participation(user_id=host_id))
            for user_id in members:
                meeting.participations.append(Participation(user_id=user_id))
            db.session.add(meeting)
            db.session.commit()
            return meeting.id

    def add_member(self, meeting_id, user_id):
        with self.app.app_context():
            db.session.add(Participation(meeting_id=meeting_id, user_id=user_id))
            db.session.commit()

    def comment(self, meeting_id, user_id, body="안녕하세요"):
        with self.app.app_context():
            comment = Comment(meeting_id=meeting_id, user_id=user_id, body=body)
            db.session.add(comment)
            db.session.commit()
            return comment.id

    def member_ids(self, meeting_id):
        with self.app.app_context():
            rows = Participation.query.filter_by(meeting_id=meeting_id).order_by(Participation.id).all()
            return [row.user_id for row in rows]

    def meeting_field(self, meeting_id, name):
        with self.app.app_context():
            return getattr(db.session.get(Meeting, meeting_id), name)

    def count(self, model):
        with self.app.app_context():
            return model.query.count()


@pytest.fixture
def data(app):
    return Data(app)


def login_as(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


@pytest.fixture
def as_user(app):
    """as_user(user_id) -> 그 사용자로 로그인된 새 테스트 클라이언트."""

    def factory(user_id):
        client = app.test_client()
        login_as(client, user_id)
        return client

    return factory


@pytest.fixture
def rendered(app):
    """그려진 템플릿의 (이름, 컨텍스트) 기록. 템플릿을 대체하지 않고 실제 렌더링 뒤에 받는다."""
    records = []

    def record(sender, template, context, **extra):
        records.append((template.name, context))

    template_rendered.connect(record, app)
    yield records
    template_rendered.disconnect(record, app)
