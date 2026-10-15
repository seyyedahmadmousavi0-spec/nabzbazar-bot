import os
import sys

# Simulate Render environment without loading local .env
os.environ["PORT"] = "10000"
os.environ["RENDER"] = "true"
# remove local .env values from env
for k in list(os.environ.keys()):
    if k.startswith("MI_"):
        del os.environ[k]

sys.path.insert(0, os.path.abspath("."))
from marketintel.config import Config
cfg = Config.from_env()
print("HOST:", cfg.admin_host)
print("PORT:", cfg.admin_port)
print("KEY:", cfg.admin_secret_key)
print("BOT_TOKEN:", cfg.bot_token)
print("LIVE_PUBLISH:", cfg.live_publish)

try:
    from marketintel.cli import _conn
    c = _conn(cfg)
    print("DB connection and migrations: OK")
except Exception as e:
    print("DB ERROR:", type(e), e)

try:
    from marketintel.admin.app import create_app
    app = create_app(cfg)
    print("create_app: OK")
except Exception as e:
    print("create_app ERROR:", type(e), e)
