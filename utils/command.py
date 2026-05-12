"""
command.py

Provides a system for registering, managing, and resolving bot commands,
including role-based permissions and plugin integration.
"""

from __future__ import annotations
from enum import IntEnum
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


class Role(IntEnum):
    """
    Enum representing user roles for command permissions.
    Lower numbers indicate higher privileges. The range 1–100 allows
    for future expansion of roles and fine-grained access control.
    """

    OWNER = 1
    SUPERADMIN = 10
    ADMIN = 20
    MODERATOR = 40
    TRUSTED = 60
    USER = 80
    NEW = 90
    NONE = 95
    BANNED = 100

    def __str__(self):
        """Return the lowercase string name of the role."""
        return self.name.lower()


def role_from_int(value: int) -> Role:
    """
    Convert an integer value to a Role enum member.
    Returns USER if the value does not match any defined role.
    """
    try:
        return Role(value)
    except ValueError:
        return Role.USER


def is_banned(role: Role) -> bool:
    """
    Determine if the given role is considered banned.
    Returns True if the role is BANNED or higher.
    """
    return role >= Role.BANNED


class CommandRegistry:
    """
    Central registry for all commands exposed by plugins.
    Supports registration, removal, and lookup of commands by name,
    handler, plugin, or prefix for efficient command management.
    """

    def __init__(self):
        """Initialize the command registry with empty indices."""
        self.index: Dict[Tuple[str, ...], Command] = {}
        self.by_handler: Dict[object, set[tuple[str, ...]]] = {}
        self.by_plugin: Dict[str, set[tuple[str, ...]]] = {}
        self.by_prefix: Dict[str, set[tuple[str, ...]]] = {}

    def register(self, name: str, cmd: "Command", plugin: str | None = None):
        """
        Register a command under the given name and optional plugin.
        Raises ValueError if the command name is already registered.
        """
        tokens = tuple(name.lower().split())
        if not tokens:
            return

        if tokens in self.index:
            existing = self.index[tokens]
            raise ValueError(
                f"Command already registered: '{' '.join(tokens)}' "
                f"(handler={existing.handler.__name__})"
            )

        self.index[tokens] = cmd

        prefix = tokens[0]
        self.by_prefix.setdefault(prefix, set()).add(tokens)

        if plugin:
            self.by_plugin.setdefault(plugin, set()).add(tokens)

        handler = getattr(cmd, "handler", None)
        if handler is not None:
            self.by_handler.setdefault(handler, set()).add(tokens)

    def remove(self, tokens: Tuple[str, ...]):
        cmd = self.index.pop(tokens, None)
        if not cmd:
            return
        prefix = tokens[0]

        if prefix in self.by_prefix:
            self.by_prefix[prefix].discard(tokens)
            if not self.by_prefix[prefix]:
                del self.by_prefix[prefix]

        handler = getattr(cmd, "handler", None)
        if handler in self.by_handler:
            self.by_handler[handler].discard(tokens)
            if not self.by_handler[handler]:
                del self.by_handler[handler]

        # This is the corrected part:
        for plugin, value_set in list(self.by_plugin.items()):
            value_set.discard(tokens)
            if not value_set:
                del self.by_plugin[plugin]

    def remove_by_handler(self, handler):
        """
        Remove all commands associated with a specific handler function.
        Useful for cleaning up commands when unloading plugins.
        """
        tokens = list(self.by_handler.get(handler, ()))
        for t in tokens:
            self.remove(t)

    def remove_by_plugin(self, plugin: str):
        """
        Remove all commands registered by a specific plugin.
        Cleans up plugin-related command entries.
        """
        tokens = list(self.by_plugin.get(plugin, ()))

        for t in tokens:
            self.remove(t)

        self.by_plugin.pop(plugin, None)

    def items(self):
        """
        Return all registered commands as (tokens, Command) pairs.
        Useful for iterating over the command registry.
        """
        return self.index.items()

    def get(self, tokens):
        """
        Retrieve a command by its token tuple.
        Returns the Command instance or None if not found.
        """
        return self.index.get(tokens)

    def debug_dump(self) -> Dict[str, dict]:
        """
        Return a structured snapshot of the command registry for debugging.
        Includes handler names, required roles, and aliases for each command.
        """
        data = {}

        for tokens, cmd in self.index.items():
            name = " ".join(tokens)

            data[name] = {
                "handler": getattr(cmd.handler, "__name__", str(cmd.handler)),
                "role": str(cmd.role),
                "aliases": list(cmd.aliases),
            }

        return data


@dataclass
class Command:
    """
    Represents a registered command, including its name, handler function,
    required role for execution, and any aliases.
    """

    name: str
    handler: Callable
    role: Role = Role.NONE
    aliases: List[str] = field(default_factory=list)


COMMANDS = CommandRegistry()


def _register(name: str, cmd: Command):
    """
    Attach command metadata to the handler for plugin registration.
    Prevents duplicate registrations during plugin reloads by checking
    existing metadata on the handler.
    """
    tokens = tuple(name.lower().split())

    if not tokens:
        return

    if not hasattr(cmd.handler, "__commands__"):
        cmd.handler.__commands__ = []
    else:
        if not isinstance(cmd.handler.__commands__, list):
            cmd.handler.__commands__ = []

    entry = (name, cmd)

    # Prevent duplicate registrations during plugin reload
    if entry not in cmd.handler.__commands__:
        cmd.handler.__commands__.append((name, cmd))


def command(
    name: str,
    role: Role = Role.NONE,
    aliases: Optional[List[str]] = None,
):
    """
    Decorator to register a function as a command with the given name,
    required role, and optional aliases. Attaches metadata to the handler.
    """
    if aliases is None:
        aliases = []

    def decorator(func: Callable):
        """
        Decorator function that attaches command metadata to the handler.
        Registers the command and its aliases for later plugin integration.
        """
        cmd = Command(
            name=name,
            handler=func,
            role=role,
            aliases=aliases,
        )

        _register(name, cmd)

        for alias in aliases:
            _register(alias, cmd)

        func._command = name
        func._command_names = [name] + aliases
        func._required_role = role
        func._aliases = aliases

        return func

    return decorator


def resolve_command(text: str):
    """
    Resolve the longest matching command from a text input string.
    Returns a tuple of (Command, arguments) if found, or (None, tokens)
    if no command matches the input.
    """
    tokens = text.split()

    if not tokens:
        return None, []

    lower_tokens = [t.lower() for t in tokens]

    best_cmd = None
    best_len = 0

    candidates = COMMANDS.by_prefix.get(lower_tokens[0], ())

    for cmd_tokens in candidates:

        cmd = COMMANDS.get(cmd_tokens)

        n = len(cmd_tokens)

        if len(lower_tokens) < n:
            continue

        if tuple(lower_tokens[:n]) == cmd_tokens:

            if n > best_len:
                best_cmd = cmd
                best_len = n

    if best_cmd is None:
        return None, tokens

    args = tokens[best_len:]

    return best_cmd, args


def has_permission(user_role: Role, required_role: Role) -> bool:
    """
    Check if a user with user_role is permitted to execute a command
    requiring required_role. Returns False if the user is banned.
    """
    if is_banned(user_role):
        return False

    return user_role <= required_role


def check_permission(user_role: Role, cmd: Command) -> bool:
    """
    Check if a user with user_role is allowed to execute the given command.
    Uses the command's required role for comparison.
    """
    return has_permission(user_role, cmd.role)


def debug_leaks():
    """
    Print debug information about the command registry to help detect
    memory leaks or improper cleanup of command references.
    """
    print("\n--- COMMAND REGISTRY DEBUG ---")

    print("index size:", len(COMMANDS.index))
    print("by_handler size:", len(COMMANDS.by_handler))
    print("by_plugin size:", len(COMMANDS.by_plugin))
    print("by_prefix size:", len(COMMANDS.by_prefix))

    if COMMANDS.by_handler:
        print("\nHandlers still referenced:")
        for handler, tokens in COMMANDS.by_handler.items():
            print(" ", handler, "->", tokens)

    if COMMANDS.by_plugin:
        print("\nPlugins still registered:")
        for plugin, tokens in COMMANDS.by_plugin.items():
            print(" ", plugin, "->", tokens)

    print("--- END DEBUG ---\n")
