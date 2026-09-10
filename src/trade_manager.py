from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .models import ExecutionReport, ManageCommand, PendingSnapshot, PositionSnapshot, Tick
from .safety import Safety, SafetyError
from .state import ActiveSignalState, comment_belongs, comment_leg

if TYPE_CHECKING:
    from .runtime import BotRuntime


class TradeManager:
    def __init__(self, runtime: BotRuntime) -> None:
        self.runtime = runtime

    async def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.runtime.logs.error(f"Gestor: {exc}")
            await asyncio.sleep(1)

    def tick(self) -> None:
        runtime = self.runtime
        settings = runtime.config.get()
        account = runtime.mt5.account()
        market = runtime.mt5.tick(settings.symbol)
        if market is None:
            return
        all_pos = runtime.mt5.positions(settings.symbol, settings.magic)
        all_pend = runtime.mt5.pendings(settings.symbol, settings.magic)
        claimed: set[int] = set()
        for active in runtime.state.list_actives():
            positions = match_positions(active, all_pos, claimed, settings)
            pendings = match_pendings(active, all_pend, claimed, settings)
            claimed.update(pos.ticket for pos in positions)
            claimed.update(pend.ticket for pend in pendings)
            if not positions and not pendings:
                runtime.state.remove_active(active.message_id)
                runtime.logs.info(f"Señal {active.token} cerrada")
                continue
            tp1 = tp1_reached(active, positions, market, runtime, settings)
            needs_be = not active.be_done or _sl_still_at_original(active, positions, settings)
            if needs_be and tp1:
                self._move_to_break_even(active, positions, pendings, market, settings, account)
            if (
                active.be_done
                and not active.tp2_done
                and active.tp2
                and tp2_reached(active, positions, market)
            ):
                self._lock_at_tp1(active, positions, pendings, market, settings, account)
            if settings.trail_enabled and active.tp2_done:
                self._trail(active, positions, market, settings, account)

    def apply_channel_manage(self, command: ManageCommand, source: str = "telegram") -> ExecutionReport:
        """Telegram: cierra L1 en BE y mueve el SL de L2/L3 a la entrada."""
        runtime = self.runtime
        settings = runtime.config.get()
        account = runtime.mt5.account()
        actives = [item for item in runtime.state.list_actives() if not item.manage_done]
        if not actives:
            return ExecutionReport(
                dry_run=settings.dry_run,
                source=source,
                rejected="No hay posición activa para esa gestión.",
            )
        runtime.logs.info(f"Gestión canal {command.kind}: L1 a BE, SL resto a entrada")
        if settings.dry_run:
            runtime.logs.info("Gestión TP1 (dry-run): no se envía a MT5")
            return ExecutionReport(dry_run=True, source=source, rejected=None, placed=[{"status": "dry-run"}])
        if not _can_manage(settings, account, runtime, "gestión canal"):
            return ExecutionReport(dry_run=False, source=source, rejected="Gestión bloqueada")
        market = runtime.mt5.tick(settings.symbol)
        if market is None:
            return ExecutionReport(dry_run=False, source=source, rejected="Sin precio de MT5")
        all_pos = runtime.mt5.positions(settings.symbol, settings.magic)
        all_pend = runtime.mt5.pendings(settings.symbol, settings.magic)
        actions: list[dict] = []
        claimed: set[int] = set()
        min_dist = runtime.mt5.stop_distance(settings.symbol)
        pip = max(settings.pip_size, 0.01)
        applied = False
        for active in actives:
            positions = match_positions(active, all_pos, claimed, settings)
            pendings = match_pendings(active, all_pend, claimed, settings)
            claimed.update(pos.ticket for pos in positions)
            claimed.update(pend.ticket for pend in pendings)
            if not positions and not pendings:
                continue
            applied = True
            first_pos = [pos for pos in positions if position_leg(active, pos) == 1]
            rest_pos = [pos for pos in positions if position_leg(active, pos) != 1]
            first_pend = [pend for pend in pendings if position_leg(active, pend) == 1]
            rest_pend = [pend for pend in pendings if position_leg(active, pend) != 1]
            entry = active.entry or (positions[0].price_open if positions else pendings[0].price)
            for pos in first_pos:
                actions.append(self._close_first_at_be(pos, entry, market, min_dist, pip, settings))
            for pend in first_pend:
                actions.append(self._cancel_first_pending(pend))
            for pos in rest_pos:
                actions.append(
                    self._sl_to_entry(active.direction, pos, entry, market, min_dist, pip, settings, None)
                )
            for pend in rest_pend:
                actions.append(
                    self._sl_to_entry(
                        active.direction, pend, entry, market, min_dist, pip, settings, pend.price
                    )
                )
            active.manage_done = True
            active.be_done = True
            runtime.state.update_active(active)
            runtime.logs.info(f"Gestión canal {active.token}: L1 BE / SL resto a entrada {entry:.2f}")
        if not applied:
            return ExecutionReport(
                dry_run=False,
                source=source,
                rejected="Hay señal activa pero no encontré sus órdenes en MT5.",
            )
        return ExecutionReport(dry_run=False, source=source, rejected=None, placed=actions)

    def _close_first_at_be(self, pos, entry: float, market: Tick, min_dist: float, pip: float, settings) -> dict:
        profitable = pos.profit >= 0 or _price_at_or_beyond_entry(pos.side, pos.price_open, market)
        if profitable:
            try:
                result = self.runtime.mt5.close_position(pos, settings)
            except Exception as exc:  # noqa: BLE001
                self.runtime.logs.error(f"No pude cerrar L1 {pos.ticket}: {exc}")
                return {"action": "close_l1", "ticket": pos.ticket, "ok": False, "error": str(exc)}
            if result.get("ok"):
                self.runtime.logs.info(f"Cierro primera (L1) ticket {pos.ticket} en BE/mercado")
            else:
                self.runtime.logs.error(
                    f"Cierre L1 {pos.ticket} retcode={result.get('retcode')} "
                    f"{result.get('hint') or result.get('comment')}"
                )
            return {"action": "close_l1", "ticket": pos.ticket, **result}
        ok = _push_sl(
            self.runtime,
            pos.side,
            pos.ticket,
            pos.sl,
            pos.tp,
            entry,
            market,
            min_dist,
            pip,
            settings,
            f"L1 aún en pérdida: SL a entrada {entry:.2f}",
            None,
        )
        return {"action": "sl_l1_entry", "ticket": pos.ticket, "ok": ok, "sl": entry}

    def _cancel_first_pending(self, pend) -> dict:
        try:
            result = self.runtime.mt5.cancel_pending(pend.ticket)
        except Exception as exc:  # noqa: BLE001
            self.runtime.logs.error(f"No pude cancelar L1 pending {pend.ticket}: {exc}")
            return {"action": "cancel_l1", "ticket": pend.ticket, "ok": False, "error": str(exc)}
        if result.get("ok"):
            self.runtime.logs.info(f"Cancelo primera pending L1 {pend.ticket}")
        return {"action": "cancel_l1", **result}

    def _sl_to_entry(
        self,
        direction: str,
        item,
        entry: float,
        market: Tick,
        min_dist: float,
        pip: float,
        settings,
        pending_price: float | None,
    ) -> dict:
        ok = _push_sl(
            self.runtime,
            direction,
            item.ticket,
            item.sl,
            item.tp,
            entry,
            market,
            min_dist,
            pip,
            settings,
            f"SL a entrada {entry:.2f}",
            pending_price,
        )
        return {
            "action": "sl_to_entry",
            "ticket": item.ticket,
            "ok": ok,
            "sl": entry,
        }

    def _move_to_break_even(
        self,
        active: ActiveSignalState,
        positions: list[PositionSnapshot],
        pendings: list[PendingSnapshot],
        market: Tick,
        settings,
        account,
    ) -> None:
        if not positions and not pendings:
            return
        if not _can_manage(settings, account, self.runtime, "BE"):
            return
        total_vol = sum(pos.volume for pos in positions) or sum(pend.volume for pend in pendings)
        if total_vol <= 0:
            return
        if positions:
            avg = sum(pos.price_open * pos.volume for pos in positions) / sum(pos.volume for pos in positions)
        else:
            avg = sum(pend.price * pend.volume for pend in pendings) / sum(pend.volume for pend in pendings)
        offset = settings.be_profit_pips * settings.pip_size
        desired = sl_break_even(active.direction, avg, offset)
        min_dist = self.runtime.mt5.stop_distance(settings.symbol)
        ok = self._apply_sl(
            active.direction,
            positions,
            pendings,
            desired,
            market,
            min_dist,
            settings,
            f"SL a BE+ {desired:.2f} (media {avg:.2f}, +{settings.be_profit_pips} pips)",
        )
        if ok:
            active.be_done = True
            self.runtime.state.update_active(active)
            self.runtime.logs.info(f"TP1: BE+ aplicado en señal {active.token}")
        else:
            self.runtime.logs.warn(
                f"TP1 detectado en {active.token} pero el SL aún no quedó en BE+ {desired:.2f}; reintento"
            )

    def _lock_at_tp1(
        self,
        active: ActiveSignalState,
        positions: list[PositionSnapshot],
        pendings: list[PendingSnapshot],
        market: Tick,
        settings,
        account,
    ) -> None:
        if not positions and not pendings:
            active.tp2_done = True
            self.runtime.state.update_active(active)
            return
        if not _can_manage(settings, account, self.runtime, "lock TP1"):
            return
        cushion = settings.be_cushion_pips * settings.pip_size
        desired = sl_lock_tp1(active.direction, active.tp1, cushion)
        min_dist = self.runtime.mt5.stop_distance(settings.symbol)
        ok = self._apply_sl(
            active.direction,
            positions,
            pendings,
            desired,
            market,
            min_dist,
            settings,
            f"TP2 hecho: SL del runner a TP1 {desired:.2f} (TP3 sigue abierto)",
        )
        if ok:
            active.tp2_done = True
            self.runtime.state.update_active(active)

    def _trail(
        self,
        active: ActiveSignalState,
        positions: list[PositionSnapshot],
        market: Tick,
        settings,
        account,
    ) -> None:
        if not positions:
            return
        if not _can_manage(settings, account, self.runtime, "trail"):
            return
        floor = sl_lock_tp1(active.direction, active.tp1, settings.be_cushion_pips * settings.pip_size)
        min_dist = self.runtime.mt5.stop_distance(settings.symbol)
        for pos in positions:
            new_sl = trail_stop(
                active.direction,
                pos.price_open,
                market,
                settings.trail_percent,
                settings.trail_distance_pips * settings.pip_size,
                floor,
            )
            if new_sl is None:
                continue
            clamped = clamp_protective_sl(active.direction, new_sl, market, min_dist)
            if clamped is None or not _sl_improves(active.direction, pos.sl, clamped):
                continue
            try:
                result = self.runtime.mt5.modify_sl(pos.ticket, clamped, pos.tp, settings)
                if result.get("ok"):
                    self.runtime.logs.info(f"Trail SL {clamped:.2f} ticket {pos.ticket}")
                else:
                    self.runtime.logs.error(
                        f"Trail rechazado {pos.ticket} retcode={result.get('retcode')} "
                        f"{result.get('hint') or result.get('comment')}"
                    )
            except Exception as exc:  # noqa: BLE001
                self.runtime.logs.error(f"Trail falló {pos.ticket}: {exc}")

    def _apply_sl(
        self,
        direction: str,
        positions: list[PositionSnapshot],
        pendings: list[PendingSnapshot],
        desired: float,
        market: Tick,
        min_dist: float,
        settings,
        message: str,
    ) -> bool:
        targets = list(positions) + list(pendings)
        if not targets:
            return False
        pip = max(settings.pip_size, 0.01)
        all_at_target = True
        for item in positions:
            if not _push_sl(
                self.runtime,
                direction,
                item.ticket,
                item.sl,
                item.tp,
                desired,
                market,
                min_dist,
                pip,
                settings,
                message,
                pending_price=None,
            ):
                all_at_target = False
        for item in pendings:
            if not _push_sl(
                self.runtime,
                direction,
                item.ticket,
                item.sl,
                item.tp,
                desired,
                market,
                min_dist,
                pip,
                settings,
                message,
                pending_price=item.price,
            ):
                all_at_target = False
        return all_at_target


def sl_break_even(direction: str, avg_entry: float, profit_offset: float) -> float:
    """SL un poco a favor para cubrir spread y cerrar por encima de 0."""
    if direction == "BUY":
        return avg_entry + profit_offset
    return avg_entry - profit_offset


def sl_lock_tp1(direction: str, tp1: float, cushion: float) -> float:
    """Tras TP2, el SL del runner queda en TP1 (un colchón por spread)."""
    if direction == "BUY":
        return tp1 - cushion
    return tp1 + cushion


def trail_stop(
    direction: str,
    entry: float,
    market: Tick,
    trail_percent: float,
    trail_distance: float,
    floor: float,
) -> float | None:
    if direction == "BUY":
        price = market.bid
        profit = price - entry
        if profit <= 0:
            return None
        if trail_percent > 0:
            proposed = entry + profit * (trail_percent / 100.0)
        else:
            proposed = price - trail_distance
        return max(proposed, floor)
    price = market.ask
    profit = entry - price
    if profit <= 0:
        return None
    if trail_percent > 0:
        proposed = entry - profit * (trail_percent / 100.0)
    else:
        proposed = price + trail_distance
    return min(proposed, floor)


def clamp_protective_sl(direction: str, desired: float, tick: Tick, min_distance: float) -> float | None:
    """Ajusta el SL para que el broker no lo rechace (stops_level / lado incorrecto)."""
    gap = max(min_distance, 0.0)
    if direction == "BUY":
        cap = tick.bid - gap
        sl = min(desired, cap)
        return sl if sl < tick.bid else None
    floor = tick.ask + gap
    sl = max(desired, floor)
    return sl if sl > tick.ask else None


def match_positions(
    active: ActiveSignalState,
    all_pos: list[PositionSnapshot],
    claimed: set[int],
    settings,
) -> list[PositionSnapshot]:
    matched = [
        pos
        for pos in all_pos
        if pos.ticket not in claimed and _strict_position(active, pos)
    ]
    if matched:
        return matched
    return _fallback_positions(active, all_pos, claimed, settings)


def match_pendings(
    active: ActiveSignalState,
    all_pend: list[PendingSnapshot],
    claimed: set[int],
    settings,
) -> list[PendingSnapshot]:
    matched = [
        pend
        for pend in all_pend
        if pend.ticket not in claimed and _strict_pending(active, pend)
    ]
    if matched:
        return matched
    return _fallback_pendings(active, all_pend, claimed, settings)


def tp1_reached(
    active: ActiveSignalState,
    positions: list[PositionSnapshot],
    market: Tick,
    runtime,
    settings,
) -> bool:
    if _level_hit(active.direction, active.tp1, market):
        return True
    if _has_leg(1, positions, active):
        return False
    if _has_leg(2, positions, active) or _has_leg(3, positions, active):
        return True
    tickets = [ticket for ticket in (active.tickets or []) if ticket]
    if tickets:
        open_tickets = {pos.ticket for pos in positions}
        if tickets[0] not in open_tickets and any(ticket in open_tickets for ticket in tickets[1:]):
            return True
    try:
        tol = max(settings.pip_size * 3, 0.3)
        return bool(
            runtime.mt5.closed_by_tp(settings.symbol, settings.magic, active.tp1, active.token, tol)
        )
    except Exception:  # noqa: BLE001
        return False


def tp2_reached(active: ActiveSignalState, positions: list[PositionSnapshot], market: Tick) -> bool:
    if _level_hit(active.direction, active.tp2, market):
        return True
    if _has_leg(2, positions, active):
        return False
    return _has_leg(3, positions, active)


def _has_leg(leg: int, positions: list[PositionSnapshot], active: ActiveSignalState) -> bool:
    tickets = active.tickets or []
    if 1 <= leg <= len(tickets) and tickets[leg - 1]:
        target = tickets[leg - 1]
        if any(pos.ticket == target for pos in positions):
            return True
    tp = {1: active.tp1, 2: active.tp2, 3: active.tp3}.get(leg, 0.0)
    for pos in positions:
        if comment_leg(pos.comment) == leg:
            return True
        if tp and abs(pos.tp - tp) <= 0.15:
            return True
    return False


def position_leg(active: ActiveSignalState, item: PositionSnapshot | PendingSnapshot) -> int:
    tickets = active.tickets or []
    if item.ticket in tickets:
        return tickets.index(item.ticket) + 1
    leg = comment_leg(item.comment)
    if leg:
        return leg
    tp = float(getattr(item, "tp", 0) or 0)
    for index, level in enumerate((active.tp1, active.tp2, active.tp3), start=1):
        if level and abs(tp - level) <= 0.15:
            return index
    return 99


def _price_at_or_beyond_entry(side: str, entry: float, market: Tick) -> bool:
    if side == "BUY":
        return market.bid >= entry
    return market.ask <= entry


def _strict_position(active: ActiveSignalState, pos: PositionSnapshot) -> bool:
    if pos.ticket in (active.tickets or []):
        return True
    return comment_belongs(pos.comment, active.token)


def _strict_pending(active: ActiveSignalState, pend: PendingSnapshot) -> bool:
    if pend.ticket in (active.tickets or []):
        return True
    return comment_belongs(pend.comment, active.token)


def _fallback_positions(
    active: ActiveSignalState,
    all_pos: list[PositionSnapshot],
    claimed: set[int],
    settings,
) -> list[PositionSnapshot]:
    by_tp = [
        pos
        for pos in all_pos
        if pos.ticket not in claimed
        and pos.side == active.direction
        and _tp_matches_signal(pos.tp, active)
    ]
    if by_tp:
        return by_tp
    return [
        pos
        for pos in all_pos
        if pos.ticket not in claimed
        and pos.side == active.direction
        and _near(pos.price_open, active.entry, _entry_tol(settings))
    ]


def _fallback_pendings(
    active: ActiveSignalState,
    all_pend: list[PendingSnapshot],
    claimed: set[int],
    settings,
) -> list[PendingSnapshot]:
    by_tp = [
        pend
        for pend in all_pend
        if pend.ticket not in claimed and _tp_matches_signal(pend.tp, active)
    ]
    if by_tp:
        return by_tp
    return [
        pend
        for pend in all_pend
        if pend.ticket not in claimed and _near(pend.price, active.entry, _entry_tol(settings))
    ]


def _push_sl(
    runtime,
    direction: str,
    ticket: int,
    current_sl: float,
    tp: float,
    desired: float,
    market: Tick,
    min_dist: float,
    pip: float,
    settings,
    message: str,
    pending_price: float | None,
) -> bool:
    if _sl_meets(direction, current_sl, desired, pip):
        return True
    clamped = clamp_protective_sl(direction, desired, market, min_dist)
    if clamped is None or not _sl_improves(direction, current_sl, clamped):
        return False
    try:
        if pending_price is None:
            result = runtime.mt5.modify_sl(ticket, clamped, tp, settings)
        else:
            result = runtime.mt5.modify_pending(ticket, pending_price, clamped, tp, settings)
        if result.get("ok"):
            runtime.logs.info(f"{message} ticket {ticket}")
            return _sl_meets(direction, clamped, desired, pip)
        runtime.logs.error(
            f"No se pudo mover SL {ticket} retcode={result.get('retcode')} "
            f"{result.get('hint') or result.get('comment')}"
        )
    except Exception as exc:  # noqa: BLE001
        runtime.logs.error(f"No se pudo mover SL {ticket}: {exc}")
    return False


def _tp_matches_signal(tp: float, active: ActiveSignalState) -> bool:
    if tp <= 0:
        return False
    for level in (active.tp1, active.tp2, active.tp3):
        if level and abs(tp - level) <= 0.15:
            return True
    return False


def _entry_tol(settings) -> float:
    return max(1.0, float(getattr(settings, "pip_size", 0.1) or 0.1) * 10)


def _near(left: float, right: float, tolerance: float) -> bool:
    return right > 0 and abs(left - right) <= tolerance


def _sl_improves(direction: str, current_sl: float, new_sl: float) -> bool:
    if direction == "BUY":
        return current_sl <= 0 or new_sl > current_sl
    return current_sl == 0 or new_sl < current_sl


def _sl_still_at_original(active: ActiveSignalState, positions: list[PositionSnapshot], settings) -> bool:
    if not positions or not active.sl:
        return False
    pip = max(settings.pip_size, 0.01) * 3
    return all(abs(pos.sl - active.sl) <= pip for pos in positions)


def _sl_meets(direction: str, current_sl: float, desired: float, tolerance: float) -> bool:
    if current_sl <= 0:
        return False
    if direction == "BUY":
        return current_sl >= desired - tolerance
    return current_sl <= desired + tolerance


def _level_hit(direction: str, level: float, tick: Tick) -> bool:
    if not level:
        return False
    if direction == "BUY":
        return tick.bid >= level
    return tick.ask <= level


def _can_manage(settings, account, runtime, label: str) -> bool:
    try:
        Safety().assert_can_trade(settings, account, "manager", ignore_dry_run=True)
    except SafetyError as exc:
        runtime.logs.warn(f"{label} bloqueado: {exc}")
        return False
    return True
