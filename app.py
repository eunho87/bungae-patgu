import os

from flask import Flask

from bungae import create_app

# Vercel은 이 파일에서 Flask 인스턴스인 app 변수를 찾아 실행한다.
app: Flask = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
