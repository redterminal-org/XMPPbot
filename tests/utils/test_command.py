import pytest
from utils.command import Role, role_from_int, is_banned, CommandRegistry


def fake_handler1():
    return 1


def fake_handler2():
    return 2


class FakeCommand:
    def __init__(self, handler): self.handler = handler


def test_role_enum_and_str():
    assert str(Role.USER) == "user"
    assert str(Role.BANNED) == "banned"


@pytest.mark.parametrize("val,expected", [
    (1, Role.OWNER), (80, Role.USER), (90, Role.NEW), (42, Role.USER), (999, Role.USER)
])
def test_role_from_int_various(val, expected):
    assert role_from_int(val) == expected


@pytest.mark.parametrize("role,result", [
    (Role.BANNED, True), (Role.NONE, False), (Role.ADMIN, False), (Role.USER, False)
])
def test_is_banned_for_various_roles(role, result):
    assert is_banned(role) == result


def test_registry_register_and_remove_and_plugin_indices():
    reg = CommandRegistry()
    c1 = FakeCommand(fake_handler1)
    c2 = FakeCommand(fake_handler2)
    reg.register("foo bar", c1, "pluginA")
    reg.register("hello", c2, None)
    # All registered?
    assert ("foo", "bar") in reg.index
    assert ("hello",) in reg.index
    # by_plugin
    assert "pluginA" in reg.by_plugin
    assert ("foo", "bar") in reg.by_plugin["pluginA"]
    # by_handler
    assert c1.handler in reg.by_handler
    assert ("foo", "bar") in reg.by_handler[c1.handler]
    # by_prefix
    assert "foo" in reg.by_prefix
    assert ("foo", "bar") in reg.by_prefix["foo"]
    # Remove "foo bar"
    reg.remove(("foo", "bar"))
    assert ("foo", "bar") not in reg.index
    for value_set in reg.by_plugin.values():
        assert ("foo", "bar") not in value_set


def test_registry_register_duplicate_raises():
    reg = CommandRegistry()
    c1 = FakeCommand(fake_handler1)
    reg.register("baz", c1)
    with pytest.raises(ValueError):
        reg.register("baz", c1)


def test_registry_remove_nonexistent_does_nothing():
    reg = CommandRegistry()
    c1 = FakeCommand(fake_handler1)
    reg.register("abc", c1)
    # Should do nothing
    reg.remove(("notareal",))
    assert ("abc",) in reg.index
