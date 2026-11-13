from flask import Blueprint, current_app, g, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError

from . import db, llm
from .auth import login_required
from .constants import CATEGORY_EMOJI, CATEGORY_LABEL
from .models import ChatUsage
from .nlu import empty_slots, has_any, merge_slots, parse_rules, sanitize_slots, slots_to_conditions, wants_browse
from .recommend import Recommendation, create_prefill, describe, open_meetings, recommend
from .timeutil import fmt_relative, fmt_when, now_kst

bp = Blueprint("chatbot", __name__)

MAX_MESSAGE = 300
HELP_TEXT = (
    "비는 시간과 하고 싶은 걸 말해 주면 열려 있는 번개 중에서 골라 드려요. "
    "예: \"지금 2시간 비는데 보드게임 할 사람 있어?\", \"내일 저녁에 밥 먹을 모임\""
)
QUICK_PROMPTS = [
    "지금 2시간 비는데 뭐 할까?",
    "오늘 저녁에 밥 먹을 사람",
    "내일 공부 모임 있어?",
    "1시간 뒤에 가볍게 운동",
]


def _model_chain():
    """기본 모델과 대체 모델을 쉼표로 이은 문자열. OpenRouter는 한 번에 3개까지 받는다."""
    names = [current_app.config["OPENROUTER_MODEL"]]
    fallbacks = current_app.config.get("OPENROUTER_FALLBACK_MODELS", "")
    if fallbacks.lower() != "none":
        names += [name.strip() for name in fallbacks.split(",") if name.strip()]
    unique = list(dict.fromkeys(names))
    return ",".join(unique[:3])


def _usage_row(user, day):
    return ChatUsage.query.filter_by(user_id=user.id, day=day).first()


def ai_left(user, day):
    """오늘 남은 AI 해석 횟수. 키가 없으면 0."""
    if not current_app.config["OPENROUTER_API_KEY"]:
        return 0
    row = _usage_row(user, day)
    return max(current_app.config["CHAT_AI_DAILY_LIMIT"] - (row.count if row else 0), 0)


def _spend_ai(user, day):
    """AI 해석 1회를 차감하고, 한도 안이라 써도 되면 True를 돌려준다.

    무료 키의 하루 한도를 한 사람이 다 쓰지 못하게 호출 전에 센다. "읽고 +1 해서 저장"하면
    동시에 온 요청들이 같은 값을 읽어 한 번만 차감되므로, DB가 한 문장으로 비교와 증가를 하게 한다.
    """
    limit = current_app.config["CHAT_AI_DAILY_LIMIT"]
    if not current_app.config["OPENROUTER_API_KEY"] or limit <= 0:
        return False
    user_id = user.id
    for _ in range(2):
        updated = ChatUsage.query.filter(
            ChatUsage.user_id == user_id, ChatUsage.day == day, ChatUsage.count < limit
        ).update({ChatUsage.count: ChatUsage.count + 1}, synchronize_session=False)
        db.session.commit()
        if updated:
            return True
        if ChatUsage.query.filter_by(user_id=user_id, day=day).first() is not None:
            return False  # 오늘 한도를 다 썼다
        db.session.add(ChatUsage(user_id=user_id, day=day, count=1))
        try:
            db.session.commit()
            return True
        except IntegrityError:
            db.session.rollback()  # 동시에 온 다른 요청이 먼저 행을 만들었다. 다시 증가를 시도한다.
    return False


def _meeting_json(rec):
    meeting = rec.meeting
    return {
        "id": meeting.id,
        "title": meeting.title,
        "category": CATEGORY_LABEL[meeting.category],
        "emoji": CATEGORY_EMOJI[meeting.category],
        "when": fmt_when(meeting.start_at, meeting.duration_min),
        "relative": fmt_relative(meeting.start_at),
        "place": meeting.place,
        "seats": f"{meeting.joined_count}/{meeting.capacity}명",
        "reasons": rec.reasons,
        "url": url_for("meetings.detail", meeting_id=meeting.id),
    }


def _understand(message, now, prev):
    """(슬롯, intent, 모델이 쓴 답, mode)를 돌려준다. 모델을 못 쓰면 규칙 기반으로 넘어간다."""
    day = now.date().isoformat()
    if _spend_ai(g.user, day):
        try:
            raw = llm.ask(
                message,
                now,
                prev,
                current_app.config["OPENROUTER_API_KEY"],
                _model_chain(),
            )
        except llm.LLMError as exc:
            current_app.logger.warning("AI 해석 실패, 규칙 기반으로 전환: %s", exc)
        else:
            slots = sanitize_slots(raw, now.date())
            intent = "chat" if raw.get("intent") == "chat" and not has_any(slots) else "search"
            reply = raw.get("reply")
            reply = " ".join(reply.split())[:300] if isinstance(reply, str) else ""
            return slots, intent, reply, "ai"

    try:
        new, found, reset_topic = parse_rules(message, now)
    except (ValueError, OverflowError):
        # 날짜 계산이 범위를 벗어나는 이상한 입력은 조건을 못 찾은 것으로 처리한다.
        new, found, reset_topic = empty_slots(), False, False
    if found:
        return merge_slots(prev, new, reset_topic), "search", "", "rule"
    if wants_browse(message):
        return empty_slots(), "search", "", "rule"
    return prev or empty_slots(), "chat", "", "rule"


@bp.get("/chat")
@login_required
def chat():
    day = now_kst().date().isoformat()
    return render_template(
        "chat.html",
        quick_prompts=QUICK_PROMPTS,
        ai_enabled=bool(current_app.config["OPENROUTER_API_KEY"]),
        ai_left=ai_left(g.user, day),
        help_text=HELP_TEXT,
        max_message=MAX_MESSAGE,
    )


@bp.post("/api/chat")
@login_required
def api_chat():
    data = request.get_json(silent=True)
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, str) or not message.strip():
        return {"error": "메시지를 입력해 주세요."}, 400
    message = " ".join(message.split())
    if len(message) > MAX_MESSAGE:
        return {"error": f"메시지는 {MAX_MESSAGE}자 안으로 적어 주세요."}, 400

    now = now_kst()
    stored = session.get("chat_slots")
    prev = sanitize_slots(stored, now.date()) if stored else None
    slots, intent, model_reply, mode = _understand(message, now, prev)
    left = ai_left(g.user, now.date().isoformat())

    if intent == "chat":
        return {"mode": mode, "reply": model_reply or HELP_TEXT, "understood": [], "meetings": [], "ai_left": left}

    session["chat_slots"] = slots
    cond = slots_to_conditions(slots, now)
    pool = open_meetings(g.user)
    results, alternatives = recommend(cond, g.user, pool=pool)

    kind = "match"
    if cond.expired:
        kind = "expired"
        reply = "말한 시간대는 이미 지났어요. 다른 시간으로 다시 물어봐 주세요. 지금 열려 있는 모임은 이래요."
        shown = [Recommendation(m, 0, ["지금 열려 있는 모임"]) for m in pool[:3]]
    elif results:
        reply = (
            "지금 참여할 수 있는 번개예요." if cond.is_empty else f"조건에 맞는 번개를 {len(results)}개 찾았어요."
        )
        shown = results
    elif alternatives:
        kind = "alternative"
        reply = "딱 맞는 번개는 아직 없어요. 대신 이런 모임은 어때요?"
        shown = alternatives
    else:
        shown = [Recommendation(m, 0, ["지금 열려 있는 모임"]) for m in pool[:3]]
        if shown:
            kind = "fallback"
            reply = "조건에 맞는 번개가 없어서 지금 열려 있는 모임을 보여 드려요."
        else:
            kind = "empty"
            reply = "지금은 참여할 수 있는 번개가 없어요. 직접 열어 보는 건 어때요?"

    return {
        "mode": mode,
        "kind": kind,
        "reply": reply,
        "understood": describe(cond),
        "meetings": [_meeting_json(rec) for rec in shown],
        "create_url": url_for("meetings.create", **create_prefill(cond)) if kind != "match" else None,
        "ai_left": left,
    }


@bp.post("/api/chat/reset")
@login_required
def api_chat_reset():
    session.pop("chat_slots", None)
    return {"ok": True}
