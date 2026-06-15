import types
import pytest

from utils.plugin_manager import PluginManager


class FakeBot:
    def __init__(self):
        self.calls = []

    def on(self, event, handler):
        self.calls.append(("on", event, handler))

    def off(self, event, handler):
        self.calls.append(("off", event, handler))


def make_fake_plugin(meta=None, has_hooks=True):
    mod = types.ModuleType("fake_plugin")
    meta = meta or {}
    setattr(mod, 'PLUGIN_META', meta)
    if has_hooks:
        async def _on_load(bot): mod.on_load_called = True
        async def _on_unload(bot): mod.on_unload_called = True
        setattr(mod, 'on_load', _on_load)
        setattr(mod, 'on_unload', _on_unload)
    else:
        # Use a safe no-op lambda instead of None
        setattr(mod, 'on_load', lambda bot: None)
        setattr(mod, 'on_unload', lambda bot: None)
    setattr(mod, "BOT_EVENTS", [])
    setattr(mod, "__name__", meta.get("name", "fake_plugin"))
    return mod


@pytest.mark.asyncio
async def test_lifecycle_full_load_and_unload(monkeypatch):
    bot = FakeBot()
    pm = PluginManager(bot=bot, package="fakepkg")
    mod = make_fake_plugin(meta={'name': 'p1'})
    # Patch import_module to always return our mod
    monkeypatch.setattr("utils.plugin_manager.importlib.import_module",
                        lambda name: mod)
    # Patch iter_modules to simulate one plugin 'p1'

    class SimpleModule:
        name = "p1"
    monkeypatch.setattr("utils.plugin_manager.pkgutil.iter_modules",
                        lambda path: [SimpleModule()])
    # Actually load and unload
    pm.meta['p1'] = {'name': 'p1'}
    pm.plugins.clear()
    await pm.load('p1')
    assert 'p1' in pm.plugins
    assert getattr(mod, "on_load_called", False)
    await pm.unload('p1')
    assert 'p1' not in pm.plugins
    assert getattr(mod, "on_unload_called", False)


@pytest.mark.asyncio
async def test_load_plugin_with_no_hooks(monkeypatch):
    bot = FakeBot()
    pm = PluginManager(bot=bot)
    # Use safe no-op for hooks
    mod = make_fake_plugin(meta={'name': 'nohooks'}, has_hooks=False)
    monkeypatch.setattr("utils.plugin_manager.importlib.import_module", lambda name: mod)
    pm.meta["nohooks"] = {'name': 'nohooks'}
    await pm.load("nohooks")


@pytest.mark.asyncio
async def test_load_all_sorted(monkeypatch):
    bot = FakeBot()
    pm = PluginManager(bot, package="fakepkg")
    pm.meta = {
        "A": {"name": "A", "requires": []},
        "B": {"name": "B", "requires": ["A"]},
    }
    fake_modA = make_fake_plugin(meta={"name": "A"})
    fake_modB = make_fake_plugin(meta={"name": "B"})
    monkeypatch.setattr("utils.plugin_manager.importlib.import_module", lambda name: fake_modA if "A" in name else fake_modB)
    monkeypatch.setattr(pm, "discover", lambda: ["A", "B"])
    await pm.load_all()
    assert set(pm.plugins.keys()) == {"A", "B"}


@pytest.mark.asyncio
async def test_reload_plugins(monkeypatch):
    bot = FakeBot()
    pm = PluginManager(bot)
    modA = make_fake_plugin(meta={"name": "A"})
    modB = make_fake_plugin(meta={"name": "B"})
    pm.meta = {"A": {"name": "A"}, "B": {"name": "B"}}
    pm.plugins = {"A": modA, "B": modB}
    monkeypatch.setattr(pm, "discover", lambda: ["A", "B"])
    monkeypatch.setattr("utils.plugin_manager.importlib.import_module", lambda name: modA if "A" in name else modB)
    for name in ["A", "B"]:
        await pm.reload(name)
    assert set(pm.plugins.keys()) == {"A", "B"}


@pytest.mark.asyncio
async def test_unload_with_dependents(monkeypatch):
    bot = FakeBot()
    pm = PluginManager(bot)
    pm.meta = {
        "X": {"name": "X", "requires": []},
        "Y": {"name": "Y", "requires": ["X"]},
    }
    pm.plugins["X"] = make_fake_plugin(meta={"name": "X"})
    pm.plugins["Y"] = make_fake_plugin(meta={"name": "Y"})
    # Should not unload since dependents exist, and no Exception is raised
    await pm.unload("X")
    assert "X" in pm.plugins
    # Remove the dependent and now it should succeed
    pm.plugins.pop("Y")
    pm.meta.pop("Y")
    await pm.unload("X")
    assert "X" not in pm.plugins
