"""
Dice rolling plugin.

To set/show plugin status in rooms, use:
    {prefix}dice on|off|status

Command:
    {prefix}dice <num>d<sides> [modifier] [operator] [target]
    {prefix}roll ...
    {prefix}r ...

Examples:
    {prefix}dice 3d20 -5 >= 30
    {prefix}roll 2d6 +2
    {prefix}r 1d100
    {prefix}dice d6
"""

import re
import random
from utils.command import command, Role
from utils.config import config
from plugins._core import (
        _is_muc_pm,
        handle_room_toggle_command,
        _get_enabled_rooms,
)

DICE_KEY = "DICE"
PLUGIN_META = {
    "name": "dice",
    "version": "0.2.0",
    "description": "Roll dice with optional modifiers and success conditions.",
    "category": "games",
}

DICE_RE = re.compile(
    r"^\s*(?:(\d+)?[dD](\d+))\s*([+-]\d+)?\s*"
    r"(<=|>=|<|>)?\s*(\d+)?\s*$"
)


@command("dice", role=Role.USER, aliases=["roll", "r"])
async def dice_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Roll dice with optional modifier and success/failure condition.

    For plugin on|off|status in rooms, use:
        {prefix}dice on|off|status

    Usage:
        {prefix}dice <num>d<sides> [modifier] [operator] [target]
        {prefix}roll ...
        {prefix}r ...

    Examples:
        {prefix}dice 3d20 -5 >= 30
        {prefix}roll 2d6 +2
        {prefix}r 1d100
        {prefix}dice d6
    """
    if not args:
        bot.reply(
            msg,
            f"🟡️ Usage: {config.get('prefix', ',')}dice <num>d<sides> "
            "[modifier] [operator] [target]"
        )
        return

    if is_room or _is_muc_pm(msg):
        handled = await handle_room_toggle_command(
            bot,
            msg,
            is_room,
            args,
            store_getter=get_dice_store,
            key=DICE_KEY,
            label="Roll Dice",
            storage="dict",
            log_prefix="[DICE]",
        )
        if handled:
            return

    enabled_rooms = await _get_enabled_rooms(bot, DICE_KEY, "dice")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ Dice Rolling is disabled in this room.")
        return

    expr = " ".join(args)
    m = DICE_RE.match(expr)
    if not m:
        bot.reply(
            msg,
            f"🟡️ Invalid syntax. Example: {config.get('prefix', ',')}dice "
            "3d20 -5 >= 30"
        )
        return

    num, sides, mod, op, target = m.groups()
    num = int(num) if num else 1
    sides = int(sides)
    if num < 1 or num > 10 or sides < 2 or sides > 100:
        bot.reply(
            msg,
            "🟡️ Dice number must be 1-10 and sides 2-100."
        )
        return

    rolls = [random.randint(1, sides) for _ in range(num)]
    mod_val = int(mod) if mod else 0
    if mod_val >= 1000 or mod_val <= -1000:
        bot.reply(
            msg,
            "🟡️ Modifier must be between -999 and 999."
        )
        return
    total = sum(rolls) + mod_val

    mod_str = f" {mod_val:+d}" if mod else ""
    result_str = f"[{', '.join(str(r) for r in rolls)}]{mod_str} = {total}"

    if op and target:
        target = int(target)
        min_result = num * 1 + mod_val
        max_result = num * sides + mod_val
        if ((op in (">=", ">") and max_result < target) or
                (op in ("<=", "<") and min_result > target)):
            bot.reply(
                msg,
                "🟡️ Impossible roll: result cannot reach the target."
            )
            return
        can_succeed = (
            (op == ">=" and max_result >= target) or
            (op == ">" and max_result > target) or
            (op == "<=" and min_result <= target) or
            (op == "<" and min_result < target)
        )
        can_fail = (
            (op == ">=" and min_result < target) or
            (op == ">" and min_result <= target) or
            (op == "<=" and max_result > target) or
            (op == "<" and max_result >= target)
        )
        if not (can_succeed and can_fail):
            bot.reply(
                msg,
                "🟡️ This roll cannot fail or cannot succeed. Please adjust "
                "your modifier or target."
            )
            return
        success = False
        if op == ">=":
            success = total >= target
        elif op == "<=":
            success = total <= target
        elif op == ">":
            success = total > target
        elif op == "<":
            success = total < target
        cond_str = f"{op} {target}"
        if success:
            result_str += f" {cond_str} [✅ SUCCESS]"
        else:
            result_str += f" {cond_str} [🔴  FAILURE]"
    bot.reply(msg, f"🎲 {result_str}", ephemeral=False)


async def get_dice_store(bot):
    return bot.db.users.plugin("dice")
