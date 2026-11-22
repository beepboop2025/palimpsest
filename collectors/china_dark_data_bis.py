"""BIS SDMX China shadow-credit, property, DSR, REER, offshore banking & bond collector.

Pulls eight official, keyless, open Bank for International Settlements (BIS)
SDMX REST dataflows for China (CN) and Hong Kong (HK):

1. ``WS_TC``            Total credit to the non-financial sector (Households,
                        Non-financial corporations, General government, Private
                        non-financial sector — captures LGFV & shadow credit
                        outside headline bank loans).
2. ``WS_CREDIT_GAP``    Credit-to-GDP gap (systemic banking stress indicator).
3. ``WS_DSR``           Debt service ratio for China's private non-financial sector.
4. ``WS_SPP``           Selected residential property prices (real & nominal).
5. ``WS_EER``           Broad 64-economy Real & Nominal Effective Exchange Rates.
6. ``WS_CBPOL``         Central bank policy rate (1Y LPR / historical 1Y benchmark).
7. ``WS_LBS_D_PUB``     Locational Banking Statistics (international cross-border
                        bank claims & liabilities on China & Hong Kong).
8. ``WS_DEBT_SEC2_PUB`` International debt securities (offshore USD/EUR bond
                        issuance by Chinese nationals & residents).

Rights & redistribution:
  BIS publishes these datasets openly under BIS Terms & Conditions permitting
  free use and redistribution with attribution and without charging a fee for
  redistribution of the raw BIS statistics themselves. Every record preserves
  exact BIS dataflow key, raw SHA-256, observation clock, and attribution.

Standard-library only; all egress routes through ``core.safe_fetch``.
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import io
import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Callable, Sequence
from urllib.parse import urlsplit

from core.econ_observation import EconomicObservation
from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

BIS_HOST = "stats.bis.org"
BIS_BASE = "https://stats.bis.org/api/v2/data/dataflow/BIS"
BIS_TERMS_URL = "https://www.bis.org/terms_conditions.htm"
USER_AGENT = "palimpsest.info observatory (BIS official SDMX ingest; desk@palimpsest.info)"
PARSER_VERSION = "bis-china-sdmx.v1"
MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BisSeriesSpec:
    """Declarative specification for one BIS SDMX China query."""

    series_id: str
    dataflow: str
    version: str
    key: str
    start_period: str
    frequency: str
    unit: str
    sector: str
    geography: str
    label: str
    domain: str

    @property
    def url(self) -> str:
        return (
            f"{BIS_BASE}/{self.dataflow}/{self.version}/{self.key}"
            f"?startPeriod={self.start_period}&format=csv"
        )


DEFAULT_BIS_CHINA_SERIES: tuple[BisSeriesSpec, ...] = (
    # Total credit to non-financial sectors (% of GDP & USD billions — captures shadow/LGFV credit)
    BisSeriesSpec(
        series_id="cn.bis.total_credit_private_nf_pct_gdp",
        dataflow="WS_TC",
        version="2.0",
        key="Q.CN.P.A.M.770.A",
        start_period="2000-Q1",
        frequency="Q",
        unit="% of GDP",
        sector="private_non_financial",
        geography="CN",
        label="China total credit to private non-financial sector (% of GDP, all lenders)",
        domain="credit_shadow_debt",
    ),
    BisSeriesSpec(
        series_id="cn.bis.total_credit_nonfin_corp_pct_gdp",
        dataflow="WS_TC",
        version="2.0",
        key="Q.CN.N.A.M.770.A",
        start_period="2000-Q1",
        frequency="Q",
        unit="% of GDP",
        sector="non_financial_corporations",
        geography="CN",
        label="China total credit to non-financial corporations incl. LGFVs (% of GDP)",
        domain="credit_shadow_debt",
    ),
    BisSeriesSpec(
        series_id="cn.bis.total_credit_households_pct_gdp",
        dataflow="WS_TC",
        version="2.0",
        key="Q.CN.H.A.M.770.A",
        start_period="2000-Q1",
        frequency="Q",
        unit="% of GDP",
        sector="households",
        geography="CN",
        label="China total credit to households & NPISHs (% of GDP)",
        domain="credit_shadow_debt",
    ),
    BisSeriesSpec(
        series_id="cn.bis.total_credit_general_gov_pct_gdp",
        dataflow="WS_TC",
        version="2.0",
        key="Q.CN.G.A.N.770.A",
        start_period="2000-Q1",
        frequency="Q",
        unit="% of GDP",
        sector="general_government",
        geography="CN",
        label="China total credit to general government (% of GDP, nominal value)",
        domain="fiscal_sovereign",
    ),
    BisSeriesSpec(
        series_id="cn.bis.total_credit_all_nf_pct_gdp",
        dataflow="WS_TC",
        version="2.0",
        key="Q.CN.C.A.M.770.A",
        start_period="2000-Q1",
        frequency="Q",
        unit="% of GDP",
        sector="all_non_financial",
        geography="CN",
        label="China total credit to all non-financial sectors (% of GDP)",
        domain="credit_shadow_debt",
    ),
    BisSeriesSpec(
        series_id="cn.bis.total_credit_private_nf_usd_bn",
        dataflow="WS_TC",
        version="2.0",
        key="Q.CN.P.A.M.USD.A",
        start_period="2000-Q1",
        frequency="Q",
        unit="USD billions",
        sector="private_non_financial",
        geography="CN",
        label="China total credit to private non-financial sector (USD billions)",
        domain="credit_shadow_debt",
    ),
    BisSeriesSpec(
        series_id="cn.bis.bank_credit_private_nf_pct_gdp",
        dataflow="WS_TC",
        version="2.0",
        key="Q.CN.P.B.M.770.A",
        start_period="2000-Q1",
        frequency="Q",
        unit="% of GDP",
        sector="private_non_financial",
        geography="CN",
        label="China bank-only credit to private non-financial sector (% of GDP; gap vs total = non-bank shadow credit)",
        domain="credit_shadow_debt",
    ),
    # Credit-to-GDP Gap & Debt Service Ratio
    BisSeriesSpec(
        series_id="cn.bis.credit_to_gdp_gap",
        dataflow="WS_CREDIT_GAP",
        version="1.0",
        key="Q.CN.P.A.C",
        start_period="2000-Q1",
        frequency="Q",
        unit="percentage points",
        sector="private_non_financial",
        geography="CN",
        label="China credit-to-GDP gap (actual minus long-term trend)",
        domain="systemic_risk",
    ),
    BisSeriesSpec(
        series_id="cn.bis.debt_service_ratio_private_nf",
        dataflow="WS_DSR",
        version="1.0",
        key="Q.CN.P",
        start_period="2000-Q1",
        frequency="Q",
        unit="% of income",
        sector="private_non_financial",
        geography="CN",
        label="China debt service ratio (private non-financial sector)",
        domain="systemic_risk",
    ),
    # Residential Property Prices (Real & Nominal)
    BisSeriesSpec(
        series_id="cn.bis.residential_property_real_index",
        dataflow="WS_SPP",
        version="1.0",
        key="Q.CN.R.628",
        start_period="2005-Q1",
        frequency="Q",
        unit="index (2010=100)",
        sector="property",
        geography="CN",
        label="China real residential property price index (BIS selected)",
        domain="property",
    ),
    BisSeriesSpec(
        series_id="cn.bis.residential_property_nominal_index",
        dataflow="WS_SPP",
        version="1.0",
        key="Q.CN.N.628",
        start_period="2005-Q1",
        frequency="Q",
        unit="index (2010=100)",
        sector="property",
        geography="CN",
        label="China nominal residential property price index (BIS selected)",
        domain="property",
    ),
    # Broad Effective Exchange Rates (Real & Nominal, 64 economies)
    BisSeriesSpec(
        series_id="cn.bis.reer_broad_monthly",
        dataflow="WS_EER",
        version="1.0",
        key="M.R.B.CN",
        start_period="2000-01",
        frequency="M",
        unit="index (2020=100)",
        sector="fx_external",
        geography="CN",
        label="China real effective exchange rate (broad 64-economy basket)",
        domain="fx_external",
    ),
    BisSeriesSpec(
        series_id="cn.bis.neer_broad_monthly",
        dataflow="WS_EER",
        version="1.0",
        key="M.N.B.CN",
        start_period="2000-01",
        frequency="M",
        unit="index (2020=100)",
        sector="fx_external",
        geography="CN",
        label="China nominal effective exchange rate (broad 64-economy basket)",
        domain="fx_external",
    ),
    # Central Bank Policy Rate (LPR 1Y)
    BisSeriesSpec(
        series_id="cn.bis.cb_policy_rate_monthly",
        dataflow="WS_CBPOL",
        version="1.0",
        key="M.CN",
        start_period="2000-01",
        frequency="M",
        unit="%",
        sector="monetary_policy",
        geography="CN",
        label="China central bank policy rate (1Y LPR since Aug 2019)",
        domain="monetary_policy",
    ),
    # Cross-border International Bank Claims & Liabilities (Locational Banking)
    BisSeriesSpec(
        series_id="cn.bis.lbs_crossborder_claims_usd_mn",
        dataflow="WS_LBS_D_PUB",
        version="1.0",
        key="Q.S.C.A.TO1.A.5J.A.5A.A.CN.N",
        start_period="2005-Q1",
        frequency="Q",
        unit="USD millions",
        sector="cross_border_banking",
        geography="CN",
        label="BIS reporting banks cross-border claims on China (all instruments, USD millions)",
        domain="offshore_banking",
    ),
    BisSeriesSpec(
        series_id="cn.bis.lbs_crossborder_liabilities_usd_mn",
        dataflow="WS_LBS_D_PUB",
        version="1.0",
        key="Q.S.L.A.TO1.A.5J.A.5A.A.CN.N",
        start_period="2005-Q1",
        frequency="Q",
        unit="USD millions",
        sector="cross_border_banking",
        geography="CN",
        label="BIS reporting banks cross-border liabilities to China (deposits/funding, USD millions)",
        domain="offshore_banking",
    ),
    BisSeriesSpec(
        series_id="hk.bis.lbs_crossborder_claims_usd_mn",
        dataflow="WS_LBS_D_PUB",
        version="1.0",
        key="Q.S.C.A.TO1.A.5J.A.5A.A.HK.N",
        start_period="2005-Q1",
        frequency="Q",
        unit="USD millions",
        sector="cross_border_banking",
        geography="HK",
        label="BIS reporting banks cross-border claims on Hong Kong (offshore China gateway)",
        domain="offshore_banking",
    ),
    # Offshore International Debt Securities (USD/EUR bonds issued by Chinese borrowers)
    BisSeriesSpec(
        series_id="cn.bis.intl_debt_sec_residence_usd_mn",
        dataflow="WS_DEBT_SEC2_PUB",
        version="1.0",
        key="Q.CN.3P.1.1.C.A.A.TO1.A.A.A.A.A.I",
        start_period="2005-Q1",
        frequency="Q",
        unit="USD millions",
        sector="offshore_bonds",
        geography="CN",
        label="International debt securities outstanding by China residence (all issuers)",
        domain="offshore_bonds",
    ),
    BisSeriesSpec(
        series_id="cn.bis.intl_debt_sec_nationality_usd_mn",
        dataflow="WS_DEBT_SEC2_PUB",
        version="1.0",
        key="Q.3P.CN.1.1.C.A.A.TO1.A.A.A.A.A.I",
        start_period="2005-Q1",
        frequency="Q",
        unit="USD millions",
        sector="offshore_bonds",
        geography="CN",
        label="International debt securities outstanding by China nationality (captures Cayman/BVI/HK offshore SPVs & LGFVs!)",
        domain="offshore_bonds",
    ),
)


def _validate_bis_url(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != BIS_HOST
        or parts.port not in (None, 443)
        or not parts.path.startswith("/api/v2/data/dataflow/BIS/")
    ):
        raise FetchError(f"BIS collector refused unreviewed URL: {url}")


def _parse_bis_period_bounds(period: str) -> tuple[date, date]:
    """Convert BIS TIME_PERIOD ('YYYY-Qn', 'YYYY-MM', 'YYYY-MM-DD', 'YYYY') to date bounds."""
    p = period.strip()
    if "-Q" in p and len(p) == 7:
        year = int(p[:4])
        quarter = int(p[6])
        if quarter not in (1, 2, 3, 4):
            raise ValueError(f"Invalid BIS quarter: {p!r}")
        start_month = (quarter - 1) * 3 + 1
        end_month = quarter * 3
        end_day = calendar.monthrange(year, end_month)[1]
        return date(year, start_month, 1), date(year, end_month, end_day)
    if len(p) == 7 and p[4] == "-":
        year = int(p[:4])
        month = int(p[5:7])
        end_day = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, end_day)
    if len(p) == 10 and p[4] == "-" and p[7] == "-":
        d = date.fromisoformat(p)
        return d, d
    if len(p) == 4 and p.isdigit():
        year = int(p)
        return date(year, 1, 1), date(year, 12, 31)
    raise ValueError(f"Unsupported BIS TIME_PERIOD format: {p!r}")


def _period_label(obs: EconomicObservation) -> str:
    if obs.frequency == "Q":
        q = (obs.period_start.month - 1) // 3 + 1
        return f"{obs.period_start.year:04d}-Q{q}"
    if obs.frequency == "M":
        return f"{obs.period_start.year:04d}-{obs.period_start.month:02d}"
    if obs.frequency == "A":
        return f"{obs.period_start.year:04d}"
    return obs.period_start.isoformat()


def parse_bis_sdmx_csv(
    raw: bytes,
    spec: BisSeriesSpec,
    *,
    collected_at: str,
    evidence_url: str,
) -> list[EconomicObservation]:
    """Parse one BIS SDMX CSV payload into validated ``EconomicObservation`` rows."""
    raw_sha = hashlib.sha256(raw).hexdigest()
    collected_dt = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    text = raw.decode("utf-8-sig", errors="strict")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "TIME_PERIOD" not in reader.fieldnames or "OBS_VALUE" not in reader.fieldnames:
        raise ValueError(f"BIS CSV for {spec.series_id} lacks TIME_PERIOD/OBS_VALUE columns")

    seen_periods: dict[str, EconomicObservation] = {}
    for row in reader:
        period = (row.get("TIME_PERIOD") or "").strip()
        val_raw = (row.get("OBS_VALUE") or "").strip()
        if not period or not val_raw:
            continue
        try:
            value = float(val_raw)
            period_start, period_end = _parse_bis_period_bounds(period)
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        # When a wildcard key matches multiple rows, take the first canonical match per period.
        if period in seen_periods:
            continue
        obs = EconomicObservation(
            series_id=spec.series_id,
            value=value,
            unit=spec.unit,
            frequency=spec.frequency,
            period_start=period_start,
            period_end=period_end,
            released_at=collected_dt,
            collected_at=collected_dt,
            source_id="bis_china_credit",
            evidence_url=evidence_url,
            revision=0,
            status="observed",
            geography=spec.geography,
            sector=spec.sector,
            firm_size="all",
            ownership="all",
            quality=0.98,
            raw_sha256=raw_sha,
            metadata={
                "family": spec.domain,
                "source_series_id": f"{spec.dataflow}/{spec.key}",
                "parser_version": PARSER_VERSION,
                "release_time_semantics": "collected_at_fallback_no_upstream_release_timestamp",
            },
        )
        seen_periods[period] = obs
    return [seen_periods[p] for p in sorted(seen_periods)]


def compute_shadow_credit_spread(
    observations: Sequence[EconomicObservation],
) -> list[dict[str, object]]:
    """Derive China's non-bank shadow-credit ratio (% of GDP) from BIS total minus bank credit."""
    total_by_p: dict[str, float] = {}
    bank_by_p: dict[str, float] = {}
    for obs in observations:
        plabel = _period_label(obs)
        if obs.series_id == "cn.bis.total_credit_private_nf_pct_gdp":
            total_by_p[plabel] = obs.value
        elif obs.series_id == "cn.bis.bank_credit_private_nf_pct_gdp":
            bank_by_p[plabel] = obs.value
    derived: list[dict[str, object]] = []
    for period in sorted(set(total_by_p) & set(bank_by_p)):
        total = total_by_p[period]
        bank = bank_by_p[period]
        non_bank = round(total - bank, 3)
        derived.append(
            {
                "period": period,
                "total_private_credit_pct_gdp": total,
                "bank_private_credit_pct_gdp": bank,
                "non_bank_shadow_credit_pct_gdp": non_bank,
                "non_bank_share_pct": round((non_bank / total) * 100.0, 2) if total > 0 else None,
            }
        )
    return derived


def compute_offshore_spv_bond_gap(
    observations: Sequence[EconomicObservation],
) -> list[dict[str, object]]:
    """Derive Cayman/BVI/HK offshore Chinese issuer bond stock = nationality minus residence."""
    nat: dict[str, float] = {}
    res: dict[str, float] = {}
    for obs in observations:
        plabel = _period_label(obs)
        if obs.series_id == "cn.bis.intl_debt_sec_nationality_usd_mn":
            nat[plabel] = obs.value
        elif obs.series_id == "cn.bis.intl_debt_sec_residence_usd_mn":
            res[plabel] = obs.value
    out: list[dict[str, object]] = []
    for period in sorted(set(nat) & set(res)):
        gap = round(nat[period] - res[period], 2)
        out.append(
            {
                "period": period,
                "nationality_usd_mn": nat[period],
                "residence_usd_mn": res[period],
                "offshore_vehicle_gap_usd_mn": gap,
                "offshore_vehicle_share_pct": round((gap / nat[period]) * 100.0, 2) if nat[period] > 0 else None,
            }
        )
    return out


def collect_bis_china(
    *,
    specs: Sequence[BisSeriesSpec] = DEFAULT_BIS_CHINA_SERIES,
    timeout: float = 30.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
    now: str | None = None,
) -> dict[str, object]:
    """Collect all configured BIS China & HK series with explicit per-series provenance."""
    collected_at = now or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    all_observations: list[EconomicObservation] = []
    series_status: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []

    for spec in specs:
        url = spec.url
        try:
            _validate_bis_url(url)
            raw = fetcher(
                url,
                timeout=timeout,
                max_bytes=MAX_BYTES,
                max_redirects=2,
                headers={"Accept": "text/csv", "User-Agent": USER_AGENT},
                url_policy=_validate_bis_url,
            )
            obs = parse_bis_sdmx_csv(raw, spec, collected_at=collected_at, evidence_url=url)
            all_observations.extend(obs)
            series_status.append(
                {
                    "series_id": spec.series_id,
                    "label": spec.label,
                    "domain": spec.domain,
                    "geography": spec.geography,
                    "unit": spec.unit,
                    "status": "ok" if obs else "empty",
                    "observation_count": len(obs),
                    "first_period": _period_label(obs[0]) if obs else None,
                    "latest_period": _period_label(obs[-1]) if obs else None,
                    "latest_value": obs[-1].value if obs else None,
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "evidence_url": url,
                }
            )
        except Exception as exc:  # noqa: BLE001 — abstain per series, never fail whole board
            log.warning("BIS series %s failed: %s", spec.series_id, exc)
            failures.append({"series_id": spec.series_id, "error": f"{type(exc).__name__}: {exc}"})
            series_status.append(
                {
                    "series_id": spec.series_id,
                    "label": spec.label,
                    "domain": spec.domain,
                    "geography": spec.geography,
                    "unit": spec.unit,
                    "status": "unavailable",
                    "observation_count": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                    "evidence_url": url,
                }
            )

    shadow_credit = compute_shadow_credit_spread(all_observations)
    offshore_spv_bonds = compute_offshore_spv_bond_gap(all_observations)
    ok_count = sum(1 for s in series_status if s["status"] == "ok")

    return {
        "schema": "palimpsest.bis-china-credit.v1",
        "generated_at": collected_at,
        "status": "ok" if ok_count == len(specs) else ("partial" if ok_count > 0 else "unavailable"),
        "source": {
            "source_id": "bis_china_credit",
            "publisher": "Bank for International Settlements (BIS)",
            "api_base": BIS_BASE,
            "terms_url": BIS_TERMS_URL,
            "attribution": "Source: Bank for International Settlements (BIS) SDMX API",
            "redistribution_note": "Free public BIS statistics with attribution; no additional charge for raw BIS statistics redistribution.",
        },
        "summary": {
            "series_configured": len(specs),
            "series_collected": ok_count,
            "series_failed": len(failures),
            "total_observations": len(all_observations),
            "latest_shadow_credit": shadow_credit[-1] if shadow_credit else None,
            "latest_offshore_spv_bond_gap": offshore_spv_bonds[-1] if offshore_spv_bonds else None,
        },
        "series_status": series_status,
        "derived": {
            "non_bank_shadow_credit_series": shadow_credit[-24:],
            "offshore_spv_bond_gap_series": offshore_spv_bonds[-24:],
        },
        "failures": failures,
        "observations": [o.to_dict() for o in all_observations],
    }
