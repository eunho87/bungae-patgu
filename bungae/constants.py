"""화면과 추천 로직이 함께 쓰는 고정 값."""

# (키, 표시 이름, 이모지)
CATEGORIES = [
    ("meal", "식사", "🍚"),
    ("cafe", "카페·수다", "☕"),
    ("study", "공부", "📚"),
    ("game", "보드게임·게임", "🎲"),
    ("sports", "운동", "🏃"),
    ("culture", "문화·취미", "🎬"),
    ("drink", "술자리", "🍻"),
    ("etc", "기타", "✨"),
]
CATEGORY_KEYS = [key for key, _, _ in CATEGORIES]
CATEGORY_LABEL = {key: label for key, label, _ in CATEGORIES}
CATEGORY_EMOJI = {key: emoji for key, _, emoji in CATEGORIES}

PRESET_TAGS = ["초면환영", "고학번환영", "새내기환영", "혼자와도OK", "가볍게", "조용히", "수다", "진지하게"]

DURATIONS = [30, 60, 90, 120, 180, 240]

PLACE_SUGGESTIONS = [
    "중앙도서관",
    "학생회관",
    "정보과학관",
    "형남공학관",
    "조만식기념관",
    "베어드홀",
    "숭실대입구역 근처",
    "상도역 근처",
]

REPORT_REASONS = [
    ("spam", "광고·홍보"),
    ("money", "금전 요구·사기 의심"),
    ("unsafe", "불쾌하거나 위험한 내용"),
    ("fake", "실제로 열리지 않는 모임"),
    ("etc", "기타"),
]
# 서로 다른 사람의 신고가 이만큼 쌓이면 운영자 확인 없이도 모집을 멈춘다.
REPORT_LIMIT = 3

CAPACITY_MIN = 2
CAPACITY_MAX = 20
MAX_TAGS = 5
MAX_DAYS_AHEAD = 14
