"""템플릿에서 쓰는 필터와 공통 값."""

from urllib.parse import urlencode

from flask import g

from . import constants, notify
from .timeutil import fmt_duration, fmt_relative, fmt_when, to_kst

STATE_LABEL = {
    "open": "모집중",
    "full": "정원 마감",
    "closed": "모집 마감",
    "blocked": "신고로 중단",
    "ongoing": "진행 중",
    "ended": "종료",
    "cancelled": "취소됨",
}


def google_calendar_url(meeting):
    """구글 캘린더 '일정 추가' 화면으로 가는 링크. 시간은 UTC로 넘긴다."""
    fmt = "%Y%m%dT%H%M%SZ"
    params = {
        "action": "TEMPLATE",
        "text": f"[번개팟구] {meeting.title}",
        "dates": f"{meeting.start_at.strftime(fmt)}/{meeting.end_at.strftime(fmt)}",
        "location": meeting.place,
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def register_template_helpers(app):
    @app.context_processor
    def inject_common():
        user = g.get("user")
        return {
            "user": user,
            "unread_notifications": notify.unread_count(user) if user else 0,
            "CATEGORIES": constants.CATEGORIES,
            "CATEGORY_LABEL": constants.CATEGORY_LABEL,
            "CATEGORY_EMOJI": constants.CATEGORY_EMOJI,
            "PRESET_TAGS": constants.PRESET_TAGS,
            "STATE_LABEL": STATE_LABEL,
            "ephemeral_db": app.config.get("EPHEMERAL_DB", False),
        }

    @app.template_filter("when")
    def when_filter(meeting):
        return fmt_when(meeting.start_at, meeting.duration_min)

    @app.template_filter("relative")
    def relative_filter(meeting):
        return fmt_relative(meeting.start_at)

    @app.template_filter("duration")
    def duration_filter(minutes):
        return fmt_duration(minutes)

    @app.template_filter("stamp")
    def stamp_filter(dt_utc):
        return to_kst(dt_utc).strftime("%m/%d %H:%M")

    @app.template_filter("gcal")
    def gcal_filter(meeting):
        return google_calendar_url(meeting)
