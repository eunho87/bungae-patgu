import re
from datetime import timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from . import db
from .constants import CATEGORY_KEYS
from .models import LoginThrottle, User
from .timeutil import now_utc

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[a-z0-9._%+-]{1,64}@soongsil\.ac\.kr$")
NICKNAME_RE = re.compile(r"^[0-9A-Za-z가-힣_]{2,12}$")
NICKNAME_RULE = "닉네임은 한글·영문·숫자·밑줄(_) 2~12자로 정해 주세요."
MAX_LOGIN_FAILS = 5
LOCK_MINUTES = 5

# 없는 계정으로 로그인할 때도 해시 계산 시간을 똑같이 써서 계정 존재 여부가 응답 시간으로 새지 않게 한다.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return {"error": "로그인이 필요해요."}, 401
            # POST 전용 주소로는 로그인 뒤에 돌아갈 수 없으므로(405) 그때는 홈으로 보낸다.
            target = request.full_path.rstrip("?") if request.method == "GET" else None
            return redirect(url_for("auth.login", next=target))
        return view(*args, **kwargs)

    return wrapped


def start_session(user):
    """로그인 상태를 만든다. 기존 세션 값은 버려 세션 고정 공격을 막는다."""
    session.clear()
    session["user_id"] = user.id
    session["pw"] = user.password_hash[-8:]
    session.permanent = True


def _safe_next(target):
    """로그인 뒤 돌아갈 주소는 사이트 내부 경로만 허용한다."""
    if not target or len(target) > 200:
        return None
    # 줄바꿈 같은 제어 문자는 응답 헤더에 넣을 수 없다.
    if any(ord(char) < 32 or ord(char) == 127 for char in target):
        return None
    parsed = urlparse(target)
    # 역슬래시는 일부 브라우저가 슬래시로 읽어 "/\evil.com"이 바깥 주소가 될 수 있다.
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


def nickname_taken(nickname, except_user_id=None):
    """대소문자만 다른 닉네임("Minsu"와 "minsu")도 같은 것으로 본다. 화면에서 구별하기 어려워 사칭에 쓰일 수 있다."""
    query = User.query.filter(func.lower(User.nickname) == nickname.lower())
    if except_user_id is not None:
        query = query.filter(User.id != except_user_id)
    return query.first() is not None


def _clean_interests(values):
    return ",".join(key for key in CATEGORY_KEYS if key in values)


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("discover.home"))
    errors = {}
    form = {"email": "", "nickname": "", "interests": [], "agree": False}
    # 공유 링크로 들어온 사람이 가입을 마치면 보려던 모임으로 바로 돌아가게 한다.
    next_url = _safe_next(request.values.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        nickname = request.form.get("nickname", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        interests = request.form.getlist("interests")
        agree = request.form.get("agree") == "1"
        form = {"email": email, "nickname": nickname, "interests": interests, "agree": agree}

        if not agree:
            errors["agree"] = "이용 안내를 확인하고 동의해 주세요."
        if not EMAIL_RE.match(email):
            errors["email"] = "숭실대학교 이메일(@soongsil.ac.kr)만 가입할 수 있어요."
        if not NICKNAME_RE.match(nickname):
            errors["nickname"] = NICKNAME_RULE
        if not 8 <= len(password) <= 64:
            errors["password"] = "비밀번호는 8자 이상 64자 이하로 정해 주세요."
        elif not password.strip():
            errors["password"] = "공백만으로 된 비밀번호는 쓸 수 없어요."
        elif password != confirm:
            errors["password_confirm"] = "비밀번호가 서로 달라요."

        if not errors:
            if User.query.filter_by(email=email).first():
                errors["email"] = "이미 가입된 이메일이에요."
            elif nickname_taken(nickname):
                errors["nickname"] = "이미 쓰이고 있는 닉네임이에요."

        if not errors:
            user = User(
                email=email,
                nickname=nickname,
                password_hash=generate_password_hash(password),
                interests=_clean_interests(interests),
            )
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                errors["email"] = "이미 가입된 이메일이거나 닉네임이에요."
            else:
                start_session(user)
                flash("가입을 환영해요! 지금 열려 있는 번개를 둘러보세요.", "success")
                return redirect(next_url or url_for("discover.home"))

    return (
        render_template("signup.html", errors=errors, form=form, next_url=next_url),
        400 if errors else 200,
    )


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("discover.home"))
    error = None
    email = ""
    next_url = _safe_next(request.values.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:120]
        password = request.form.get("password", "")
        now = now_utc()
        throttle = db.session.get(LoginThrottle, email) if email else None

        if throttle and throttle.locked_until and throttle.locked_until > now:
            error = f"로그인 시도가 너무 많아요. {LOCK_MINUTES}분 뒤에 다시 시도해 주세요."
        else:
            user = User.query.filter_by(email=email).first()
            ok = check_password_hash(user.password_hash if user else _DUMMY_HASH, password)
            if user and ok:
                if throttle:
                    db.session.delete(throttle)
                    db.session.commit()
                start_session(user)
                return redirect(next_url or url_for("discover.home"))

            error = "이메일 또는 비밀번호가 맞지 않아요."
            if email:
                if throttle is None:
                    throttle = LoginThrottle(email=email, fail_count=0)
                    db.session.add(throttle)
                if throttle.locked_until and throttle.locked_until <= now:
                    throttle.fail_count = 0
                    throttle.locked_until = None
                throttle.fail_count += 1
                if throttle.fail_count >= MAX_LOGIN_FAILS:
                    throttle.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                    throttle.fail_count = 0
                try:
                    db.session.commit()
                except IntegrityError:
                    # 같은 이메일로 실패 요청이 동시에 와서 기록 행을 둘이 만들려 한 경우
                    db.session.rollback()

    return render_template("login.html", error=error, email=email, next_url=next_url), (400 if error else 200)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
