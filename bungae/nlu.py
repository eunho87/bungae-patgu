"""사용자 문장을 검색 조건으로 바꾼다.

흐름은 두 단계다.
  1) 문장 -> 슬롯(dict): 날짜, 시작/끝 시각, 소요 시간, 종류, 키워드
     - 언어 모델(llm.py)이 하거나, 모델을 못 쓰면 이 파일의 parse_rules가 한다.
  2) 슬롯 -> Conditions: slots_to_conditions가 실제 시각 구간으로 계산한다.
어느 쪽이 1)을 하든 2)는 같은 코드라서, 모델이 틀려도 검색 자체는 항상 같은 규칙으로 돈다.
"""

import re
from datetime import date, datetime, time, timedelta

from .constants import CATEGORY_KEYS, MAX_DAYS_AHEAD
from .recommend import Conditions
from .timeutil import from_kst

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def empty_slots():
    return {
        "date": None,  # "YYYY-MM-DD" (KST)
        "start": None,  # "HH:MM"
        "end": None,  # "HH:MM"
        "duration_min": None,
        "strict_end": False,  # True면 end 전에 모임이 끝나야 한다(빈 시간을 말한 경우)
        "categories": [],
        "keywords": [],
    }


def sanitize_slots(raw, today):
    """바깥에서 온 값(모델 출력, 세션 쿠키)을 믿지 않고 형식에 맞는 것만 남긴다."""
    slots = empty_slots()
    if not isinstance(raw, dict):
        return slots

    value = raw.get("date")
    if isinstance(value, str):
        try:
            day = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            day = None
        if day and today <= day <= today + timedelta(days=MAX_DAYS_AHEAD):
            slots["date"] = day.isoformat()

    for key, alias in (("start", "start_time"), ("end", "end_time")):
        value = raw.get(key) or raw.get(alias)
        if isinstance(value, str):
            value = value.strip()
            if value == "24:00":
                value = "23:59"
            match = TIME_RE.match(value)
            if match:
                slots[key] = f"{int(match.group(1)):02d}:{match.group(2)}"

    value = raw.get("duration_min")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 10 <= value <= 720:
        slots["duration_min"] = int(value)

    slots["strict_end"] = raw.get("strict_end") is True

    value = raw.get("categories")
    if isinstance(value, list):
        slots["categories"] = [key for key in CATEGORY_KEYS if key in value]

    value = raw.get("keywords")
    if isinstance(value, list):
        for word in value:
            if not isinstance(word, str):
                continue
            word = word.strip().lstrip("#")
            if word in GENERIC_KEYWORDS:
                continue
            if 1 <= len(word) <= 20 and word not in slots["keywords"]:
                slots["keywords"].append(word)
        slots["keywords"] = slots["keywords"][:5]
    return slots


def has_any(slots):
    return any(slots[key] for key in ("date", "start", "end", "duration_min", "categories", "keywords"))


def _at(day, hhmm):
    if not hhmm:
        return None
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hour, minute))


def slots_to_conditions(slots, now):
    """슬롯을 실제 검색 구간으로 바꾼다. now는 KST."""
    today = now.date()
    explicit_day = slots["date"] is not None
    day = date.fromisoformat(slots["date"]) if explicit_day else today
    start = _at(day, slots["start"])
    end = _at(day, slots["end"])
    duration = slots["duration_min"]

    window_start = window_end = None
    must_end = False
    max_duration = None

    # 날짜를 말하지 않았는데 그 시각이 오늘 이미 지났으면 내일 이야기로 본다.
    # 날짜를 분명히 말했으면("오늘 1시까지") 그대로 두고, 지난 시간이라는 사실을 그대로 드러낸다.
    one_day = timedelta(days=1)
    if start and end:
        if end <= start:
            end += one_day
        if not explicit_day and end <= now:
            start += one_day
            end += one_day
        window_start, window_end = start, end
        must_end = slots["strict_end"]
        max_duration = duration
    elif start:
        usable_until = start + timedelta(minutes=duration or 0)
        if not explicit_day and usable_until < now - timedelta(minutes=1):
            start += one_day
        if duration:
            # "3시부터 2시간 비어" -> 그 안에 끝나는 모임
            window_start, window_end, must_end = start, start + timedelta(minutes=duration), True
        else:
            # "3시 이후에 할 만한 거" -> 그날 그 시각 이후에 시작하는 모임(가까운 순으로 보여 준다)
            window_start, window_end = start, datetime.combine(start.date() + one_day, time.min)
    elif end:
        if not explicit_day and end <= now:
            end += one_day
        window_start = datetime.combine(day, time.min)
        window_end, must_end = end, True
        max_duration = duration
    elif duration:
        if explicit_day and day != today:
            window_start = datetime.combine(day, time.min)
            window_end = window_start + timedelta(days=1)
            max_duration = duration
        else:
            # "2시간 비는데" -> 지금부터 2시간 안에 끝나는 모임
            window_start, window_end, must_end = now, now + timedelta(minutes=duration), True
    elif explicit_day:
        window_start = datetime.combine(day, time.min)
        window_end = window_start + timedelta(days=1)

    if window_start is not None and window_end > now:
        # 아직 남은 구간이면 시작을 지금으로 당긴다. 이미 다 지난 구간은 건드리지 않는다(Conditions.expired).
        window_start = max(window_start, now)

    return Conditions(
        window_start=from_kst(window_start) if window_start else None,
        window_end=from_kst(window_end) if window_end else None,
        must_end_in_window=must_end,
        max_duration_min=max_duration,
        categories=list(slots["categories"]),
        keywords=list(slots["keywords"]),
    )


def merge_slots(prev, new, reset_topic=False):
    """이어지는 대화("그럼 저녁에는?")에서 말하지 않은 부분은 직전 조건을 물려받는다."""
    if not prev:
        return new
    merged = dict(new)
    new_has_time = any(new[key] for key in ("date", "start", "end", "duration_min"))
    if not new_has_time:
        for key in ("date", "start", "end", "duration_min", "strict_end"):
            merged[key] = prev[key]
    elif not new["date"] and (new["start"] or new["end"]):
        merged["date"] = prev["date"]
    if not reset_topic and not new["categories"] and not new["keywords"]:
        merged["categories"] = list(prev["categories"])
        merged["keywords"] = list(prev["keywords"])
    return merged


# ---------------------------------------------------------------------------
# 규칙 기반 해석 (언어 모델을 쓸 수 없을 때의 대체 경로)
# ---------------------------------------------------------------------------

_KO_NUM = {
    "한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6,
    "일곱": 7, "여덟": 8, "아홉": 9, "열": 10, "열한": 11, "열두": 12,
}
_NUM = r"(\d{1,2}|열한|열두|다섯|여섯|일곱|여덟|아홉|한|두|세|네|열)"
_MERIDIEM = r"(오전|오후|아침|저녁|밤|낮|새벽)"
# "3시", "오후 3시 반", "세 시 20분". "3시간"은 소요 시간이라 제외한다.
CLOCK_RE = re.compile(rf"(?:{_MERIDIEM}\s*)?(?<!\d){_NUM}\s*시(?!간)\s*(?:(반)|(\d{{1,2}})\s*분)?")
COLON_RE = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
# "3~5시" -> "3시~5시"로 풀어 쓴 뒤 해석한다.
RANGE_SHORT_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[~\-]\s*(\d{1,2})\s*시(?!간)")
# 숫자는 자릿수를 제한한다. 제한이 없으면 "99999999시간 뒤" 같은 입력에서 날짜 계산이 넘친다.
_LATER = r"(?:뒤|후|있다가|이따가?)"
OFFSET_H_RE = re.compile(rf"((?<![\d.])\d{{1,2}}(?:\.5)?|한|두|세|네|반)\s*시간\s*(?:(반)|(\d{{1,2}})\s*분)?\s*{_LATER}")
OFFSET_M_RE = re.compile(rf"(?<![\d.])(\d{{1,3}})\s*분\s*{_LATER}")
DURATION_H_RE = re.compile(r"((?<![\d.])\d{1,2}(?:\.5)?|한|두|세|네|다섯|여섯|반)\s*시간\s*(반)?")
DURATION_M_RE = re.compile(r"(?<![\d.])(\d{1,3})\s*분")
WEEKDAY_RE = re.compile(r"(다음\s*주\s*)?([월화수목금토일])요일")
MONTH_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일")
UNTIL_AFTER_RE = re.compile(r"\s*(?:쯤|정도|경)?\s*(?:까지|전까지|전에|이전|전(?![가-힣]))")
# "밥 말고 카페"의 밥처럼 빼 달라고 한 낱말은 종류 판단에서 제외한다.
NEGATED_RE = re.compile(r"\S+\s*(?:말고|빼고|제외하고|제외)")
UNTIL_RE = re.compile(r"까지|전에|이전")
FREE_RE = re.compile(r"비는|비어|비었|빈\s*시간|공강|남는|시간\s*(?:이|은)?\s*(?:남|있|돼|되|괜찮)")
NOW_RE = re.compile(r"지금|당장|바로|이따")
RESET_RE = re.compile(r"아무거나|상관\s*없|뭐든|다른\s*(?:거|종류|모임)")
BROWSE_RE = re.compile(r"추천|모임|번개|뭐\s*하|뭐\s*할|할\s*만|할만|심심|놀\s*사람|놀고")

DAY_PARTS = [
    ("아침", "07:00", "10:00"),
    ("오전", "09:00", "12:00"),
    ("점심", "11:30", "13:30"),
    ("오후", "13:00", "18:00"),
    ("저녁", "17:30", "21:00"),
    ("밤", "20:00", "23:59"),
    ("낮", "12:00", "17:00"),
]

# 종류별 (일반 단어, 구체 단어). 구체 단어는 종류를 정하면서 키워드로도 쓴다.
CATEGORY_WORDS = {
    "study": (
        ["공부", "스터디", "과제", "모각코", "모각공", "복습", "열공"],
        ["스터디카페", "카공", "코딩", "알고리즘", "토익", "자격증", "독서실"],
    ),
    "meal": (
        ["밥", "식사", "먹", "맛집", "학식", "배고"],
        ["마라탕", "치킨", "피자", "국밥", "라면", "돈까스", "떡볶이", "햄버거", "고기", "초밥", "파스타", "짜장면"],
    ),
    "cafe": (["카페", "커피", "수다", "디저트"], ["빙수", "케이크", "버블티"]),
    "game": (
        ["보드게임", "게임", "보겜"],
        ["피시방", "pc방", "방탈출", "마작", "체스", "포커", "오버워치", "발로란트"],
    ),
    "sports": (
        ["운동", "헬스", "스포츠"],
        ["축구", "풋살", "농구", "배드민턴", "러닝", "조깅", "산책", "탁구", "볼링", "클라이밍", "테니스", "요가", "캐치볼", "야구"],
    ),
    "culture": (["취미"], ["영화", "전시", "노래방", "코노", "공연", "사진", "독서", "뮤지컬", "그림", "밴드"]),
    "drink": ([], ["맥주", "소주", "치맥", "와인", "막걸리", "하이볼", "포차"]),
}
# '기술', '예술'의 술은 술자리가 아니다.
DRINK_RE = re.compile(r"(?<![가-힣])술")
MOOD_WORDS = {"조용": "조용", "초면": "초면", "고학번": "고학번", "새내기": "새내기", "혼자": "혼자", "가볍": "가볍게"}
PLACE_WORDS = ["도서관", "학생회관", "정보과학관", "형남", "조만식", "베어드", "숭실대입구", "상도역", "운동장"]


# 종류나 시간대를 가리킬 뿐인 낱말은 키워드로 쓰지 않는다. 모델이 "공부", "점심" 같은 말을 키워드에 넣으면
# 화면의 '이렇게 이해했어요'가 지저분해지고, "점심"이 제목에 든 엉뚱한 모임에 점수가 붙는다.
GENERIC_KEYWORDS = (
    {word for general, _ in CATEGORY_WORDS.values() for word in general}
    | {word for word, _, _ in DAY_PARTS}
    | {"오늘", "내일", "모레", "지금", "술", "모임", "번개", "사람", "같이"}
)


def _to_number(token):
    if token == "반":
        return 0.5
    if token in _KO_NUM:
        return _KO_NUM[token]
    return float(token)


def _resolve_hour(marker, hour, minute, bump_past, now, after=None):
    """오전/오후 표시가 없는 시각을 24시간제로 바꾼다. 해석할 수 없으면 None.

    bump_past: 오늘 이미 지난 "9시"를 밤 9시로 볼지 여부.
    after: 구간의 끝 시각을 해석할 때 넘기는 시작 시각 (시, 분).
    """
    if hour > 24 or (hour == 24 and minute):
        return None
    if hour == 24:
        return 23, 59
    if marker in ("오후", "저녁", "밤", "낮"):
        if marker == "밤" and hour == 12:
            return 23, 59
        if marker == "밤" and hour <= 4:
            pass  # "밤 1시"는 자정을 넘긴 01시
        elif hour < 12:
            hour += 12
    elif marker in ("오전", "아침", "새벽"):
        if hour == 12:
            hour = 0
    else:
        if 1 <= hour <= 7:
            hour += 12  # "3시"는 보통 오후 3시
        elif 8 <= hour <= 11 and bump_past and (hour, minute) <= (now.hour, now.minute):
            hour += 12  # 오늘 이미 지난 "9시"는 밤 9시로 본다
    if after is not None and marker is None and (hour, minute) <= after:
        if hour == 12:
            return 23, 59  # "밤 10시부터 12시까지"의 12시는 자정
        if hour < 12:
            hour += 12  # "10시부터 1시까지"처럼 끝이 시작보다 이르면 오후로
    return hour, minute


def _find_clocks(text):
    """문장에서 시각 표현을 찾아 (시작 위치, 끝 위치, 표시, 시, 분) 목록으로 돌려준다."""
    found = []
    for match in CLOCK_RE.finditer(text):
        marker, token, half, minute = match.groups()
        if marker is None and not token.isdigit():
            # '하네 시작'처럼 다른 낱말 끝에 붙은 한글 수사는 시각이 아니다.
            if match.start() > 0 and "가" <= text[match.start() - 1] <= "힣":
                continue
        hour = int(_to_number(token))
        minutes = 30 if half else int(minute or 0)
        if minutes > 59:
            continue
        found.append((match.start(), match.end(), marker, hour, minutes))
    for match in COLON_RE.finditer(text):
        hour = int(match.group(1))
        # "15:30", "03:30"은 24시간제로 쓴 것이고, "3:30"은 "3시 30분"과 같은 말로 본다.
        marker = "24h" if hour >= 13 or hour == 0 or match.group(1).startswith("0") else None
        found.append((match.start(), match.end(), marker, hour, int(match.group(2))))
    found.sort()
    return found


def parse_rules(text, now):
    """(슬롯, 조건을 하나라도 찾았는지, 주제를 새로 잡으라는 말이 있었는지). now는 KST."""
    text = " ".join(text.split())
    lowered = text.lower()
    slots = empty_slots()
    today = now.date()

    # 1) 날짜
    day = None
    if "모레" in text:
        day = today + timedelta(days=2)
    elif "내일" in text:
        day = today + timedelta(days=1)
    elif "오늘" in text:
        day = today
    else:
        match = MONTH_DAY_RE.search(text)
        weekday = WEEKDAY_RE.search(text)
        if match:
            try:
                day = date(today.year, int(match.group(1)), int(match.group(2)))
                if day < today:
                    # 12월 말에 "1월 2일"이라고 하면 내년이다.
                    day = day.replace(year=today.year + 1)
            except ValueError:
                day = None
        elif weekday:
            index = "월화수목금토일".index(weekday.group(2))
            if weekday.group(1):
                # "다음 주 월요일"은 다음 주 달력의 월요일이다.
                day = today + timedelta(days=7 - today.weekday() + index)
            else:
                day = today + timedelta(days=(index - today.weekday()) % 7)

    work = MONTH_DAY_RE.sub(" ", text)

    # 2) "1시간 30분 뒤" -> 시작 시각
    offset_start = None
    minutes = None
    match = OFFSET_H_RE.search(work)
    if match:
        minutes = _to_number(match.group(1)) * 60 + (30 if match.group(2) else 0) + int(match.group(3) or 0)
    else:
        match = OFFSET_M_RE.search(work)
        if match:
            minutes = int(match.group(1))
    if match:
        offset_start = now + timedelta(minutes=int(minutes))
        day = offset_start.date()
        slots["start"] = f"{offset_start:%H:%M}"
        work = work[: match.start()] + " " + work[match.end():]

    # 3) 시각 표현
    work = RANGE_SHORT_RE.sub(r"\1시~\2시", work)
    found = _find_clocks(work)
    is_range = len(found) >= 2 and offset_start is None
    # 구간("10시부터 1시까지")은 두 시각을 함께 보고 판단하므로 낱개 시각처럼 밤으로 넘기지 않는다.
    bump_past = (day is None or day == today) and not is_range
    clocks = []
    for begin, end, marker, hour, minute in found:
        if marker == "24h":
            resolved = (hour, minute)
        else:
            after = clocks[-1][2] if clocks else None
            resolved = _resolve_hour(marker, hour, minute, bump_past, now, after)
        if resolved:
            clocks.append((begin, end, resolved, marker))
    if len(clocks) >= 2 and offset_start is None and day is None:
        (first, second) = clocks[0], clocks[1]
        (sh, sm), (eh, em) = first[2], second[2]
        unmarked = first[3] is None and second[3] is None
        passed = (eh, em) <= (now.hour, now.minute)
        if unmarked and passed and sh < 12 and (sh, sm) < (eh, em) and eh + 12 <= 23:
            # 저녁 8시에 "9시부터 11시까지"라고 하면 오늘 밤 21~23시다.
            clocks[0] = (first[0], first[1], (sh + 12, sm), None)
            clocks[1] = (second[0], second[1], (eh + 12, em), None)
    clocks = [(begin, end, resolved) for begin, end, resolved, *_ in clocks]
    clock_text = work
    for begin, end, _ in clocks:
        # 시각 표현을 같은 길이의 공백으로 지워 "3시 30분"의 30분이 소요 시간으로 읽히지 않게 한다.
        work = work[:begin] + " " * (end - begin) + work[end:]

    free_time = bool(FREE_RE.search(text))
    if offset_start is None:
        if len(clocks) >= 2:
            slots["start"] = "%02d:%02d" % clocks[0][2]
            slots["end"] = "%02d:%02d" % clocks[1][2]
            slots["strict_end"] = free_time or bool(UNTIL_RE.search(text))
        elif len(clocks) == 1:
            begin, end, resolved = clocks[0]
            if UNTIL_AFTER_RE.match(clock_text[end:end + 8]):
                slots["end"] = "%02d:%02d" % resolved
                slots["strict_end"] = True
            else:
                slots["start"] = "%02d:%02d" % resolved
        else:
            # "점심 말고 저녁"이면 저녁을 잡아야 하므로, 빼 달라고 한 낱말을 지운 문장에서 찾는다.
            part_text = NEGATED_RE.sub(" ", text)
            for word, part_start, part_end in DAY_PARTS:
                if word in part_text:
                    slots["start"], slots["end"] = part_start, part_end
                    break

    # 4) 소요 시간
    duration = None
    match = DURATION_H_RE.search(work)
    if match:
        duration = _to_number(match.group(1)) * 60 + (30 if match.group(2) else 0)
        rest = DURATION_M_RE.match(work[match.end():].lstrip())
        if rest:
            duration += int(rest.group(1))
    else:
        match = DURATION_M_RE.search(work)
        if match:
            duration = int(match.group(1))
    if duration and 10 <= duration <= 720:
        slots["duration_min"] = int(duration)

    if day is not None:
        slots["date"] = day.isoformat()
    if not (slots["start"] or slots["end"] or slots["duration_min"]) and day is None and NOW_RE.search(text):
        slots["start"] = f"{now:%H:%M}"

    # 5) 종류와 키워드
    scan = NEGATED_RE.sub(" ", lowered).replace("스터디 카페", "스터디카페")
    keywords = []
    categories = []
    for category, (general, specific) in CATEGORY_WORDS.items():
        hit = False
        for word in sorted(specific, key=len, reverse=True):
            # 앞 글자가 한글이면 다른 낱말의 일부다("보드게임이라면"의 라면, "오전시간"의 전시).
            pattern = re.compile(rf"(?<![가-힣]){re.escape(word)}")
            if pattern.search(scan):
                hit = True
                keywords.append(word)
                scan = pattern.sub(" ", scan)
        if any(word in scan for word in general):
            hit = True
        if category == "drink" and DRINK_RE.search(scan):
            hit = True
        if hit:
            categories.append(category)
    for stem, keyword in MOOD_WORDS.items():
        if stem in scan:
            keywords.append(keyword)
    for word in PLACE_WORDS:
        if word in scan:
            keywords.append(word)

    reset_topic = bool(RESET_RE.search(text))
    if not reset_topic:
        slots["categories"] = [key for key in CATEGORY_KEYS if key in categories]
        slots["keywords"] = keywords[:5]

    found = has_any(slots) or reset_topic
    return slots, found, reset_topic


def wants_browse(text):
    """조건은 없지만 '뭐 할 만한 거 없어?'처럼 둘러보려는 말인지."""
    return bool(BROWSE_RE.search(text))
