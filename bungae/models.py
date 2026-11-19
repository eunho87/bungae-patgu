from datetime import timedelta

from sqlalchemy import UniqueConstraint, true

from . import db
from .constants import CATEGORY_LABEL
from .timeutil import now_utc


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    nickname = db.Column(db.String(20), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    interests = db.Column(db.String(200), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc)
    # 학교 메일로 보낸 코드를 입력해 그 주소의 주인임을 확인했는지. 이 기능을 넣기 전에 가입한 계정은
    # 확인된 것으로 본다(DB에 칸을 새로 붙일 때 기본값 TRUE).
    email_verified = db.Column(db.Boolean, nullable=False, default=True, server_default=true())

    @property
    def interest_list(self):
        return [key for key in self.interests.split(",") if key]


class Meeting(db.Model):
    __tablename__ = "meetings"

    id = db.Column(db.Integer, primary_key=True)
    host_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(40), nullable=False)
    category = db.Column(db.String(20), nullable=False)
    start_at = db.Column(db.DateTime, nullable=False, index=True)  # UTC
    duration_min = db.Column(db.Integer, nullable=False)
    place = db.Column(db.String(60), nullable=False)
    capacity = db.Column(db.Integer, nullable=False)  # 호스트 포함 총원
    description = db.Column(db.Text, nullable=False, default="")
    tags = db.Column(db.String(120), nullable=False, default="")  # 쉼표로 구분
    contact_link = db.Column(db.String(200), nullable=False, default="")
    # 호스트의 의사와 신고로 인한 중단만 저장한다: open / closed(직접 마감) / blocked(신고 누적) / cancelled.
    # 정원이 찼는지, 시작 시간이 지났는지는 저장하지 않고 그때그때 계산한다.
    status = db.Column(db.String(12), nullable=False, default="open")
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc)

    host = db.relationship("User", lazy="joined")
    participations = db.relationship(
        "Participation",
        back_populates="meeting",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Participation.created_at",
    )
    comments = db.relationship(
        "Comment",
        back_populates="meeting",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="Comment.created_at",
    )
    reports = db.relationship("Report", back_populates="meeting", cascade="all, delete-orphan", lazy="select")

    @property
    def end_at(self):
        return self.start_at + timedelta(minutes=self.duration_min)

    @property
    def tag_list(self):
        return [tag for tag in self.tags.split(",") if tag]

    @property
    def joined_count(self):
        return len(self.participations)

    @property
    def seats_left(self):
        return max(self.capacity - self.joined_count, 0)

    @property
    def is_full(self):
        return self.joined_count >= self.capacity

    @property
    def has_started(self):
        return self.start_at <= now_utc()

    @property
    def state(self):
        """화면에 보여 줄 상태: cancelled / ongoing / ended / closed / full / open."""
        if self.status == "cancelled":
            return "cancelled"
        if self.has_started:
            return "ongoing" if self.end_at > now_utc() else "ended"
        if self.status == "blocked":
            return "blocked"
        if self.status == "closed":
            return "closed"
        if self.is_full:
            return "full"
        return "open"

    @property
    def joinable(self):
        return self.state == "open"

    @property
    def closing_soon(self):
        """모집 중인데 한 자리만 남은 모임. 2명짜리 모임은 처음부터 한 자리뿐이라 치지 않는다."""
        return self.state == "open" and self.seats_left == 1 and self.capacity > 2

    @property
    def starting_soon(self):
        """모집 중이고 1시간 안에 시작하는 모임.

        자리가 넉넉한데도 '마감 임박'이라고 하면 틀린 말이라, 시간이 임박한 경우는 따로 '곧 시작'으로 알린다.
        """
        return self.state == "open" and self.start_at - now_utc() <= timedelta(hours=1)

    @property
    def counted_reports(self):
        """모집을 멈추는 데 세는 신고 수.

        가입은 이메일 형식만 확인하므로, 방금 만든 계정 여러 개로 남의 모임을 닫아 버릴 수 있다.
        그래서 가입한 지 하루가 지난 계정이나 그 모임에 실제로 참여한 사람의 신고만 센다.
        """
        cutoff = now_utc() - timedelta(hours=24)
        members = {p.user_id for p in self.participations}
        return sum(1 for r in self.reports if r.user.id in members or r.user.created_at <= cutoff)

    def has_participant(self, user):
        return user is not None and any(p.user_id == user.id for p in self.participations)

    def overlaps(self, other):
        return self.start_at < other.end_at and other.start_at < self.end_at

    @property
    def search_text(self):
        # 종류 이름("식사")으로도 찾을 수 있게 표시 이름을 함께 넣는다.
        label = CATEGORY_LABEL.get(self.category, "")
        return " ".join([self.title, self.tags, self.description, self.place, label]).lower()


class Participation(db.Model):
    __tablename__ = "participations"
    __table_args__ = (UniqueConstraint("meeting_id", "user_id", name="uq_participation"),)

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc)

    meeting = db.relationship("Meeting", back_populates="participations")
    user = db.relationship("User", lazy="joined")


class Comment(db.Model):
    __tablename__ = "comments"

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc)

    meeting = db.relationship("Meeting", back_populates="comments")
    user = db.relationship("User", lazy="joined")


class Report(db.Model):
    """모임 신고. 한 사람이 한 모임을 한 번만 신고할 수 있다."""

    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("meeting_id", "user_id", name="uq_report"),)

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reason = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc)

    meeting = db.relationship("Meeting", back_populates="reports")
    user = db.relationship("User", lazy="joined")


class Notification(db.Model):
    """사이트 안 알림. 푸시를 보낼 수 없으므로 다음에 접속했을 때 종 아이콘으로 알려 준다."""

    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)  # 받는 사람
    # 모임이 지워지면 DB가 이 칸을 비운다(알림 자체는 남는다).
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id", ondelete="SET NULL"), index=True)
    # 알림을 일으킨 사람. 그 사람이 탈퇴하면 닉네임이 적힌 알림을 함께 지우기 위해 둔다.
    actor_id = db.Column(db.Integer, index=True)
    message = db.Column(db.String(200), nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc)


class LoginThrottle(db.Model):
    """이메일별 로그인 실패 횟수. 서버리스에서는 메모리에 둘 수 없어 DB에 둔다."""

    __tablename__ = "login_throttles"

    email = db.Column(db.String(120), primary_key=True)
    fail_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)


class EmailCode(db.Model):
    """메일로 보낸 6자리 코드. 원래 숫자는 저장하지 않고 서버 비밀 키로 만든 해시만 둔다.

    purpose는 'verify'(가입 확인) 또는 'reset'(비밀번호 재설정). 발송 간격과 시간당 횟수 제한도 이 행들로 센다.
    """

    __tablename__ = "email_codes"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    purpose = db.Column(db.String(10), nullable=False)
    code_hash = db.Column(db.String(64), nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    used = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc)
    expires_at = db.Column(db.DateTime, nullable=False)


class ChatUsage(db.Model):
    """사용자별 하루 AI 해석 사용 횟수."""

    __tablename__ = "chat_usages"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_chat_usage"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    day = db.Column(db.String(10), nullable=False)  # KST 날짜 YYYY-MM-DD
    count = db.Column(db.Integer, nullable=False, default=0)
