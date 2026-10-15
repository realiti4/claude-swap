"""Optional paid overflow policy; ordinary rotation remains unchanged otherwise."""
import math


def choose(config, current, eligible, emails, usage, headroom):
    """None: ordinary policy; []: keep paid account; [slot]: switch.

    Fail closed on unknown quota or missing/disabled paid capacity. Only
    enter paid overflow once all eligible included windows are exhausted.
    """
    target = next((n for n in eligible if emails.get(n) == config.get('accountEmail')), None)
    if not target or headroom.get(current) is None or headroom[current] > 0:
        return None
    included = [n for n in eligible if n != current and headroom.get(n) is not None and headroom[n] > 0]
    if included:
        # Escape a hard limit, even when the ordinary proactive hysteresis
        # would reject the last few percent of included quota.
        return sorted(included, key=lambda n: (-headroom[n], n))
    if any(headroom.get(n) is None for n in eligible):
        return None
    value = usage.get(target)
    overflow = value.get('spend', {}) if isinstance(value, dict) else {}
    if overflow.get('enabled') is not True:
        return None
    used, limit, cap = overflow.get('used'), overflow.get('limit'), config.get('maxMonthlyUsd')
    if overflow.get('currency') != 'USD':
        return None
    if not all(isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x) for x in (used, limit, cap)):
        return None
    if used < 0 or limit <= 0 or cap <= 0 or limit > cap or used >= min(limit, cap):
        return None
    return [] if current == target else [target]
