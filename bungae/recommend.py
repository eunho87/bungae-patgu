"""조건에 맞는 모임을 고르고 점수를 매긴다.

홈의 '나를 위한 추천', 모임 상세의 '비슷한 모임', 챗봇이 모두 이 모듈 하나를 쓴다.
어떤 모임을 보여 줄지는 항상 이 코드가 DB에서 직접 고르고, 언어 모델은 관여하지 않는다.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .constants import CATEGORY_LABEL, DURATIONS
from .models import Meeting, Participation
from .timeutil import day_label, fmt_duration, now_utc, to_kst

NEAR_MINUTES = 30  # 이만큼까지 늦게 끝나는 모임은 '거의 맞는' 후보로 남긴다


@dataclass
class Conditions:
    window_start: datetime | None = None  # UTC. 모임 시작이 이 시각 이후여야 한다
    window_end: datetime | None = None  # UTC
    must_end_in_window: bool = False  # True면 모임이 window_end 전에 끝나야 한다
    max_duration_min: int | None = None
    categories: list = field(default_factory=list)
    keywords: list = field(default_factory=list)

    @property
    def has_time(self):
        return bool(self.window_start or self.window_end or self.max_duration_min)

    @property
    def expired(self):
        """말한 시간대가 이미 다 지났는지."""
        return self.window_end is not None and self.window_end <= now_utc()

    @property
    def is_empty(self):
        return not (self.has_time or self.categories or self.keywords)


@dataclass
class Recommendation:
    meeting: Meeting
    score: int
    reasons: list


def open_meetings(user=None, limit=200, candidates=None, mine=None):
    """지금 참여할 수 있는 모임. 내가 이미 들어가 있거나 내 일정과 겹치는 모임은 뺀다.

    candidates, mine: 부르는 쪽이 이미 읽어 둔 목록이 있으면 넘겨서 같은 조회를 되풀이하지 않는다.
    """
    now = now_utc()
    if candidates is None:
        candidates = (
            Meeting.query.filter(Meeting.status == "open", Meeting.start_at > now)
            .order_by(Meeting.start_at)
            .limit(limit)
            .all()
        )
    meetings = [m for m in candidates if m.status == "open" and m.start_at > now and not m.is_full]
    if user is None:
        return meetings
    if mine is None:
        mine = my_upcoming(user)
    mine_ids = {m.id for m in mine}
    return [m for m in meetings if m.id not in mine_ids and not any(m.overlaps(o) for o in mine)]


def my_upcoming(user):
    """내가 참여 중이고 아직 끝나지 않은 모임."""
    now = now_utc()
    meetings = (
        Meeting.query.join(Participation)
        .filter(
            Participation.user_id == user.id,
            Meeting.status != "cancelled",
            # 모임은 길어야 4시간이므로 그보다 전에 시작한 것은 이미 끝났다. 지난 기록까지 다 읽지 않는다.
            Meeting.start_at > now - timedelta(hours=6),
        )
        .all()
    )
    return [m for m in meetings if m.end_at > now]


def time_fit(meeting, cond):
    """('fit' | 'near' | 'miss' | 'none', 설명)"""
    if not cond.has_time:
        return "none", None
    near_reason = None

    if cond.max_duration_min and meeting.duration_min > cond.max_duration_min:
        over = meeting.duration_min - cond.max_duration_min
        if over > NEAR_MINUTES:
            return "miss", None
        near_reason = f"{over}분 더 걸려요"

    if cond.window_start and meeting.start_at < cond.window_start:
        return "miss", None

    if cond.window_end:
        if cond.must_end_in_window:
            over = int((meeting.end_at - cond.window_end).total_seconds() // 60)
            if meeting.start_at >= cond.window_end or over > NEAR_MINUTES:
                return "miss", None
            if over > 0:
                near_reason = f"{over}분 늦게 끝나요"
        elif meeting.start_at >= cond.window_end:
            return "miss", None

    if near_reason:
        return "near", near_reason
    return "fit", "시간이 맞아요"


def _score(meeting, cond, interests):
    fit, time_reason = time_fit(meeting, cond)
    cat_ok = not cond.categories or meeting.category in cond.categories
    text = meeting.search_text
    hits = [kw for kw in cond.keywords if kw.lower() in text]

    score = 0
    reasons = []
    if fit == "fit":
        score += 40
        reasons.append(time_reason)
    elif fit == "near":
        score += 20
        reasons.append(time_reason)
    if cond.categories and cat_ok:
        score += 30
        reasons.append(f"{CATEGORY_LABEL[meeting.category]} 모임")
    for kw in hits[:3]:
        score += 10
        reasons.append(f"'{kw}' 포함")
    if meeting.category in interests:
        score += 15
        if not (cond.categories and cat_ok):
            reasons.append("내 관심사")
    until_start = meeting.start_at - now_utc()
    if until_start <= timedelta(hours=2):
        score += 10
        if len(reasons) < 3:
            reasons.append("곧 시작해요")
    elif until_start <= timedelta(hours=6):
        score += 5
    if meeting.seats_left == 1:
        score += 3
        if len(reasons) < 3:
            reasons.append("마지막 한 자리")
    return fit, cat_ok, hits, score, reasons[:3]


def recommend(cond, user=None, limit=5, pool=None):
    """(조건에 맞는 추천, 조건을 조금 푼 대안) 을 돌려준다. 대안은 추천이 하나도 없을 때만 채운다.

    pool: open_meetings()의 결과를 이미 갖고 있으면 넘긴다.
    """
    interests = user.interest_list if user else []
    strict, loose = [], []
    for meeting in pool if pool is not None else open_meetings(user):
        fit, cat_ok, hits, score, reasons = _score(meeting, cond, interests)
        keywords_only = bool(cond.keywords) and not cond.categories and not cond.has_time
        matched = fit != "miss" and cat_ok and (hits or not keywords_only)
        if matched:
            strict.append(Recommendation(meeting, score, reasons))
        elif cond.categories and cat_ok:
            loose.append(Recommendation(meeting, score, ["시간은 다르지만 같은 종류예요"]))
        elif fit in ("fit", "near") and cond.has_time:
            loose.append(Recommendation(meeting, score, ["그 시간에 열리는 다른 모임이에요"]))
        elif hits:
            loose.append(Recommendation(meeting, score, [f"'{hits[0]}' 관련 모임이에요"]))

    def order(rec):
        return (-rec.score, rec.meeting.start_at)

    strict.sort(key=order)
    if strict:
        return strict[:limit], []
    loose.sort(key=order)
    return [], loose[:3]


def for_you(user, limit=3, pool=None):
    """홈 화면용: 프로필 관심사와 맞는 모임."""
    if not user.interest_list:
        return []
    results, _ = recommend(Conditions(categories=user.interest_list), user, limit=limit, pool=pool)
    return results


def similar_to(meeting, user, limit=3, pool=None):
    """모임 상세용: 같은 종류의 다른 열린 모임."""
    results, _ = recommend(Conditions(categories=[meeting.category]), user, limit=limit + 1, pool=pool)
    return [rec for rec in results if rec.meeting.id != meeting.id][:limit]


def describe(cond):
    """조건을 사람이 읽는 짧은 문구 목록으로 바꾼다. 챗봇의 '이렇게 이해했어요'에 쓴다."""
    chips = []
    start = to_kst(cond.window_start) if cond.window_start else None
    end = to_kst(cond.window_end) if cond.window_end else None
    if start and end:
        to_midnight = end.time() == datetime.min.time() and end.date() != start.date()
        if to_midnight and not cond.must_end_in_window:
            # 끝이 자정이면 '그날 중'이라는 뜻이다.
            whole_day = start.time() == datetime.min.time()
            chips.append(day_label(start) if whole_day else f"{day_label(start)} {start:%H:%M} 이후")
        elif cond.must_end_in_window and start.time() == datetime.min.time():
            chips.append(f"{day_label(end)} {end:%H:%M}까지")
        elif end.date() != start.date() and not to_midnight:
            chips.append(f"{day_label(start)} {start:%H:%M}~{day_label(end)} {end:%H:%M}")
        else:
            chips.append(f"{day_label(start)} {start:%H:%M}~{'24:00' if to_midnight else f'{end:%H:%M}'}")
    elif start:
        chips.append(f"{day_label(start)} {start:%H:%M} 이후")
    elif end:
        chips.append(f"{day_label(end)} {end:%H:%M}까지")
    if cond.max_duration_min:
        chips.append(f"{fmt_duration(cond.max_duration_min)} 이내")
    chips.extend(CATEGORY_LABEL[key] for key in cond.categories)
    chips.extend(f"#{kw}" for kw in cond.keywords)
    return chips


def create_prefill(cond):
    """조건에 맞는 모임이 없을 때 '직접 만들기' 폼을 미리 채울 값."""
    params = {}
    if cond.categories:
        params["category"] = cond.categories[0]
    start_utc = None
    if cond.window_start and not cond.expired:
        # '지금'으로 잡힌 시작 시각은 폼에서 바로 통과하도록 10분 단위로 올려 준다.
        start_utc = max(cond.window_start, now_utc() + timedelta(minutes=10))
        start_utc += timedelta(minutes=(-start_utc.minute) % 10)
        start_utc = start_utc.replace(second=0, microsecond=0)
        start = to_kst(start_utc)
        params["date"] = start.strftime("%Y-%m-%d")
        whole_day = to_kst(cond.window_start).time() == datetime.min.time() and not cond.must_end_in_window
        if not whole_day:
            # "내일 보드게임"처럼 날짜만 말한 경우에는 시각을 00:00으로 채우지 않고 비워 둔다.
            params["time"] = start.strftime("%H:%M")
    length = cond.max_duration_min
    if cond.must_end_in_window and cond.window_end and start_utc:
        # 시작을 뒤로 민 만큼 빈 시간이 줄어든다. 남은 시간 안에 끝나는 길이만 권한다.
        room = int((cond.window_end - start_utc).total_seconds() // 60)
        length = min(length, room) if length else room
    if length:
        fitting = [d for d in DURATIONS if d <= length]
        params["duration"] = fitting[-1] if fitting else DURATIONS[0]
    return params
