"""
Portering af afgiftsberegner-mmkcyber's klient-side JS-beregninger (index.html <script>) til Python.
Skal holdes i sync med RATES / computeAfgift / solveExAfgiftValue / computeForholdsmaessig /
deriveMarketValue / deriveResidualValue / linearRegressionKmValue / deriveResidualValueCombined
i https://github.com/mmk-cyber/afgiftsberegner-mmkcyber (privat repo) index.html.

2026-satser fra Skatteministeriet — verificér periodisk mod motorst.dk.
"""
from dataclasses import dataclass, field
from typing import Optional

RATES = {
    "band1": 76400,
    "band2": 237400,
    "co2Low": 107,
    "co2Mid": 30,
    "co2MidRate": 587,   # ikke brugt direkte (kun midAmt*rate beregnes eksplicit nedenfor, matcher JS)
    "co2LowRate": 294,
    "co2HighRate": 1115,
    "standardDeduction": 25500,
    "indfasning": {"konventionel": 1.0, "phev": 0.68, "ev": 0.40},
    "extraDeduction": {"konventionel": 0, "phev": 43000, "ev": 161300},
}


def value_tax(value: float) -> dict:
    b1amt = min(value, RATES["band1"])
    b2amt = max(0.0, min(value, RATES["band2"]) - RATES["band1"])
    b3amt = max(0.0, value - RATES["band2"])
    b1tax = b1amt * 0.25
    b2tax = b2amt * 0.85
    b3tax = b3amt * 1.5
    return {
        "total": b1tax + b2tax + b3tax,
        "b1amt": b1amt, "b2amt": b2amt, "b3amt": b3amt,
        "b1tax": b1tax, "b2tax": b2tax, "b3tax": b3tax,
    }


def co2_surcharge(co2: float, fuel_type: str) -> dict:
    if fuel_type == "ev" or co2 <= RATES["co2Mid"]:
        return {"total": 0.0, "midAmt": 0.0, "midTax": 0.0, "highAmt": 0.0, "highTax": 0.0}
    mid_amt = max(0.0, min(co2, RATES["co2Low"]) - RATES["co2Mid"])
    high_amt = max(0.0, co2 - RATES["co2Low"])
    mid_tax = mid_amt * RATES["co2MidRate"]
    high_tax = high_amt * RATES["co2HighRate"]
    return {"total": mid_tax + high_tax, "midAmt": mid_amt, "midTax": mid_tax, "highAmt": high_amt, "highTax": high_tax}


def compute_afgift(value: float, co2: float, fuel_type: str) -> dict:
    v_tax = value_tax(value)
    c = co2_surcharge(co2, fuel_type)
    grundafgift = v_tax["total"] + c["total"]
    after_std = max(0.0, grundafgift - RATES["standardDeduction"])
    indf_pct = RATES["indfasning"][fuel_type]
    after_indf = after_std * indf_pct
    extra_ded = RATES["extraDeduction"][fuel_type]
    final = max(0.0, after_indf - extra_ded)
    return {
        "valueTax": v_tax["total"], "valueTaxBands": v_tax,
        "co2Surcharge": c["total"], "co2Bands": c,
        "grundafgift": grundafgift,
        "standardDeduction": RATES["standardDeduction"], "afterStandardDeduction": after_std,
        "indfasningPct": indf_pct, "afterIndfasning": after_indf,
        "extraDeduction": extra_ded,
        "final": final,
    }


def solve_ex_afgift_value(total_incl_afgift: float, co2: float, fuel_type: str) -> float:
    lo, hi = 0.0, total_incl_afgift
    for _ in range(60):
        mid = (lo + hi) / 2
        r = compute_afgift(mid, co2, fuel_type)
        total = mid + r["final"]
        if total > total_incl_afgift:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def compute_forholdsmaessig(ex_afgift_value: float, start_age_months: float, lease_months: float,
                             rente_tillaeg_pct: float, co2: float, fuel_type: str) -> dict:
    r = compute_afgift(ex_afgift_value, co2, fuel_type)
    full_tax = r["final"]
    alder = start_age_months
    periode = lease_months
    if alder < 3:
        k15 = periode if (periode + alder < 4) else (3 - alder)
    else:
        k15 = 0
    if alder > 36:
        k16 = 0
    else:
        k16 = (36 - alder - k15) if (periode > (36 - alder)) else (periode - k15)
    k17 = periode - k15 - k16
    vaerditab_pct = k15 * 0.02 + k16 * 0.01 + k17 * 0.005
    vaerditab_afgift = full_tax * vaerditab_pct
    restafgift = full_tax - vaerditab_afgift
    rente_af_restafgift = restafgift * (rente_tillaeg_pct / 100) * (periode / 12)
    total_paid = vaerditab_afgift + rente_af_restafgift
    return {
        "avgMonthly": total_paid / periode if periode else 0.0,
        "totalTaxPaid": total_paid,
        "restafgiftVedSlut": restafgift,
        "fullTax": full_tax,
        "vaerditabPct": vaerditab_pct,
    }


def annuity_with_balloon(p: float, b: float, annual_rate_pct: float, n: float) -> float:
    i = annual_rate_pct / 100 / 12
    if i == 0:
        return (p - b) / n
    return (p - b / (1 + i) ** n) * i / (1 - (1 + i) ** (-n))


@dataclass
class Comparison:
    kilde: str
    beskrivelse: str
    pris: float
    dato: str = ""
    km: float = 0
    link: str = ""
    vaerdi_u_afgift: float = 0
    regafgift: float = 0
    opkraevet: float = 0
    andel_pct: float = 0


def derive_market_value(comparisons: list[Comparison]) -> dict:
    if not comparisons:
        return {"totalIncl": 0.0, "tier": "none", "usedCount": 0, "excludedNote": ""}
    used = sorted(comparisons, key=lambda c: c.pris)
    excluded_note = ""
    if len(used) < 3:
        tier = "thin"
    elif len(used) == 3:
        tier = "mild"
    else:
        tier = "ok"
        if len(used) >= 5:
            dropped = [used[0], used[-1]]
            used = used[1:-1]
            excluded_note = (
                f"Billigste ({round(dropped[0].pris):,} kr.) og dyreste ({round(dropped[1].pris):,} kr.) "
                f"er set bort fra, jf. Motorstyrelsens praksis."
            ).replace(",", ".")
    total_incl = sum(c.pris for c in used) / len(used)
    return {"totalIncl": total_incl, "tier": tier, "usedCount": len(used), "excludedNote": excluded_note}


def derive_residual_value(comparisons: list[Comparison], target_km: float) -> dict:
    with_value = [c for c in comparisons if c.vaerdi_u_afgift > 0]
    if not with_value:
        return {"value": 0.0, "source": None}
    best = min(with_value, key=lambda c: abs(c.km - target_km))
    return {"value": best.vaerdi_u_afgift, "source": best}


def linear_regression_km_value(comparisons: list[Comparison], target_km: float) -> Optional[float]:
    pts = [c for c in comparisons if c.vaerdi_u_afgift > 0 and c.km > 0]
    n = len(pts)
    if n < 2:
        return None
    sum_x = sum(c.km for c in pts)
    sum_y = sum(c.vaerdi_u_afgift for c in pts)
    sum_xy = sum(c.km * c.vaerdi_u_afgift for c in pts)
    sum_xx = sum(c.km * c.km for c in pts)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None
    b = (n * sum_xy - sum_x * sum_y) / denom
    a = (sum_y - b * sum_x) / n
    val = a + b * target_km
    return val if val > 0 else None


def derive_residual_value_combined(comparisons: list[Comparison], target_km: float, ex_afgift_value: float,
                                    age_months: float, lease_months: float, co2: float, fuel_type: str) -> dict:
    method_a_info = derive_residual_value(comparisons, target_km)
    method_a = method_a_info["value"] if method_a_info["value"] > 0 else None
    method_b = linear_regression_km_value(comparisons, target_km)
    method_c = None
    if ex_afgift_value > 0 and lease_months > 0:
        fm = compute_forholdsmaessig(ex_afgift_value, age_months, lease_months, 0, co2, fuel_type)
        method_c = ex_afgift_value * (1 - fm["vaerditabPct"])
    vals = [v for v in (method_a, method_b, method_c) if v is not None and v > 0]
    combined = sum(vals) / len(vals) if vals else 0.0
    return {
        "value": combined, "methodA": method_a, "methodB": method_b, "methodC": method_c,
        "source": method_a_info["source"], "count": len(vals),
    }


def full_calculation(comparisons: list[Comparison], co2: float, age_months: float, km_stand: float,
                      fuel_type: str, months: float, downpct: float, rate: float, rest_rente: float) -> dict:
    """Samlet beregning, svarer til renderAll() i index.html."""
    market = derive_market_value(comparisons)
    ex_afgift_value = solve_ex_afgift_value(market["totalIncl"], co2, fuel_type) if market["totalIncl"] > 0 else 0.0
    r = compute_afgift(ex_afgift_value, co2, fuel_type)
    kontant_total = ex_afgift_value + r["final"]

    future_km = km_stand + (km_stand / age_months) * months if age_months > 0 else km_stand
    residual_info = derive_residual_value_combined(comparisons, future_km, ex_afgift_value, age_months, months, co2, fuel_type)
    residual = residual_info["value"]

    udbetaling = ex_afgift_value * (downpct / 100)
    financed = ex_afgift_value - udbetaling
    residual_too_high = residual > financed
    car_monthly = 0.0 if residual_too_high else annuity_with_balloon(financed, residual, rate, months)
    fm = compute_forholdsmaessig(ex_afgift_value, age_months, months, rest_rente, co2, fuel_type)
    total_monthly = fm["avgMonthly"]

    value_loss_incl = max(0.0, ex_afgift_value - residual)
    value_loss_excl = value_loss_incl / 1.25
    value_loss_pct = round(value_loss_incl / ex_afgift_value * 100) if ex_afgift_value > 0 else 0

    return {
        "market": market,
        "exAfgiftValue": ex_afgift_value,
        "afgift": r,
        "kontantTotal": kontant_total,
        "residual": residual_info,
        "leasing": {
            "carMonthly": car_monthly,
            "afgiftMonthly": fm["avgMonthly"],
            "residualExcl": residual / 1.25,
            "residualIncl": residual,
            "valueLossExcl": value_loss_excl,
            "valueLossIncl": value_loss_incl,
            "valueLossPct": value_loss_pct,
            "totalMonthlyExcl": total_monthly,
            "totalMonthlyIncl": total_monthly * 1.25,
            "futureKm": future_km,
        },
    }
