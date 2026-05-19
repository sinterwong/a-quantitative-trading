"""Shared exception hierarchy used by data layer and factor pipeline.

Why this module exists
----------------------
Several modules used to swallow exceptions and return empty values
(``FundamentalDataManager.get_fundamentals`` returned an empty ``DataFrame``
on any error, ``backend.services.llm.cache`` cached zero-confidence fallbacks),
making it impossible for callers to tell "data legitimately missing" from
"network failed / API down". When the underlying issue resolves, the system
keeps reading the stale degraded value.

These dedicated exceptions let upstream callers handle the two cases
differently: a missing symbol is fine to skip, but a transient fetch failure
should be logged loudly and degrade the pipeline confidence, not silently
become a "score of 0".
"""

from __future__ import annotations


class QuantTradingError(Exception):
    """Base class for project-internal exceptions."""


class DataSourceError(QuantTradingError):
    """Raised when an upstream data fetch failed (network / API / parsing).

    Use this when the call itself errored. Do NOT use it for the "we asked
    cleanly and the provider said there is no data for this symbol" case —
    that's a legitimate empty result and should be conveyed by returning an
    empty DataFrame / None / equivalent sentinel.
    """


class FactorEvalError(QuantTradingError):
    """Raised when a factor's evaluate() blows up due to a real bug
    (NaN propagation, shape mismatch, divide-by-zero on real data, …).

    Distinct from :class:`DataSourceError`: a factor that legitimately has
    no data should return zeros, not raise.
    """


__all__ = ["QuantTradingError", "DataSourceError", "FactorEvalError"]
