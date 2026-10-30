"""Read-only bounds at known market levels; never an order or a profit forecast."""
from __future__ import annotations
import math
import time
from decimal import ROUND_DOWN, ROUND_UP
from .risk import D, cfd_fx
from .structure import structural_levels, validate_structural_plan


def market_feasibility(data, quote, rules, settings, short=False, setup=None):
    """Best net RR at the current quote, conservative ticks and known obstacles.

    Uses the tightest structurally valid stop, including the 0.35 ATR noise
    floor, and the furthest target before the nearest known obstacle. Position
    sizing, portfolio capacity, setup admission and future price paths are not
    assessed. This function neither repairs a proposal nor authorizes a trade.
    """
    stamp = getattr(quote, 'time_ms', None)
    result = dict(status='unknown', reason='Données de faisabilité absentes ou invalides.',
                  max_net_rr=None, reference_stop=None, reference_target=None,
                  quote_time_ms=stamp if type(stamp) is int else None, costs={})

    def finish(status, reason):
        result.update(status=status, reason=reason)
        return result

    try:
        now = int(time.time() * 1000)
        symbol = quote.symbol
        cfd = symbol.startswith('CAP:')
        if quote.symbol != rules.symbol or short and not cfd:
            return finish('unknown', 'Cotation et contrat incompatibles avec le sens étudié.')
        if stamp is None or not 0 <= now - stamp <= 15000:
            return finish('unknown', 'Cotation trop ancienne ou future : borne non vérifiable.')
        prices = (quote.bid, quote.ask, rules.tick_size)
        if any(not math.isfinite(v) or v <= 0 for v in prices) or quote.bid > quote.ask:
            return finish('unknown', 'Prix, spread ou précision du contrat invalides.')
        if cfd and (quote.market_status != 'TRADEABLE' or quote.currency != 'USD'
                    or rules.currency != 'USD' or rules.multiplier != 1):
            return finish('unknown', 'Marché CFD fermé ou contrat non pris en charge.')
        slip = settings.slippage_bps / 10000
        fee = getattr(settings, 'cfd_fee_rate', 0.) if cfd else settings.fee_rate
        minimum_rr = settings.min_rr
        if (any(not math.isfinite(v) for v in (slip, fee, minimum_rr))
                or not 0 <= slip <= .01 or not 0 <= fee <= .01 or minimum_rr < 2):
            return finish('unknown', 'Coûts ou ratio minimal non vérifiables.')
        fx_bid, fx_ask = cfd_fx(quote) if cfd else (1., 1.)
        financing, source = 0., 'not_applicable'
        if cfd:
            financing = getattr(rules, 'financing_short_daily' if short else 'financing_long_daily', None)
            if financing is None:
                financing = max(getattr(settings, 'cfd_financing_daily', .001),
                                getattr(rules, 'financing_daily', 0.))
                source = 'configured_daily_estimate'
            else:
                source = getattr(rules, 'financing_source', None) or 'provider_directional_daily_estimate'
            if not isinstance(financing, (int, float)) or not math.isfinite(financing) or not 0 <= financing <= .05:
                return finish('unknown', 'Financement du sens étudié non vérifiable.')
        sign = -1 if short else 1
        reference = quote.bid if short else quote.ask
        entry = reference * (1 - slip if short else 1 + slip)
        if not math.isfinite(entry) or entry <= 0:
            return finish('unknown', 'Prix d’entrée simulé non vérifiable.')
        result['costs'] = dict(entry_fill=entry, fee_rate=fee, slippage_bps=settings.slippage_bps,
                               financing_daily=financing, financing_source=source,
                               financing_reserve_days=1 if cfd else 0,
                               conversion_bid=fx_bid, conversion_ask=fx_ask)
        market_reference=(quote.ask if short else quote.bid) if setup=='pullback' else None
        levels = structural_levels(data, entry, 'SHORT' if short else 'LONG', now_ms=now, setup=setup, reference_price=market_reference)
        anchor, obstacle, atr = levels['stop_reference'], levels['target_obstacle'], levels['atr']
        if anchor is None:
            return finish('missing_stop', 'Aucun pivot connu pour justifier un stop structurel.')
        reference_fields=('price','timeframe','source','pivot_at','confirmed_at','broken_level','observed_extreme','zone_start_at','contact_at')
        result['stop_reference']={key:anchor[key] for key in reference_fields if key in anchor}
        if obstacle is not None:
            result['target_obstacle']={key:obstacle[key] for key in reference_fields if key in obstacle}
        if setup=='pullback':
            result['target_basis']=levels.get('target_basis')
            result['minor_obstacles']=[{key:r[key] for key in reference_fields if key in r}
                for r in levels.get('minor_obstacles',[])[:8]]
        rounding = ROUND_UP if short else ROUND_DOWN

        tick = D(rules.tick_size)

        def rounded(price):
            return (price / tick).to_integral_value(rounding=rounding) * tick

        # Both constraints refer to absolute prices, not a desired reward/risk.
        # Compute the margins before converting to a binary float. For example,
        # 98.3 - .15 is 98.14999999999999 as a float: flooring that value would
        # incorrectly spend an extra tick and can turn a valid 2R plan into WAIT.
        anchor_price, entry_price, atr_price = D(anchor['price']), D(entry), D(atr)
        stop_buffer, noise_floor = D('.15') * atr_price, D('.35') * atr_price
        stop = (max(anchor_price + stop_buffer, entry_price + noise_floor) if short
                else min(anchor_price - stop_buffer, entry_price - noise_floor))
        # A stop must allow an unbreached invalidation on the executable exit side.
        stop = rounded(stop)
        if (stop <= D(quote.ask) if short else stop >= D(quote.bid)):
            stop = rounded(D(quote.ask if short else quote.bid))
            if (stop <= D(quote.ask) if short else stop >= D(quote.bid)):
                stop -= sign * tick
        # RiskEngine enforces this same comparison on executable float prices.
        # Retain a whole conservative tick if its float boundary needs one.
        if sign * (entry - float(stop)) < .35 * atr:
            stop -= sign * tick
        stop = float(stop)
        if not math.isfinite(stop):
            return finish('unknown', 'Stop de référence non vérifiable.')
        result['reference_stop'] = stop
        if obstacle is None:
            return finish('missing_target', 'Aucun obstacle connu pour borner un objectif de marché.')
        target = float(rounded(D(obstacle['price']) - sign * D('.1') * atr_price))
        if not math.isfinite(target):
            return finish('unknown', 'Objectif de référence non vérifiable.')
        result['reference_target'] = target
        if stop <= 0 or target <= 0 or sign * (entry - stop) <= 0 or sign * (target - entry) <= 0:
            return finish('invalid_levels', 'Marge insuffisante avant le premier obstacle connu après arrondi.')
        try:
            validate_structural_plan(data, entry, stop, target, short=short, now_ms=now, setup=setup, reference_price=market_reference)
        except ValueError:
            return finish('invalid_levels', 'Aucun couple de niveaux valide avec les pivots et la précision du contrat.')
        stop_fill = stop * (1 + slip if short else 1 - slip)
        target_fill = target * (1 + slip if short else 1 - slip)
        entry_fee = entry * fee * fx_ask
        carry = entry * financing * fx_ask
        if cfd:
            loss = sign * (entry - stop_fill) * fx_ask + entry_fee + stop_fill * fee * fx_ask + carry
            reward = sign * (target_fill - entry) * fx_bid - entry_fee - target_fill * fee * fx_ask - carry
        else:
            loss = entry * (1 + fee) - stop_fill * (1 - fee)
            reward = target_fill * (1 - fee) - entry * (1 + fee)
        if not math.isfinite(loss) or not math.isfinite(reward) or loss <= 0:
            return finish('unknown', 'Perte ou récompense nette non vérifiable.')
        rr = reward / loss
        if not math.isfinite(rr):
            return finish('unknown', 'Ratio net non vérifiable.')
        result['max_net_rr'] = rr
        result['costs'].update(entry_fee_per_unit=entry_fee, financing_reserve_per_unit=carry,
                               loss_per_unit=loss, reward_per_unit=reward)
        if reward <= 0 or rr + 1e-10 < minimum_rr:
            return finish('insufficient_rr', 'Même la borne optimiste aux niveaux connus ne couvre pas le ratio net requis.')
        return finish('feasible', 'Borne compatible avec le ratio net requis ; aucun ordre validé et aucun gain garanti.')
    except (AttributeError, KeyError, TypeError, ValueError, ArithmeticError):
        return finish('unknown', 'Structure ou coûts non vérifiables : aucune borne exploitable.')
