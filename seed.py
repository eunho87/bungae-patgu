"""시연용 예시 데이터를 내 컴퓨터에서 직접 넣는다.

    python seed.py                 처음 한 번: 데모 계정 5개와 예시 모임을 만든다
    python seed.py --refresh       데모 계정이 연 모임을 지우고 지금 시각 기준으로 다시 만든다
    python seed.py --password PW   데모 계정 비밀번호를 직접 정한다(생략하면 무작위로 만들어 한 번 보여 준다)

.env 에 DATABASE_URL 이 있으면 그 DB(배포용)에, 없으면 로컬 SQLite 에 넣는다.
배포 환경에서는 이 스크립트 대신 환경 변수 SEED_DEMO=1 을 켜 두면 서버가 스스로 채운다(bungae/demo.py).
"""

import argparse
import secrets

from werkzeug.security import generate_password_hash

from bungae import create_app, db
from bungae.demo import clear_meetings, create_meetings, demo_meetings, ensure_users


def main():
    parser = argparse.ArgumentParser(description="번개팟구 시연용 데이터")
    parser.add_argument("--refresh", action="store_true", help="데모 계정이 연 모임을 지우고 다시 만든다")
    parser.add_argument("--password", help="데모 계정 비밀번호")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        password = args.password or secrets.token_urlsafe(9)
        users, created_users = ensure_users(password)
        if created_users:
            print(f"데모 계정을 만들었습니다: demo1~demo5@soongsil.ac.kr / 비밀번호 {password}")
            print("이 비밀번호는 다시 볼 수 없으니 적어 두세요. 공개 배포에서는 시연이 끝나면 계정을 지우는 것이 좋습니다.")
        elif args.password:
            for user in users:
                user.password_hash = generate_password_hash(args.password)
            print("데모 계정 비밀번호를 바꿨습니다.")
        db.session.commit()

        existing = demo_meetings(users)
        if existing and not args.refresh:
            print(f"데모 모임이 이미 {len(existing)}개 있습니다. 시간을 지금 기준으로 다시 맞추려면 --refresh 를 붙이세요.")
            return
        clear_meetings(users)
        created = create_meetings(users)
        db.session.commit()
        print(f"예시 모임 {len(created)}개를 만들었습니다.")


if __name__ == "__main__":
    main()
