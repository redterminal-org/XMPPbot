import pytest
import tempfile
import os
import shutil
import asyncio


@pytest.fixture(scope='session')
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def tmp_db_path(tmp_path):
    db_path = tmp_path / "test_db.sqlite"
    yield str(db_path)
    try:
        os.remove(db_path)
    except OSError:
        pass


@pytest.fixture
def clean_config(monkeypatch, tmp_path):
    # Set up env vars or config as needed before tests run
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('''
{
  "jid": "testbot@example.tld",
  "password": "Passw0rd",
  "nick": "testbot",
  "timezone": "US/Alaska",
  "owner": "owner@example.tld",

  "youtube_api_key": "ToP53cRetPassw0rd",

  "prefix": "+",
  "db": "bot_test.db",
  "loglevel": "INFO",
  "users": {
    "max_room_nicks": 5
  },

  "avatar": "avatar.jpg",
  "avatar_type": "image/jpeg",

  "reminder_max_age_days": 365
}
''')
    monkeypatch.setenv("ENVSBOT_CONFIG", str(cfg_path))
    yield cfg_path
