import pytest
import io
import types
import builtins

import plugins._reg_profile as _reg_profile


# --- HASH HELPERS ---

def test_sha1_bytes_and_consistency():
    data = b"hello world"
    result = _reg_profile.sha1(data)

    assert isinstance(result, str)
    # SHA1 of "hello world" in hex:
    assert result == "2aae6c35c94fcfb415dbe95f408b9ce91ee846ed"


def test_read_hash_file_exists(tmp_path):
    path = tmp_path / "hashfile"
    path.write_text(" hashvalue \n")

    # Should read and strip whitespace
    assert _reg_profile.read_hash(str(path)) == "hashvalue"


def test_read_hash_file_not_exists(tmp_path):
    path = tmp_path / "doesnotexist"

    assert _reg_profile.read_hash(str(path)) is None


def test_read_hash_file_error(monkeypatch, tmp_path):
    # Patch open to raise exception
    path = tmp_path / "hashfile"
    path.write_text("somevalue")

    def raise_ioerror_read(*a, **k):
        raise IOError("READ")

    monkeypatch.setattr(builtins, "open", raise_ioerror_read)

    assert _reg_profile.read_hash(str(path)) is None


def test_write_hash_file_success(tmp_path):
    path = tmp_path / "writefile"

    _reg_profile.write_hash(str(path), "xyz")

    assert path.read_text() == "xyz"


def test_write_hash_file_error(monkeypatch, tmp_path):
    path = tmp_path / "cannotwrite"

    def raise_ioerror_write(*a, **k):
        raise IOError("WRITE")

    monkeypatch.setattr(builtins, "open", raise_ioerror_write)

    # Should not raise, swallows error
    _reg_profile.write_hash(str(path), "abc")


# --- VCARD BUILDER ---

def test_build_vcard_basic_and_nested():
    # Fake card like slixmpp.xmlstream.stanzabase.ElementBase API
    class FakeCard(dict):
        def __getitem__(self, k):
            if k not in self:
                self[k] = FakeCard()
            return super().__getitem__(k)

        def __setitem__(self, k, v):
            super().__setitem__(k, v)

    card = FakeCard()
    data = {
        "FN": "Test Bot",
        "NICKNAME": "envsbot",
        "ADR": {"COUNTRY": "Wonderland", "CITY": "Imaginaria"},
    }

    _reg_profile.build_vcard(card, data)

    assert card["FN"] == "Test Bot"
    assert card["NICKNAME"] == "envsbot"
    assert isinstance(card["ADR"], dict)
    assert card["ADR"]["COUNTRY"] == "Wonderland"
    assert card["ADR"]["CITY"] == "Imaginaria"


# --- update_vcard ---

@pytest.mark.asyncio
async def test_update_vcard_py_missing(monkeypatch):
    log_msgs = []

    monkeypatch.setattr(_reg_profile.os.path, "exists", lambda p: False)
    monkeypatch.setattr(_reg_profile.log, "warning",
                        lambda msg: log_msgs.append(msg))

    bot = object()

    await _reg_profile.update_vcard(bot)

    assert log_msgs and "vcard.py does not exist" in log_msgs[0]


@pytest.mark.asyncio
async def test_update_vcard_import_error(monkeypatch, tmp_path):
    vcard_py = tmp_path / "vcard.py"
    vcard_py.write_text("raise Exception('fail')")

    monkeypatch.setattr(_reg_profile.os.path, "exists", lambda p: True)
    monkeypatch.setattr(_reg_profile.os.path, "abspath",
                        lambda p: str(vcard_py.parent))
    monkeypatch.setattr(_reg_profile.os.path, "dirname",
                        lambda p: str(vcard_py.parent))

    import importlib.util

    orig_spec_from_file_location = importlib.util.spec_from_file_location

    def our_spec_from_file_location(name, location):
        # Force our vcard_py spec
        spec = orig_spec_from_file_location(name, str(vcard_py))
        return spec

    monkeypatch.setattr(
        _reg_profile.importlib.util,
        "spec_from_file_location",
        our_spec_from_file_location,
    )

    error_msgs = []
    monkeypatch.setattr(
        _reg_profile,
        "log",
        types.SimpleNamespace(error=lambda msg: error_msgs.append(msg)),
    )

    await _reg_profile.update_vcard(object())

    assert any("Error importing vcard.py" in m for m in error_msgs)


@pytest.mark.asyncio
async def test_update_vcard_not_str(monkeypatch, tmp_path):
    vcard_py = tmp_path / "vcard.py"
    vcard_py.write_text("VCARD = 12345")

    monkeypatch.setattr(_reg_profile.os.path, "exists", lambda p: True)
    monkeypatch.setattr(_reg_profile.os.path, "abspath",
                        lambda p: str(vcard_py.parent))
    monkeypatch.setattr(_reg_profile.os.path, "dirname",
                        lambda p: str(vcard_py.parent))

    import importlib.util

    orig_spec_from_file_location = importlib.util.spec_from_file_location

    def our_spec_from_file_location(name, location):
        spec = orig_spec_from_file_location(name, str(vcard_py))
        return spec

    monkeypatch.setattr(
        _reg_profile.importlib.util,
        "spec_from_file_location",
        our_spec_from_file_location,
    )

    error_msgs = []
    monkeypatch.setattr(
        _reg_profile,
        "log",
        types.SimpleNamespace(error=lambda msg: error_msgs.append(msg)),
    )

    await _reg_profile.update_vcard(object())

    assert any(
        "VCARD variable in vcard.py is not a string" in m for m in error_msgs)


@pytest.mark.asyncio
async def test_update_vcard_no_change(monkeypatch, tmp_path):
    vcard_py = tmp_path / "vcard.py"
    vcard_text = "bot"
    vcard_py.write_text(f"VCARD = '''{vcard_text}'''")

    # patch file system lookups
    monkeypatch.setattr(_reg_profile.os.path, "exists", lambda p: True)
    monkeypatch.setattr(_reg_profile.os.path, "abspath",
                        lambda p: str(vcard_py.parent))
    monkeypatch.setattr(_reg_profile.os.path, "dirname",
                        lambda p: str(vcard_py.parent))

    import importlib.util

    orig_spec_from_file_location = importlib.util.spec_from_file_location

    def our_spec_from_file_location(name, location):
        spec = orig_spec_from_file_location(name, str(vcard_py))
        return spec

    monkeypatch.setattr(
        _reg_profile.importlib.util,
        "spec_from_file_location",
        our_spec_from_file_location,
    )

    # patch SHA1 to return fixed; patch read_hash to match
    fixed_hash = "deadbeef"
    monkeypatch.setattr(_reg_profile, "sha1", lambda data: fixed_hash)
    monkeypatch.setattr(_reg_profile, "read_hash", lambda path: fixed_hash)

    info_msgs = []
    monkeypatch.setattr(
        _reg_profile,
        "log",
        types.SimpleNamespace(info=lambda msg: info_msgs.append(msg)),
    )

    await _reg_profile.update_vcard(object())

    assert any("unchanged" in m for m in info_msgs)


@pytest.mark.asyncio
async def test_update_vcard_success(monkeypatch, tmp_path):
    vcard_py = tmp_path / "vcard.py"
    vcard_text = "<vCard xmlns='vcard-temp'><FN>bot</FN></vCard>"
    vcard_py.write_text(f"VCARD = '''{vcard_text}'''")

    monkeypatch.setattr(_reg_profile.os.path, "exists", lambda p: True)
    monkeypatch.setattr(_reg_profile.os.path, "abspath",
                        lambda p: str(vcard_py.parent))
    monkeypatch.setattr(_reg_profile.os.path, "dirname",
                        lambda p: str(vcard_py.parent))

    import importlib.util

    orig_spec_from_file_location = importlib.util.spec_from_file_location

    def our_spec_from_file_location(name, location):
        spec = orig_spec_from_file_location(name, str(vcard_py))
        return spec

    monkeypatch.setattr(
        _reg_profile.importlib.util,
        "spec_from_file_location",
        our_spec_from_file_location,
    )

    # simulate hash changed
    monkeypatch.setattr(_reg_profile, "sha1", lambda b: "newhash12")
    monkeypatch.setattr(_reg_profile, "read_hash", lambda p: "different_hash")

    write_called = []
    monkeypatch.setattr(
        _reg_profile,
        "write_hash",
        lambda path, value: write_called.append((path, value)),
    )

    class Bot:
        def make_iq_set(self):
            class IQ:
                def __init__(self):
                    self.elem = None
                    self.sent = False

                def append(self, elem):
                    self.elem = elem

                async def send(self):
                    self.sent = True

            return IQ()

    info_msgs = []
    error_msgs = []
    monkeypatch.setattr(
        _reg_profile,
        "log",
        types.SimpleNamespace(
            info=lambda msg: info_msgs.append(msg),
            error=lambda msg: error_msgs.append(msg),
        ),
    )

    await _reg_profile.update_vcard(Bot())

    assert not error_msgs
    assert any("updated" in m for m in info_msgs)
    assert write_called
    assert write_called[0][1] == "newhash12"


# More: update_avatar setup_profile, on_load, on_ready

@pytest.mark.asyncio
async def test_update_avatar_all_paths(monkeypatch, tmp_path):
    # Cover avatar_path missing, not exists, bad type, unchanged,
    # error, success

    class AvatarBot(dict):
        def __init__(self, plugins):
            super().__init__(plugins)
            self.boundjid = types.SimpleNamespace(bare="bot@example.org")

    async def noop_publish_avatar(data):
        return None

    async def noop_publish_avatar_metadata(meta):
        return None

    async def noop_set_avatar(**kwargs):
        return None

    bot = AvatarBot({
        "xep_0084": types.SimpleNamespace(
            publish_avatar=noop_publish_avatar,
            publish_avatar_metadata=noop_publish_avatar_metadata,
        ),
        "xep_0153": types.SimpleNamespace(
            set_avatar=noop_set_avatar,
        ),
    })

    # No avatar_path
    monkeypatch.setattr(_reg_profile, "config", {})  # config with nothing
    await _reg_profile.update_avatar(bot)

    # Not exists
    monkeypatch.setattr(
        _reg_profile,
        "config",
        {"avatar": "missing.png", "avatar_type": "image/png"},
    )
    monkeypatch.setattr(_reg_profile.os.path, "exists", lambda p: False)
    monkeypatch.setattr(
        _reg_profile,
        "log",
        types.SimpleNamespace(warning=lambda m: None),
    )

    await _reg_profile.update_avatar(bot)

    # ----
    # Our all-modes open mock:
    class DummyFile(io.BytesIO):
        def __init__(self, *a, **k):
            super().__init__(b"bytes")
            self._written = []

        def write(self, val):
            # accept str or bytes for simplicity, track writes
            self._written.append(val)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def open_mock(path, mode="r", *a, **k):
        if "b" in mode:
            return DummyFile()

        if "w" in mode:
            # file opened for writing the hash: accept write(str)
            class DummyW:
                def __init__(self):
                    self._written = []

                def write(self, val):
                    self._written.append(val)

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

            return DummyW()

        raise RuntimeError("unexpected open mode %r" % mode)

    monkeypatch.setattr(_reg_profile.os.path, "exists", lambda p: True)
    monkeypatch.setattr(builtins, "open", open_mock)
    monkeypatch.setattr(_reg_profile, "sha1", lambda d: "hash1")
    monkeypatch.setattr(_reg_profile, "read_hash", lambda p: "v2:hash1")
    monkeypatch.setattr(
        _reg_profile,
        "log",
        types.SimpleNamespace(
            info=lambda m: None,
            error=lambda m: None,
        ),
    )

    # Exists, unchanged
    await _reg_profile.update_avatar(bot)

    # Exists, bad type
    monkeypatch.setattr(_reg_profile, "read_hash", lambda p: "different")
    monkeypatch.setattr(
        _reg_profile,
        "config",
        {"avatar": "avatar.png", "avatar_type": "bad/image"},
    )

    await _reg_profile.update_avatar(bot)

    # Exists, good type, happy path
    monkeypatch.setattr(
        _reg_profile,
        "config",
        {"avatar": "avatar.png", "avatar_type": "image/png"},
    )
    monkeypatch.setattr(_reg_profile, "sha1", lambda d: "newhash")
    monkeypatch.setattr(_reg_profile, "read_hash", lambda p: "oldhash")

    wrote = []
    monkeypatch.setattr(
        _reg_profile,
        "write_hash",
        lambda path, value: wrote.append((path, value)),
    )

    avatar_published = []
    metadata_published = []
    xep0153_calls = []

    async def publish_avatar(data):
        avatar_published.append(data)

    async def publish_avatar_metadata(meta):
        metadata_published.append(meta)

    async def set_avatar(**kwargs):
        xep0153_calls.append(kwargs)

    bot2 = AvatarBot({
        "xep_0084": types.SimpleNamespace(
            publish_avatar=publish_avatar,
            publish_avatar_metadata=publish_avatar_metadata,
        ),
        "xep_0153": types.SimpleNamespace(
            set_avatar=set_avatar,
        ),
    })

    await _reg_profile.update_avatar(bot2)

    assert avatar_published == [b"bytes"]
    assert metadata_published == [
        [
            {
                "id": "newhash",
                "type": "image/png",
                "bytes": len(b"bytes"),
            }
        ]
    ]
    assert xep0153_calls == [
        {
            "jid": "bot@example.org",
            "avatar": b"bytes",
            "mtype": "image/png",
        }
    ]
    assert wrote
    assert wrote[0][1] == "v2:newhash"


@pytest.mark.asyncio
async def test_setup_profile_user_entry(monkeypatch):
    # happy path: user exists
    got_roster_called = []

    class FakeDBUsers:
        async def get(self, jid):
            return {"jid": jid}

        async def create(self, jid, nick):
            assert False

    class FakeBot:
        async def get_roster(self):
            got_roster_called.append(True)

        boundjid = type("bjid", (), {"bare": "jidval"})()
        db = types.SimpleNamespace(users=FakeDBUsers())

    wrote = []

    monkeypatch.setattr(_reg_profile, "update_vcard",
                        lambda bot: _awaitable(None))
    monkeypatch.setattr(_reg_profile, "update_avatar",
                        lambda bot: _awaitable(None))
    monkeypatch.setattr(
        _reg_profile,
        "log",
        types.SimpleNamespace(
            info=lambda m: wrote.append(m),
            error=lambda m: None,
        ),
    )
    monkeypatch.setattr(_reg_profile, "config", {"nick": "botnick"})

    await _reg_profile.setup_profile(FakeBot())

    assert got_roster_called and wrote


@pytest.mark.asyncio
async def test_setup_profile_user_created(monkeypatch):
    # happy path: user missing, creation succeeds
    called = []

    class FakeDBUsers:
        async def get(self, jid):
            return None

        async def create(self, jid, nick):
            called.append(("create", jid, nick))

    class FakeBot:
        async def get_roster(self):
            called.append("roster")

        boundjid = type("bjid", (), {"bare": "jidval"})()
        db = types.SimpleNamespace(users=FakeDBUsers())

    monkeypatch.setattr(_reg_profile, "update_vcard",
                        lambda bot: _awaitable(None))
    monkeypatch.setattr(_reg_profile, "update_avatar",
                        lambda bot: _awaitable(None))
    monkeypatch.setattr(
        _reg_profile,
        "log",
        types.SimpleNamespace(
            info=lambda m: called.append(m),
            error=lambda m: None,
        ),
    )
    monkeypatch.setattr(_reg_profile, "config", {"nick": "nick"})

    await _reg_profile.setup_profile(FakeBot())

    assert any("create" in str(x) for x in called)


@pytest.mark.asyncio
async def test_setup_profile_user_create_error(monkeypatch):
    # error creating
    called = []

    class FakeDBUsers:
        async def get(self, jid):
            return None

        async def create(self, jid, nick):
            raise Exception("fail!")

    class FakeBot:
        async def get_roster(self):
            called.append("roster")

        boundjid = type("bjid", (), {"bare": "jidval"})()
        db = types.SimpleNamespace(users=FakeDBUsers())

    monkeypatch.setattr(_reg_profile, "update_vcard",
                        lambda bot: _awaitable(None))
    monkeypatch.setattr(_reg_profile, "update_avatar",
                        lambda bot: _awaitable(None))
    monkeypatch.setattr(
        _reg_profile,
        "log",
        types.SimpleNamespace(
            info=lambda m: called.append(m),
            error=lambda m: called.append("error"),
        ),
    )
    monkeypatch.setattr(_reg_profile, "config", {"nick": "nick"})

    await _reg_profile.setup_profile(FakeBot())

    assert "error" in called


@pytest.mark.asyncio
async def test_on_load_and_on_ready(monkeypatch):
    called = []

    class DummyStore:
        async def set(self, jid, k, v):
            called.append((jid, k, v))

    class DummyUsers:
        def plugin(self, name):
            return DummyStore()

    class Bot:
        def register_plugin(self, name):
            called.append(name)

        boundjid = type("Jid", (), {"bare": "jidval"})()
        db = types.SimpleNamespace(users=DummyUsers())

    monkeypatch.setattr(_reg_profile, "setup_profile",
                        lambda bot: _awaitable(None))

    await _reg_profile.on_load(Bot())

    assert "xep_0054" in called
    assert "xep_0084" in called
    assert "xep_0153" in called
    assert "xep_0163" in called

    monkeypatch.setattr(_reg_profile, "config", {
                        "timezone": "Europe/Stockholm"})

    await _reg_profile.on_ready(Bot())

    assert any(isinstance(x, tuple) and x[1] == "TIMEZONE" for x in called)


def _awaitable(val):
    async def awt(*a, **k):
        return val

    return awt()
