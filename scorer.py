from dataclasses import dataclass


@dataclass
class ScoreResult:
    score: float
    score_reason: str


# Scoring benchmarks
_PAYMENT_GREAT = 600.0   # <=600 is great, higher is worse
_PAYMENT_MAX = 1200.0    # ceiling for normalization

_MONTHS_LOW = 4          # too short
_MONTHS_SWEET_LOW = 8
_MONTHS_SWEET_HIGH = 18
_MONTHS_HIGH = 36        # ceiling for normalization

_KM_MIN = 10_000         # floor
_KM_GREAT = 20_000       # 20k+/yr is excellent

_TAKEOVER_MAX = 3_000.0  # ceiling for normalization


def _payment_score(payment: float | None) -> float:
    if payment is None:
        return 0.5  # neutral when unknown
    if payment <= _PAYMENT_GREAT:
        return 1.0
    if payment >= _PAYMENT_MAX:
        return 0.0
    return 1.0 - (payment - _PAYMENT_GREAT) / (_PAYMENT_MAX - _PAYMENT_GREAT)


def _months_score(months: int | None) -> float:
    if months is None:
        return 0.5
    if _MONTHS_SWEET_LOW <= months <= _MONTHS_SWEET_HIGH:
        return 1.0
    if months < _MONTHS_SWEET_LOW:
        # drops off sharply below sweet spot
        return max(0.0, months / _MONTHS_SWEET_LOW)
    # above sweet spot — decreasing but still usable
    return max(0.0, 1.0 - (months - _MONTHS_SWEET_HIGH) / (_MONTHS_HIGH - _MONTHS_SWEET_HIGH))


def _km_score(km: int | None) -> float:
    if km is None:
        return 0.5
    if km >= _KM_GREAT:
        return 1.0
    if km <= _KM_MIN:
        return 0.0
    return (km - _KM_MIN) / (_KM_GREAT - _KM_MIN)


def _takeover_score(cash: float | None) -> float:
    if not cash:
        return 0.0
    return min(1.0, cash / _TAKEOVER_MAX)


def _km_used_score(km_used: int | None, km_allowance: int | None) -> float:
    if km_used is None or not km_allowance:
        return 0.5
    ratio = km_used / km_allowance
    # Lower ratio = more runway = better
    return max(0.0, 1.0 - ratio)


def score_listing(listing: dict) -> ScoreResult:
    # Prefer effective_payment (incentive-adjusted) over raw monthly_payment for scoring
    payment = listing.get("effective_payment") or listing.get("monthly_payment")
    months = listing.get("months_remaining")
    km_allowance = listing.get("km_allowance")
    km_used = listing.get("km_used")
    takeover_cash = listing.get("takeover_cash") or 0.0
    using_effective = listing.get("effective_payment") is not None

    s_payment = _payment_score(payment)
    s_months = _months_score(months)
    s_km = _km_score(km_allowance)
    s_takeover = _takeover_score(takeover_cash)
    s_km_used = _km_used_score(km_used, km_allowance)

    weighted = (
        s_payment   * 0.35 +
        s_months    * 0.20 +
        s_km        * 0.15 +
        s_takeover  * 0.20 +
        s_km_used   * 0.10
    )
    score = round(weighted * 10, 1)

    # Build a human-readable reason
    reasons = []
    if payment is not None:
        label = "Low payment" if payment <= _PAYMENT_GREAT else "High payment"
        suffix = " eff." if using_effective else ""
        reasons.append(f"{label} (${payment:,.0f}/mo{suffix})")
    if months is not None:
        if _MONTHS_SWEET_LOW <= months <= _MONTHS_SWEET_HIGH:
            reasons.append(f"Good term ({months} mo remaining)")
        elif months < _MONTHS_SWEET_LOW:
            reasons.append(f"Short term ({months} mo remaining)")
        else:
            reasons.append(f"Long term ({months} mo remaining)")
    if takeover_cash > 0:
        reasons.append(f"${takeover_cash:,.0f} incentive cash")
    if km_allowance and km_allowance >= _KM_GREAT:
        reasons.append(f"High km allowance ({km_allowance:,}/yr)")

    score_reason = " + ".join(reasons) if reasons else "Insufficient data for detailed reason"

    return ScoreResult(score=score, score_reason=score_reason)
