"""메일로 보내는 6자리 확인 코드.

- 코드는 10분 동안만 유효하고, 틀린 입력은 5번까지만 받는다. 5번 틀리면 그 코드는 버리고 새로 받아야 한다.
- 같은 주소로는 1분에 한 번, 1시간에 5번까지만 보낸다. 남의 주소로 메일 폭탄을 보내는 데 쓰이지 않게 한다.
- 6자리라 경우의 수가 100만 개뿐이지만, 코드 하나에 5번, 한 시간에 코드 5개까지라 맞힐 확률은 1만분의 1보다 작다.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from flask import current_app

from . import db, mailer
from .models import EmailCode
from .timeutil import now_utc

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
RESEND_GAP = timedelta(seconds=60)
HOURLY_LIMIT = 5

SUBJECTS = {
    "verify": "[번개팟구] 가입 인증 코드",
    "reset": "[번개팟구] 비밀번호 재설정 코드",
}
BODIES = {
    "verify": "번개팟구 가입 인증 코드: {code}\n\n가입을 마치려면 10분 안에 이 코드를 입력해 주세요.\n직접 가입하지 않았다면 이 메일은 무시해도 돼요. 코드를 입력하지 않으면 계정은 만들어지지 않아요.\n",
    "reset": "번개팟구 비밀번호 재설정 코드: {code}\n\n10분 안에 이 코드를 입력하고 새 비밀번호를 정해 주세요.\n직접 요청하지 않았다면 이 메일은 무시해도 돼요. 비밀번호는 바뀌지 않아요.\n",
}


def _digest(email, purpose, code):
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, f"{purpose}:{email}:{code}".encode(), hashlib.sha256).hexdigest()


def wait_seconds(email, purpose):
    """지금 새 코드를 보낼 수 없으면 몇 초 기다려야 하는지, 보낼 수 있으면 0."""
    now = now_utc()
    recent = (
        EmailCode.query.filter(
            EmailCode.email == email,
            EmailCode.purpose == purpose,
            EmailCode.created_at > now - timedelta(hours=1),
        )
        .order_by(EmailCode.created_at)
        .all()
    )
    if not recent:
        return 0
    waits = [int((recent[-1].created_at + RESEND_GAP - now).total_seconds()) + 1]
    if len(recent) >= HOURLY_LIMIT:
        waits.append(int((recent[-HOURLY_LIMIT].created_at + timedelta(hours=1) - now).total_seconds()) + 1)
    return max(0, *waits)


def send(email, purpose):
    """새 코드를 만들어 메일로 보낸다. 앞서 보낸 코드는 쓸 수 없게 된다.

    발송에 실패하면 mailer.MailError를 그대로 올리고 아무것도 저장하지 않는다(재시도 제한에도 세지 않는다).
    """
    now = now_utc()
    EmailCode.query.filter(EmailCode.created_at < now - timedelta(days=1)).delete(synchronize_session=False)
    EmailCode.query.filter_by(email=email, purpose=purpose, used=False).update({"used": True}, synchronize_session=False)
    code = f"{secrets.randbelow(10**6):06d}"
    db.session.add(
        EmailCode(email=email, purpose=purpose, code_hash=_digest(email, purpose, code), created_at=now, expires_at=now + CODE_TTL)
    )
    try:
        mailer.send(email, SUBJECTS[purpose], BODIES[purpose].format(code=code))
    except mailer.MailError:
        db.session.rollback()
        raise
    db.session.commit()


def check(email, purpose, code):
    """'ok' / 'wrong'(남은 기회 있음) / 'locked'(기회를 다 씀) / 'expired' / 'none'(받은 코드가 없음).

    결과를 저장하려면 부른 쪽에서 commit 한다.
    """
    row = (
        EmailCode.query.filter_by(email=email, purpose=purpose, used=False)
        .order_by(EmailCode.created_at.desc())
        .first()
    )
    if row is None:
        return "none"
    if row.expires_at <= now_utc():
        return "expired"
    if row.attempts >= MAX_ATTEMPTS:
        return "locked"
    if hmac.compare_digest(row.code_hash, _digest(email, purpose, code)):
        row.used = True
        return "ok"
    row.attempts += 1
    return "locked" if row.attempts >= MAX_ATTEMPTS else "wrong"


def forget(email):
    EmailCode.query.filter_by(email=email).delete(synchronize_session=False)
