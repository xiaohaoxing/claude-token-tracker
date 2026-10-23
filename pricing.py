#!/usr/bin/env python3
"""
Model pricing for Claude Token Tracker.

The price book lives in ``pricing.json`` next to this file (override with
``CLAUDE_TRACKER_PRICING``) so prices can be corrected without touching code:
every model is either flat-priced or split into peak / idle tiers, and a tier
may depend on the wall-clock time of the API call.

Design notes:
  * Prices are quoted in each model's own currency, exactly as the vendor
    publishes them (DeepSeek bills in CNY). Everything is converted to USD on
    the way out, because that is the unit stored in the database.
  * Peak windows are declared as data: timezone offset, weekdays, time ranges
    and the list of statutory holidays. DeepSeek's CN business-hours window is
    the first user of this.
  * A missing/broken price book must never break the Stop hook, so loading
    falls back to a built-in table and records the error for --doctor.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

PRICING_FILE = Path(os.environ.get(
    "CLAUDE_TRACKER_PRICING",
    Path(__file__).resolve().parent / "pricing.json",
))

TOKENS_PER_UNIT = 1_000_000.0

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


# ── Rates ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Rate:
    """Price per 1M tokens in the model's own currency."""

    input_miss: float
    input_hit: float
    cache_write: float
    output: float

    def cost(self, input_tokens: int, output_tokens: int,
             cache_write_tokens: int, cache_read_tokens: int) -> float:
        return (
            input_tokens * self.input_miss
            + output_tokens * self.output
            + cache_write_tokens * self.cache_write
            + cache_read_tokens * self.input_hit
        ) / TOKENS_PER_UNIT


# ── Peak windows ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PeakWindow:
    """A recurring high-price window, expressed in the vendor's local time."""

    key: str
    label: str
    tz: timezone
    weekdays: FrozenSet[int]
    ranges: Tuple[Tuple[time, time], ...]
    holidays: FrozenSet[date]

    def is_peak(self, when: datetime) -> bool:
        local = when.astimezone(self.tz)
        if local.isoweekday() not in self.weekdays:
            return False
        if local.date() in self.holidays:
            return False
        now = local.timetz().replace(tzinfo=None)
        for start, end in self.ranges:
            if start <= now < end:
                return True
        return False


# ── Models ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ModelPrice:
    model_id: str
    provider: str
    patterns: Tuple[str, ...]
    currency: str
    flat: Optional[Rate]
    peak: Optional[Rate]
    idle: Optional[Rate]
    window: Optional[PeakWindow]

    def matches(self, model: str) -> bool:
        name = (model or "").strip().lower()
        if not name:
            return False
        return any(p in name for p in self.patterns)

    def rate(self, when: Optional[datetime] = None) -> Rate:
        if self.flat is not None:
            return self.flat
        # Without a timestamp we cannot claim the expensive tier: bill idle.
        if when is not None and self.window is not None and self.window.is_peak(when):
            return self.peak  # type: ignore[return-value]
        return self.idle  # type: ignore[return-value]

    @property
    def timed(self) -> bool:
        return self.flat is None


# ── Timestamp parsing ─────────────────────────────────────────────────────────
def parse_timestamp(value) -> Optional[datetime]:
    """Parse a transcript/DB timestamp into an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _FRACTION_RE.sub(r"\1", text)  # Python 3.7 accepts 3 or 6 digits only
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ── Price book ────────────────────────────────────────────────────────────────
class Pricing:
    def __init__(self, data: dict, path: Path, raw_text: str,
                 error: Optional[str] = None) -> None:
        self.path = path
        self.raw_text = raw_text
        self.error = error
        self.version = str(data.get("version", "unknown"))
        self.sources: Dict[str, str] = dict(data.get("sources") or {})
        fx = data.get("fx") or {}
        self.base_currency = str(fx.get("base", "USD"))
        self.fx: Dict[str, float] = {
            k: float(v) for k, v in fx.items()
            if k != "base" and isinstance(v, (int, float))
        }
        self.windows: Dict[str, PeakWindow] = {}
        for key, spec in (data.get("peak_windows") or {}).items():
            self.windows[key] = _parse_window(key, spec)
        self.models: Tuple[ModelPrice, ...] = tuple(
            _parse_model(spec, self.windows) for spec in (data.get("models") or [])
        )
        if not self.models:
            raise ValueError("pricing book contains no models")
        self.fallback_id = str(data.get("fallback") or self.models[0].model_id)
        self.fallback = self.by_id(self.fallback_id) or self.models[0]

    # -- lookup ---------------------------------------------------------------
    def by_id(self, model_id: str) -> Optional[ModelPrice]:
        for m in self.models:
            if m.model_id == model_id:
                return m
        return None

    def find(self, model: str) -> Optional[ModelPrice]:
        for m in self.models:
            if m.matches(model):
                return m
        return None

    def known(self, model: str) -> bool:
        return self.find(model) is not None

    # -- pricing --------------------------------------------------------------
    def rate_for(self, model: str, when=None) -> Tuple[ModelPrice, Rate, bool]:
        """Return (model entry, rate, used_fallback)."""
        entry = self.find(model)
        if entry is None:
            return self.fallback, self.fallback.rate(parse_timestamp(when)), True
        return entry, entry.rate(parse_timestamp(when)), False

    def cost(self, model: str, input_tokens: int, output_tokens: int,
             cache_write_tokens: int = 0, cache_read_tokens: int = 0,
             when=None) -> float:
        """Cost in USD for one API call."""
        entry, rate, _ = self.rate_for(model, when)
        native = rate.cost(input_tokens or 0, output_tokens or 0,
                           cache_write_tokens or 0, cache_read_tokens or 0)
        return self.to_usd(native, entry.currency)

    def is_peak(self, model: str, when) -> bool:
        entry = self.find(model)
        if entry is None or entry.window is None or not entry.timed:
            return False
        ts = parse_timestamp(when)
        return bool(ts and entry.window.is_peak(ts))

    def to_usd(self, amount: float, currency: str) -> float:
        if currency == self.base_currency:
            return amount
        rate = self.fx.get(currency)
        if not rate:
            return amount  # unknown currency: leave the number untouched
        return amount / rate

    # -- bookkeeping ----------------------------------------------------------
    def signature(self) -> str:
        digest = hashlib.sha1(self.raw_text.encode("utf-8")).hexdigest()[:12]
        return f"{self.version}:{digest}"

    def summary(self) -> dict:
        return {
            "path": str(self.path),
            "version": self.version,
            "signature": self.signature(),
            "models": [m.model_id for m in self.models],
            "fallback": self.fallback_id,
            "fx": dict(self.fx),
            "timed_models": [m.model_id for m in self.models if m.timed],
            "error": self.error,
        }


def _parse_time(text: str) -> time:
    m = _TIME_RE.match(str(text).strip())
    if not m:
        raise ValueError(f"bad time in pricing book: {text!r}")
    hour, minute, second = m.group(1), m.group(2), m.group(3)
    return time(int(hour), int(minute), int(second or 0))


def _parse_window(key: str, spec: dict) -> PeakWindow:
    offset = float(spec.get("utc_offset_hours", 0) or 0)
    tz = timezone(timedelta(hours=offset))
    ranges = tuple(
        (_parse_time(span[0]), _parse_time(span[1])) for span in (spec.get("ranges") or [])
    )
    holidays = frozenset(
        date.fromisoformat(str(d)[:10]) for d in (spec.get("holidays") or [])
    )
    return PeakWindow(
        key=key,
        label=str(spec.get("label", key)),
        tz=tz,
        weekdays=frozenset(int(d) for d in (spec.get("weekdays") or [1, 2, 3, 4, 5])),
        ranges=ranges,
        holidays=holidays,
    )


def _parse_rate(spec: dict) -> Rate:
    return Rate(
        input_miss=float(spec["input_miss"]),
        input_hit=float(spec["input_hit"]),
        cache_write=float(spec.get("cache_write", spec["input_miss"])),
        output=float(spec["output"]),
    )


def _parse_model(spec: dict, windows: Dict[str, PeakWindow]) -> ModelPrice:
    window_key = spec.get("peak_window")
    window = windows.get(window_key) if window_key else None
    if spec.get("rates"):
        flat, peak, idle = _parse_rate(spec["rates"]), None, None
    else:
        flat = None
        peak = _parse_rate(spec["peak"])
        idle = _parse_rate(spec.get("idle") or spec["peak"])
        if window is None:
            raise ValueError(f"model {spec.get('id')!r} has peak/idle prices but no peak_window")
    return ModelPrice(
        model_id=str(spec.get("id") or (spec.get("match") or ["?"])[0]),
        provider=str(spec.get("provider", "")),
        patterns=tuple(str(p).lower() for p in (spec.get("match") or [])),
        currency=str(spec.get("currency", "USD")).upper(),
        flat=flat,
        peak=peak,
        idle=idle,
        window=window,
    )


def _embedded(path: Path, error: str) -> Pricing:
    """
    Last-resort table so the Stop hook keeps recording usage when pricing.json
    is missing or malformed. Anthropic Sonnet rates, flat, matching the historic
    default of this tool.
    """
    data = {
        "version": "embedded",
        "fallback": "claude-sonnet-4-6",
        "models": [{
            "id": "claude-sonnet-4-6",
            "provider": "anthropic",
            "match": ["claude-"],
            "currency": "USD",
            "rates": {"input_miss": 3.00, "input_hit": 0.30,
                      "cache_write": 3.75, "output": 15.00},
        }],
    }
    return Pricing(data, path, json.dumps(data), error=error)


def load(path: Optional[Path] = None) -> Pricing:
    target = Path(path) if path else PRICING_FILE
    try:
        raw = target.read_text(encoding="utf-8")
        return Pricing(json.loads(raw), target, raw)
    except Exception as exc:  # noqa: BLE001 - never break the hook over a price file
        return _embedded(target, f"{type(exc).__name__}: {exc}")


DEFAULT = load()
