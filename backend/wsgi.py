"""WSGI entrypoint.

Production:  gunicorn -c deploy/gunicorn.conf.py wsgi:app
Development: python wsgi.py      (binds to server.host:server.port from config.json)
"""

from app import create_app
from app.config import Config

config = Config()
application = create_app(config)
app = application

if __name__ == "__main__":
    prefix = config.URL_PREFIX or ""
    print(f"→ http://{config.HOST}:{config.PORT}{prefix}/")
    app.run(host=config.HOST, port=config.PORT, debug=True, threaded=True)
