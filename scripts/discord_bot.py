"""Discord approval bot.

A single phase run typically makes 1-5 approval requests. Lifecycle:

    bot = ApprovalBot()
    await bot.connect()
    decision = await bot.request_approval(orders, summary, timeout_s=90)
    if decision.approved:
        ...
    await bot.close()

Or as an async context manager:

    async with ApprovalBot() as bot:
        decision = await bot.request_approval(...)
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Sequence

import discord

from ladder import LimitOrder, average_fill_price, total_notional


APPROVE_EMOJI = "✅"
REJECT_EMOJI = "❌"


class _ReadyAwareClient(discord.Client):
    def __init__(self, ready_event: asyncio.Event, **kwargs):
        super().__init__(**kwargs)
        self._ready_event = ready_event

    async def on_ready(self) -> None:
        self._ready_event.set()


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    reason: str
    message_id: int | None = None

    @classmethod
    def yes(cls, message_id: int) -> "ApprovalDecision":
        return cls(approved=True, reason="user reacted ✅", message_id=message_id)

    @classmethod
    def no(cls, reason: str, message_id: int | None = None) -> "ApprovalDecision":
        return cls(approved=False, reason=reason, message_id=message_id)


class ApprovalBot:
    def __init__(
        self,
        *,
        token: str | None = None,
        channel_id: int | None = None,
        user_id: int | None = None,
    ):
        self._token = token or os.environ.get("DISCORD_TOKEN")
        self._channel_id = int(
            channel_id if channel_id is not None else os.environ["DISCORD_CHANNEL_ID"]
        )
        self._user_id = int(
            user_id if user_id is not None else os.environ["DISCORD_USER_ID"]
        )
        if not self._token:
            raise RuntimeError("DISCORD_TOKEN not set")

        intents = discord.Intents.default()
        self._ready = asyncio.Event()
        self._client = _ReadyAwareClient(self._ready, intents=intents)
        self._runner: asyncio.Task | None = None

    async def connect(self) -> None:
        """Start the gateway connection. Resolves when bot is ready."""
        self._runner = asyncio.create_task(self._client.start(self._token))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=20)
        except asyncio.TimeoutError:
            await self.close()
            raise RuntimeError("Discord bot did not become ready within 20s")

    async def close(self) -> None:
        if not self._client.is_closed():
            await self._client.close()
        if self._runner is not None:
            try:
                await self._runner
            except Exception:
                pass

    async def __aenter__(self) -> "ApprovalBot":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _channel(self) -> discord.TextChannel:
        ch = self._client.get_channel(self._channel_id)
        if ch is None:
            ch = await self._client.fetch_channel(self._channel_id)
        return ch  # type: ignore[return-value]

    async def post_message(self, content: str) -> discord.Message:
        ch = await self._channel()
        return await ch.send(content)

    async def post_error(self, phase: str, error: str) -> None:
        await self.post_message(f"🚨 **{phase}** error: `{error}`")

    async def request_approval(
        self,
        *,
        symbol: str,
        side: str,
        orders: Sequence[LimitOrder],
        context: str = "",
        phase: str = "",
        timeout_s: int = 90,
    ) -> ApprovalDecision:
        """Post a proposal with ✅/❌ reactions and wait for the user.

        Only reactions from DISCORD_USER_ID count. Other reactions are ignored.
        Timeout → ApprovalDecision.no("timeout").
        """
        if not orders:
            return ApprovalDecision.no("empty order list")

        embed = self._build_proposal_embed(
            symbol=symbol, side=side, orders=orders, context=context, phase=phase
        )
        ch = await self._channel()
        msg = await ch.send(embed=embed)
        await msg.add_reaction(APPROVE_EMOJI)
        await msg.add_reaction(REJECT_EMOJI)

        def check(reaction: discord.Reaction, user: discord.User) -> bool:
            return (
                reaction.message.id == msg.id
                and user.id == self._user_id
                and str(reaction.emoji) in (APPROVE_EMOJI, REJECT_EMOJI)
            )

        try:
            reaction, _user = await self._client.wait_for(
                "reaction_add", check=check, timeout=timeout_s
            )
        except asyncio.TimeoutError:
            await msg.reply(f"⏱ approval timeout after {timeout_s}s — order canceled.")
            return ApprovalDecision.no(f"timeout after {timeout_s}s", message_id=msg.id)

        if str(reaction.emoji) == APPROVE_EMOJI:
            await msg.reply("✅ approved — placing orders…")
            return ApprovalDecision.yes(message_id=msg.id)

        await msg.reply("❌ rejected — no orders placed.")
        return ApprovalDecision.no("user rejected", message_id=msg.id)

    async def post_fill_update(
        self,
        *,
        symbol: str,
        filled: int,
        total: int,
        avg_price: float,
        reply_to_message_id: int | None = None,
    ) -> None:
        text = (
            f"📥 **{symbol}** fills: {filled}/{total} shares "
            f"@ avg ${avg_price:.2f}"
        )
        ch = await self._channel()
        if reply_to_message_id is not None:
            try:
                ref = await ch.fetch_message(reply_to_message_id)
                await ref.reply(text)
                return
            except discord.NotFound:
                pass
        await ch.send(text)

    def _build_proposal_embed(
        self,
        *,
        symbol: str,
        side: str,
        orders: Sequence[LimitOrder],
        context: str,
        phase: str,
    ) -> discord.Embed:
        avg = average_fill_price(orders)
        total_qty = sum(o.qty for o in orders)
        notional = total_notional(orders)
        lo = min(o.price for o in orders)
        hi = max(o.price for o in orders)
        rungs_str = " · ".join(
            f"${o.price:.2f}×{o.qty}" for o in orders[:10]
        )
        if len(orders) > 10:
            rungs_str += f" · …+{len(orders) - 10}"

        color = 0x2ECC71 if side == "buy" else 0xE67E22
        title = f"PROPOSED: {side.upper()} {symbol} — {total_qty}sh"
        embed = discord.Embed(title=title, color=color)
        embed.add_field(
            name="Avg fill (if all rungs fill)",
            value=f"${avg:.2f}",
            inline=True,
        )
        embed.add_field(
            name="Total notional",
            value=f"${notional:,.2f}",
            inline=True,
        )
        embed.add_field(
            name="Range",
            value=f"${lo:.2f} – ${hi:.2f} ({len(orders)} rungs)",
            inline=True,
        )
        embed.add_field(name="Ladder", value=rungs_str, inline=False)
        if context:
            embed.add_field(name="Context", value=context[:1024], inline=False)
        ext = "ext-hrs" if any(o.extended_hours for o in orders) else "RTH"
        footer = f"phase: {phase} · session: {ext} · react ✅ to send, ❌ to cancel"
        embed.set_footer(text=footer)
        return embed
