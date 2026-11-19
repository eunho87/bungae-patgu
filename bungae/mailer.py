"""메일 발송.

SMTP로 보낸다. 기본값은 Gmail(smtp.gmail.com:587)이고, 조원 한 명의 Gmail 주소와 그 계정에서 만든
'앱 비밀번호'(16자리)를 MAIL_USERNAME, MAIL_PASSWORD 환경 변수에 넣으면 된다.
설정이 없으면 보내지 않고 내용을 서버 로그에 남긴다(로컬 개발과 테스트용).
"""

import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from flask import current_app


class MailError(Exception):
    """메일 서버에 연결하지 못했거나 보내기를 거절당했다."""


def configured(config):
    return bool(config.get("MAIL_USERNAME") and config.get("MAIL_PASSWORD"))


def send(to, subject, body):
    config = current_app.config
    if not configured(config):
        current_app.logger.warning("메일 발송 설정이 없어 보내지 않고 로그에 남깁니다.\n받는 사람: %s\n제목: %s\n%s", to, subject, body)
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("번개팟구", config.get("MAIL_FROM") or config["MAIL_USERNAME"]))
    message["To"] = to
    message.set_content(body)
    try:
        with smtplib.SMTP(config["MAIL_SMTP_HOST"], config["MAIL_SMTP_PORT"], timeout=10) as smtp:
            smtp.starttls()
            smtp.login(config["MAIL_USERNAME"], config["MAIL_PASSWORD"])
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as error:
        current_app.logger.exception("메일 발송 실패: %s", to)
        raise MailError(str(error)) from error
