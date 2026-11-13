"""시연용 예시 데이터를 넣는다.

    python seed.py                 처음 한 번: 데모 계정 5개와 예시 모임을 만든다
    python seed.py --refresh       데모 계정이 연 모임을 지우고 지금 시각 기준으로 다시 만든다
    python seed.py --password PW   데모 계정 비밀번호를 직접 정한다(생략하면 무작위로 만들어 한 번 보여 준다)

.env 에 DATABASE_URL 이 있으면 그 DB(배포용)에, 없으면 로컬 SQLite 에 넣는다.
"""

import argparse
import secrets
from datetime import datetime, time, timedelta

from werkzeug.security import generate_password_hash

from bungae import create_app, db
from bungae.models import Comment, Meeting, Notification, Participation, User
from bungae.timeutil import from_kst, now_kst

DEMO_USERS = [
    ("demo1@soongsil.ac.kr", "번개장인", "game,meal"),
    ("demo2@soongsil.ac.kr", "도서관지박령", "study,cafe"),
    ("demo3@soongsil.ac.kr", "풋살왕", "sports,drink"),
    ("demo4@soongsil.ac.kr", "고학번선배", "meal,culture"),
    ("demo5@soongsil.ac.kr", "새내기", "cafe,game"),
]


def plan(now):
    """(호스트 번호, 시작 시각(KST), 모임 정보, 참여자 번호들) 목록. 지금 시각에 맞춰 매번 새로 계산한다."""
    base = now.replace(second=0, microsecond=0) + timedelta(minutes=(-now.minute) % 30 or 30)
    today, tomorrow = now.date(), now.date() + timedelta(days=1)

    def at(day, hour, minute=0):
        when = datetime.combine(day, time(hour, minute))
        return when if when > now + timedelta(minutes=20) else when + timedelta(days=1)

    return [
        (4, base + timedelta(minutes=30), dict(title="공강에 커피 한 잔 하실 분", category="cafe", duration_min=60,
            place="학생회관", capacity=2, tags="가볍게,초면환영", description="수업 사이에 시간이 떠서요. 가볍게 커피 마셔요."), []),
        (0, base + timedelta(hours=1), dict(title="학생회관에서 보드게임 한 판", category="game", duration_min=120,
            place="학생회관", capacity=4, tags="초면환영,가볍게", description="스플렌더랑 루미큐브 가져갑니다. 처음 하는 분도 알려 드려요."), [2]),
        (3, base + timedelta(hours=1, minutes=30), dict(title="학식 같이 먹어요", category="meal", duration_min=60,
            place="학생회관", capacity=3, tags="혼자와도OK,고학번환영", description="혼밥이 지겨운 분들 모여요. 학번 안 따집니다."), []),
        (1, base + timedelta(hours=2), dict(title="중앙도서관 모각공 2시간", category="study", duration_min=120,
            place="중앙도서관", capacity=6, tags="조용히,진지하게", description="각자 할 일 하고 끝나면 인사만 해요. 말 걸지 않기!"), [4]),
        (2, base + timedelta(hours=3), dict(title="풋살 한 게임 하실 분", category="sports", duration_min=90,
            place="운동장", capacity=10, tags="초면환영", description="인원 모이면 5대5로 가볍게 뜁니다. 운동화만 챙겨 오세요."), [0, 3]),
        (3, at(today, 18, 30), dict(title="저녁 마라탕 번개", category="meal", duration_min=90,
            place="숭실대입구역 근처", capacity=4, tags="수다,고학번환영", description="맵기는 각자 고르는 걸로. 다 먹고 헤어져요."), [1]),
        (2, at(today, 19, 30), dict(title="과제 끝! 치맥 한잔", category="drink", duration_min=120,
            place="상도역 근처", capacity=6, tags="수다", description="과제 낸 사람만 올 수 있음(농담). 1차만 하고 갑니다."), [3]),
        (0, at(tomorrow, 12, 0), dict(title="점심 국밥 같이 드실 분", category="meal", duration_min=60,
            place="숭실대입구역 근처", capacity=4, tags="혼자와도OK", description="12시에 역 3번 출구 앞에서 만나요."), []),
        (4, at(tomorrow, 15, 0), dict(title="카페에서 수다 떨어요", category="cafe", duration_min=90,
            place="숭실대입구역 근처", capacity=4, tags="새내기환영,수다", description="학교 생활 얘기 나눠요. 선배님들 환영합니다."), [3]),
        (1, at(tomorrow, 17, 0), dict(title="코노 1시간만", category="culture", duration_min=60,
            place="상도역 근처", capacity=3, tags="가볍게", description="스트레스 풀러 가요. 딱 한 시간만!"), []),
        (1, at(tomorrow + timedelta(days=1), 14, 0), dict(title="알고리즘 스터디 맛보기", category="study", duration_min=120,
            place="정보과학관", capacity=5, tags="진지하게", description="쉬운 문제 두 개를 같이 풀고 풀이를 비교해 봐요."), [4]),
    ]


def main():
    parser = argparse.ArgumentParser(description="번개팟구 시연용 데이터")
    parser.add_argument("--refresh", action="store_true", help="데모 계정이 연 모임을 지우고 다시 만든다")
    parser.add_argument("--password", help="데모 계정 비밀번호")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        users = [User.query.filter_by(email=email).first() for email, _, _ in DEMO_USERS]
        if not all(users):
            password = args.password or secrets.token_urlsafe(9)
            for index, (email, nickname, interests) in enumerate(DEMO_USERS):
                if users[index] is None:
                    users[index] = User(email=email, nickname=nickname, interests=interests,
                                        password_hash=generate_password_hash(password))
                    db.session.add(users[index])
            db.session.commit()
            print(f"데모 계정을 만들었습니다: demo1~demo5@soongsil.ac.kr / 비밀번호 {password}")
            print("이 비밀번호는 다시 볼 수 없으니 적어 두세요. 공개 배포에서는 시연이 끝나면 계정을 지우는 것이 좋습니다.")
        elif args.password:
            for user in users:
                user.password_hash = generate_password_hash(args.password)
            db.session.commit()
            print("데모 계정 비밀번호를 바꿨습니다.")

        demo_ids = [user.id for user in users]
        existing = Meeting.query.filter(Meeting.host_id.in_(demo_ids)).all()
        if existing and not args.refresh:
            print(f"데모 모임이 이미 {len(existing)}개 있습니다. 시간을 지금 기준으로 다시 맞추려면 --refresh 를 붙이세요.")
            return
        if existing:
            # 지울 모임을 가리키는 알림은 남기되 연결만 끊는다(PostgreSQL은 가리키는 대상이 사라지는 것을 막는다).
            Notification.query.filter(Notification.meeting_id.in_([m.id for m in existing])).update(
                {"meeting_id": None}, synchronize_session=False
            )
        for meeting in existing:
            db.session.delete(meeting)
        db.session.commit()

        created = []
        for host, start_kst, info, joiners in plan(now_kst()):
            meeting = Meeting(host_id=users[host].id, start_at=from_kst(start_kst), **info)
            meeting.participations.append(Participation(user_id=users[host].id))
            for joiner in joiners:
                meeting.participations.append(Participation(user_id=users[joiner].id))
            db.session.add(meeting)
            created.append(meeting)
        db.session.commit()
        db.session.add(Comment(meeting_id=created[1].id, user_id=users[4].id, body="보드게임 처음인데 가도 되나요?"))
        db.session.add(Comment(meeting_id=created[1].id, user_id=users[0].id, body="그럼요! 규칙부터 알려 드릴게요."))
        db.session.commit()
        print(f"예시 모임 {len(created)}개를 만들었습니다.")


if __name__ == "__main__":
    main()
