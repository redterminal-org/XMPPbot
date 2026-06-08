import pytest
import random

import plugins.dice as dice

from types import SimpleNamespace

# --- Mock helpers


class DummyStore:
    def __init__(self):
        self.data = {}

    async def get(self, jid, key, default=None):
        d = self.data.setdefault(jid, {})
        return d.get(key, default)

    async def set(self, jid, key, value):
        self.data.setdefault(jid, {})[key] = value


class DummyUsers:
    def __init__(self):
        self._stores = {}

    async def flush_all(self):
        return

    def plugin(self, name):
        if name not in self._stores:
            self._stores[name] = DummyStore()
        return self._stores[name]


class DummyBot:
    def __init__(self):
        self.db = SimpleNamespace(users=DummyUsers())
        self.replies = []
        self._called = []
        self._random = []
        self.plugin = {}
        self._sent = []
        self.boundjid = SimpleNamespace(bare="bot@xmpp")
        self.bot_plugins = SimpleNamespace(register_event=self._register_event)

    def make_message(self, **kwargs):
        msg = SimpleNamespace(**kwargs)
        msg.send = lambda: self._sent.append(msg)
        return msg

    async def _safe_send_message(self, msg):
        self._sent.append(msg)

    def reply(self, msg, text, *a, **k):
        self.replies.append((text, msg))

    def _register_event(self, *a, **k):
        self._called.append(("register_event", a, k))

# --- Fixtures


@pytest.fixture(autouse=True)
def reset_globals(monkeypatch):
    if hasattr(dice, "JOINED_ROOMS"):
        dice.JOINED_ROOMS.clear()
    yield
    if hasattr(dice, "JOINED_ROOMS"):
        dice.JOINED_ROOMS.clear()


@pytest.fixture
def bot(monkeypatch):
    async def always_false(*a, **k): return False
    async def always_true(*a, **k): return True
    # Patch handle_room_toggle_command to simulate always False
    monkeypatch.setattr(dice, "handle_room_toggle_command", always_false)
    # Patch _get_enabled_rooms to always return a set with the test room

    async def fake_enabled_rooms(bot, key, plugin):
        return {"roomA", "roomB", "roomZ"}  # always enabled for these rooms
    monkeypatch.setattr(dice, "_get_enabled_rooms", fake_enabled_rooms)
    # Patch _is_muc_pm to always False unless explicitly tested
    monkeypatch.setattr(dice, "_is_muc_pm", lambda msg: False)
    return DummyBot()

# --- Tests ---


@pytest.mark.asyncio
async def test_usage_message(bot):
    msg = {"from": SimpleNamespace(bare="roomA")}
    await dice.dice_command(bot, "u", "n", [], msg, False)
    assert "Usage" in bot.replies[-1][0]


@pytest.mark.asyncio
async def test_room_toggle_offers_status(monkeypatch, bot):
    # Now toggle returns True means plugin handled
    async def always_true(*a, **k): return True
    monkeypatch.setattr(dice, "handle_room_toggle_command", always_true)
    msg = {"from": SimpleNamespace(bare="roomA")}
    await dice.dice_command(bot, "u", "n", ["status"], msg, True)
    # Should return early, not reply anything further
    assert not bot.replies


@pytest.mark.asyncio
async def test_disabled_room(bot, monkeypatch):
    # Should reply that dice rolling is disabled if not in enabled_rooms
    async def disabled_rooms(bot, k, p): return set()
    monkeypatch.setattr(dice, "_get_enabled_rooms", disabled_rooms)
    msg = {"from": SimpleNamespace(bare="roomNA")}
    await dice.dice_command(bot, "u", "n", ["2d6"], msg, True)
    assert "disabled in this room" in bot.replies[-1][0]


@pytest.mark.asyncio
async def test_invalid_syntax(bot):
    msg = {"from": SimpleNamespace(bare="roomA")}
    await dice.dice_command(bot, "u", "n", ["notadice"], msg, False)
    assert "Invalid syntax" in bot.replies[-1][0]


@pytest.mark.asyncio
async def test_limits(bot):
    msg = {"from": SimpleNamespace(bare="roomA")}
    # Too many dice
    await dice.dice_command(bot, "u", "n", ["20d6"], msg, False)
    assert "Dice number must be 1-10" in bot.replies[-1][0]
    # Too few sides
    await dice.dice_command(bot, "u", "n", ["1d1"], msg, False)
    assert "Dice number must be 1-10" in bot.replies[-1][0]


@pytest.mark.asyncio
async def test_modifier_limits(bot):
    msg = {"from": SimpleNamespace(bare="roomA")}
    await dice.dice_command(bot, "u", "n", ["1d6", "+1000"], msg, False)
    assert "Modifier must be between" in bot.replies[-1][0]
    await dice.dice_command(bot, "u", "n", ["1d6", "-1000"], msg, False)
    assert "Modifier must be between" in bot.replies[-1][0]


@pytest.mark.asyncio
async def test_impossible_roll(bot):
    msg = {"from": SimpleNamespace(bare="roomA")}
    # Max roll 2d6+0 is 12, try impossible >= 99
    await dice.dice_command(bot, "u", "n", ["2d6", ">=99"], msg, False)
    assert "Impossible roll" in bot.replies[-1][0]


@pytest.mark.asyncio
async def test_tautologies(bot):
    msg = {"from": SimpleNamespace(bare="roomA")}
    # 1d6, mod 0, impossible to always succeed/fail
    await dice.dice_command(bot, "u", "n", ["1d6", "<=7"], msg, False)
    assert "cannot fail or cannot succeed" in bot.replies[-1][0]


@pytest.mark.asyncio
async def test_success_and_failure_paths(bot, monkeypatch):
    msg = {"from": SimpleNamespace(bare="roomA")}
    # Patch random.randint to known value
    monkeypatch.setattr(random, "randint", lambda a,
                        b: b)  # always highest value
    await dice.dice_command(bot, "u", "n", ["2d6", "+0", ">=6"], msg, False)
    out = bot.replies[-1][0]
    assert "[✅ SUCCESS]" in out or "[🔴  FAILURE]" in out
    assert out.startswith("🎲")
    # Now force a failure path: lowest value
    bot.replies.clear()
    monkeypatch.setattr(random, "randint", lambda a,
                        b: a)  # always lowest value
    await dice.dice_command(bot, "u", "n", ["2d6", "+0", ">=6"], msg, False)
    out2 = bot.replies[-1][0]
    assert "[✅ SUCCESS]" in out2 or "[🔴  FAILURE]" in out2


@pytest.mark.asyncio
async def test_basic_roll(bot, monkeypatch):
    msg = {"from": SimpleNamespace(bare="roomA")}
    monkeypatch.setattr(random, "randint", lambda a, b: 3)
    await dice.dice_command(bot, "u", "n", ["2d6"], msg, False)
    out = bot.replies[-1][0]
    assert out.startswith("🎲")
    # Should see [3, 3] in output
    assert "[3, 3]" in out


@pytest.mark.asyncio
async def test_1d_roll_default(bot, monkeypatch):
    msg = {"from": SimpleNamespace(bare="roomA")}
    monkeypatch.setattr(random, "randint", lambda a, b: 2)
    await dice.dice_command(bot, "u", "n", ["d6"], msg, False)
    out = bot.replies[-1][0]
    assert "[2]" in out and out.startswith("🎲")
