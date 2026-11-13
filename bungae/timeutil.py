"""시간 처리.

DB에는 시간대 정보가 없는 UTC 값을 저장하고, 화면과 챗봇 해석은 한국 시간(KST)으로 한다.
배포 서버의 시계는 UTC라서 이 변환을 한곳에 모아 두지 않으면 '오늘 저녁'이 9시간 어긋난다.
한국은 서머타임이 없으므로 고정 오프셋(+9)으로 충분하다.
"""

from datetime import datetime, timedelta, timezone

KST_OFFSET = timedelta(hours=9)
WEEKDAYS = "월화수목금토일"

_fixed_now = None


def set_fixed_now(dt):
    """테스트에서 현재 시각(UTC)을 고정한다. None이면 실제 시각을 쓴다."""
    global _fixed_now
    _fixed_now = dt


def now_utc():
    if _fixed_now is not None:
        return _fixed_now
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_kst(dt_utc):
    return dt_utc + KST_OFFSET


def from_kst(dt_kst):
    return dt_kst - KST_OFFSET


def now_kst():
    return to_kst(now_utc())


def day_label(dt_kst, today=None):
    today = today or now_kst().date()
    diff = (dt_kst.date() - today).days
    if diff == -1:
        return "어제"
    if diff == 0:
        return "오늘"
    if diff == 1:
        return "내일"
    if diff == 2:
        return "모레"
    return f"{dt_kst.month}/{dt_kst.day}({WEEKDAYS[dt_kst.weekday()]})"


def fmt_when(start_utc, duration_min=None):
    """'오늘 15:00~17:00' 형태."""
    start = to_kst(start_utc)
    text = f"{day_label(start)} {start:%H:%M}"
    if duration_min:
        end = start + timedelta(minutes=duration_min)
        text += f"~{end:%H:%M}"
    return text


def fmt_relative(start_utc):
    """'30분 뒤', '2시간 뒤' 형태. 이미 지났으면 빈 문자열."""
    minutes = int((start_utc - now_utc()).total_seconds() // 60)
    if minutes < 0:
        return ""
    if minutes < 1:
        return "곧 시작"
    if minutes < 60:
        return f"{minutes}분 뒤"
    hours = minutes // 60
    if hours < 24:
        rest = minutes % 60
        return f"{hours}시간 {rest}분 뒤" if rest and hours < 3 else f"{hours}시간 뒤"
    # 하루 넘게 남았으면 날짜 표시("모레 22:00")로 충분하다. "1일 뒤"를 붙이면 47시간 뒤인 모레와 어긋나 보인다.
    return ""


def fmt_duration(minutes):
    if minutes < 60:
        return f"{minutes}분"
    hours, rest = divmod(minutes, 60)
    return f"{hours}시간 {rest}분" if rest else f"{hours}시간"
