#!/usr/bin/env python3
"""Generate variable-duration surf windows within 1.65–2.40 m WebCalTides.
Intervals are cosine-interpolated between consecutive high/low predictions,
merged, then clipped to 07:00–22:00 and filtered for at least 60 minutes.
The 2.10 m optimum is indicative, not an additional filter.
No swell, wind, surge or access-safety assessment is performed.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar, Event


HIGH_PATTERNS = [
    r"\bhigh tide\b",
    r"\bhigh\b",
    r"\bmar(?:e|é)e haute\b",
    r"\bpleine mer\b",
    r"\bhaute mer\b",
]
LOW_PATTERNS = [
    r"\blow tide\b",
    r"\blow\b",
    r"\bmar(?:e|é)e basse\b",
    r"\bbasse mer\b",
]


@dataclass(frozen=True)
class TideEvent:
    start: datetime
    summary: str
    kind: str  # "high" or "low"
    uid: str
    raw_description: str = ""
    height_m: Optional[float] = None


@dataclass(frozen=True)
class SurfWindow:
    start: datetime
    end: datetime
    source: TideEvent
    next_tide: TideEvent
    phase: str
    clipped: bool = False


def normalize_source_url(source: str) -> str:
    source = source.strip()
    if source.startswith("webcal://"):
        return "https://" + source[len("webcal://") :]
    return source


def load_ics(source: str, timeout: int = 30) -> bytes:
    source = normalize_source_url(source)

    if source.startswith("http://") or source.startswith("https://"):
        headers = {
            "User-Agent": "surf-calendar-generator/2.0",
            "Accept": "text/calendar, text/plain, */*",
        }
        response = requests.get(source, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.content

    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path}")
    return path.read_bytes()


def text_value(component: Event, field: str) -> str:
    value = component.get(field)
    if value is None:
        return ""
    return str(value)


def to_datetime(value, default_tz: ZoneInfo) -> Optional[datetime]:
    if value is None:
        return None
    dt = value.dt if hasattr(value, "dt") else value
    if isinstance(dt, date) and not isinstance(dt, datetime):
        # Ignore all-day events; tide events should be timed.
        return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=default_tz)
    return dt.astimezone(default_tz)


def strip_accents_light(text: str) -> str:
    return (
        text.casefold()
        .replace("é", "e")
        .replace("è", "e")
        .replace("ê", "e")
        .replace("ë", "e")
        .replace("à", "a")
        .replace("â", "a")
        .replace("ù", "u")
        .replace("û", "u")
        .replace("î", "i")
        .replace("ï", "i")
        .replace("ô", "o")
        .replace("ö", "o")
        .replace("ç", "c")
    )


def classify_tide(summary: str, description: str = "") -> Optional[str]:
    text = strip_accents_light(f"{summary} {description}")
    high_patterns = [strip_accents_light(p) for p in HIGH_PATTERNS]
    low_patterns = [strip_accents_light(p) for p in LOW_PATTERNS]

    high = any(re.search(p, text, re.IGNORECASE) for p in high_patterns)
    low = any(re.search(p, text, re.IGNORECASE) for p in low_patterns)

    if high and not low:
        return "high"
    if low and not high:
        return "low"
    return None


HEIGHT_RE = re.compile(r"(?<!\w)(-?\d+(?:[.,]\d+)?)\s*(m|metres?|meters?|ft|feet)\b", re.IGNORECASE)


def extract_height_m(summary: str, description: str = "") -> Optional[float]:
    """Extract the prediction, e.g. WebCalTides SUMMARY:High Tide 3.2 m."""
    for source_text in (summary, description):
        match = HEIGHT_RE.search(source_text)
        if match:
            value = float(match.group(1).replace(",", "."))
            return value * 0.3048 if match.group(2).lower() in {"ft", "feet"} else value
    return None


def parse_tides(ics_bytes: bytes, default_tz_name: str) -> tuple[list[TideEvent], list[tuple[datetime, str]]]:
    default_tz = ZoneInfo(default_tz_name)
    cal = Calendar.from_ical(ics_bytes)
    tides: list[TideEvent] = []
    ignored: list[tuple[datetime, str]] = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        start = to_datetime(component.get("DTSTART"), default_tz)
        if start is None:
            continue

        summary = text_value(component, "SUMMARY").strip()
        description = text_value(component, "DESCRIPTION").strip()
        kind = classify_tide(summary, description)

        # Important: WebCalTides also includes Sunrise/Sunset events.
        # We only keep actual High Tide / Low Tide events.
        if kind is None:
            ignored.append((start, summary))
            continue

        uid = text_value(component, "UID").strip() or hashlib.sha1(
            f"{start.isoformat()}|{summary}|{description}".encode("utf-8")
        ).hexdigest()
        tides.append(
            TideEvent(
                start=start, summary=summary, kind=kind, uid=uid,
                raw_description=description, height_m=extract_height_m(summary, description),
            )
        )

    tides.sort(key=lambda event: event.start)
    ignored.sort(key=lambda event: event[0])
    return tides, ignored


def iter_daily_intersections(
    start: datetime,
    end: datetime,
    day_start_hour: float,
    day_end_hour: float,
) -> Iterable[tuple[datetime, datetime, bool]]:
    """Yield intersections between [start, end] and each daily allowed time range.

    Example with the default range 07:00-22:00:
    - 05:30-08:00 becomes 07:00-08:00
    - 20:30-23:00 becomes 20:30-22:00
    - 23:30-01:00 is ignored
    """
    if end <= start:
        return

    tz = start.tzinfo
    current_day = start.date()
    last_day = end.date()

    def hour_to_time(day, hour: float) -> datetime:
        whole_hours = int(hour)
        minutes = int(round((hour - whole_hours) * 60))
        if minutes == 60:
            whole_hours += 1
            minutes = 0
        return datetime(day.year, day.month, day.day, whole_hours, minutes, tzinfo=tz)

    while current_day <= last_day:
        allowed_start = hour_to_time(current_day, day_start_hour)
        allowed_end = hour_to_time(current_day, day_end_hour)

        clipped_start = max(start, allowed_start)
        clipped_end = min(end, allowed_end)
        if clipped_end > clipped_start:
            was_clipped = clipped_start != start or clipped_end != end
            yield clipped_start, clipped_end, was_clipped

        current_day = current_day + timedelta(days=1)


def build_surf_windows(
    tides: list[TideEvent],
    lower_height_m: float = 1.65,
    upper_height_m: float = 2.40,
    min_duration_minutes: int = 60,
    day_start_hour: float = 7.0,
    day_end_hour: float = 22.0,
) -> list[SurfWindow]:
    """Intersections of the cosine-interpolated water level with [lower, upper].

    Adjacent high/low segments are merged *before* duration filtering; this
    handles a high tide within the permitted band as one continuous window.
    """
    if not (0 <= day_start_hour < 24 and 0 < day_end_hour <= 24):
        raise ValueError("Invalid daily start/end hours.")
    if day_end_hour <= day_start_hour:
        raise ValueError("day_end_hour must be greater than day_start_hour.")
    if not (math.isfinite(lower_height_m) and math.isfinite(upper_height_m)
            and lower_height_m < upper_height_m):
        raise ValueError("Height range must be finite with lower < upper.")
    if min_duration_minutes <= 0:
        raise ValueError("Minimum duration must be positive.")

    segments: list[SurfWindow] = []
    for tide, next_tide in zip(tides, tides[1:]):
        h0, h1 = tide.height_m, next_tide.height_m
        if h0 is None or h1 is None or tide.kind == next_tide.kind:
            continue
        if h0 == h1 or max(h0, h1) < lower_height_m or min(h0, h1) > upper_height_m:
            continue
        a = tide.start.astimezone(timezone.utc)
        b = next_tide.start.astimezone(timezone.utc)
        seconds = (b - a).total_seconds()
        if seconds <= 0:
            continue

        def at_height(height: float) -> datetime:
            x = (height - h0) / (h1 - h0)
            if x <= 0:
                return a
            if x >= 1:
                return b
            return a + timedelta(seconds=seconds * math.acos(1 - 2 * x) / math.pi)

        if h1 > h0:
            start, end, phase = at_height(lower_height_m), at_height(upper_height_m), "montante"
        else:
            start, end, phase = at_height(upper_height_m), at_height(lower_height_m), "descendante"
        if end <= start:
            continue
        tz = tide.start.tzinfo
        segments.append(SurfWindow(
            start.astimezone(tz), end.astimezone(tz), tide, next_tide, phase
        ))

    segments.sort(key=lambda w: w.start.astimezone(timezone.utc))
    merged: list[SurfWindow] = []
    for w in segments:
        if merged and w.start.astimezone(timezone.utc) <= (
            merged[-1].end.astimezone(timezone.utc) + timedelta(seconds=1)
        ):
            p = merged[-1]
            around_high = (p.phase == "pleine mer" or w.phase == "pleine mer"
                           or (p.phase == "montante" and w.phase == "descendante"
                               and p.next_tide.kind == "high"))
            phase = "pleine mer" if around_high else (
                p.phase if p.phase == w.phase else "marée variable"
            )
            merged[-1] = SurfWindow(
                p.start,
                max(p.end, w.end, key=lambda x: x.astimezone(timezone.utc)),
                p.source, w.next_tide, phase,
            )
        else:
            merged.append(w)

    result: list[SurfWindow] = []
    for w in merged:
        for start, end, clipped in iter_daily_intersections(
            w.start, w.end, day_start_hour, day_end_hour
        ):
            if end.astimezone(timezone.utc) - start.astimezone(timezone.utc) >= timedelta(minutes=min_duration_minutes):
                result.append(SurfWindow(
                    start, end, w.source, w.next_tide, w.phase, clipped
                ))
    return sorted(result, key=lambda w: w.start.astimezone(timezone.utc))


def format_kind(kind: str) -> str:
    if kind == "high":
        return "marée haute"
    if kind == "low":
        return "marée basse"
    return "marée"


def make_uid(window: SurfWindow) -> str:
    material = "|".join([
        window.source.uid, window.next_tide.uid,
        window.start.astimezone(timezone.utc).isoformat(), "height-range",
    ])
    return hashlib.sha1(material.encode("utf-8")).hexdigest() + "@surf-calendar"


def generate_calendar(
    windows: Iterable[SurfWindow], calendar_name: str, timezone_name: str,
    lower_height_m: float = 1.65, upper_height_m: float = 2.40,
    optimal_height_m: float = 2.10,
    min_duration_minutes: int = 60,
    day_start_hour: float = 7.0, day_end_hour: float = 22.0,
) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//Surf Calendar Generator//Biarritz Tide Windows//FR")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", calendar_name)
    cal.add("x-wr-timezone", timezone_name)
    now = datetime.now(timezone.utc)
    for w in windows:
        event = Event()
        event.add("uid", make_uid(w))
        event.add("dtstamp", now)
        event.add("created", now)
        event.add("last-modified", now)
        event.add("dtstart", w.start)
        event.add("dtend", w.end)
        event.add("summary", f"Surf — Côte des Basques — {w.phase}")
        event.add("location", "Côte des Basques, Biarritz")
        lines = [
            "Fenêtre de surf potentiellement favorable, durée variable (marée seule).",
            "",
            f"Plage de hauteur : {lower_height_m:.2f}–{upper_height_m:.2f} m WebCalTides/TICON.",
            f"Hauteur optimale présumée : {optimal_height_m:.2f} m (indicative seulement).",
            f"Phase : {w.phase}.",
            f"Marée précédente : {w.source.summary} — {w.source.start.strftime('%d/%m/%Y %H:%M %Z')}",
            f"Marée suivante : {w.next_tide.summary} — {w.next_tide.start.strftime('%d/%m/%Y %H:%M %Z')}",
            "",
            "Calcul : interpolation cosinus entre marées successives, fusion des intervalles contigus.",
            f"Filtre : {day_start_hour:g}h–{day_end_hour:g}h ; minimum {min_duration_minutes} minutes.",
            "ATTENTION : houle, vent, surcote et sécurité d'accès aux rochers non évalués.",
        ]
        if w.clipped:
            lines.append("Fenêtre tronquée par les limites horaires.")
        event.add("description", "\n".join(lines))
        cal.add_component(event)
    return cal


def print_preview(tides: list[TideEvent], ignored: list[tuple[datetime, str]], windows: list[SurfWindow], n: int) -> None:
    print("\nPremières marées utiles détectées :")
    for tide in tides[:n]:
        print(f"- {tide.start:%Y-%m-%d %H:%M %Z} | {format_kind(tide.kind)} | {tide.summary} | hauteur {tide.height_m}")
    if ignored:
        print(f"\nInfo : {len(ignored)} événement(s) non-marée ignoré(s), par exemple :")
        for start, summary in ignored[: min(5, len(ignored))]:
            print(f"- {start:%Y-%m-%d %H:%M %Z} | ignoré | {summary}")
    print("\nPremiers créneaux surf générés :")
    for w in windows[:n]:
        minutes = int((w.end.astimezone(timezone.utc) - w.start.astimezone(timezone.utc)).total_seconds() // 60)
        clipped = " | tronqué par plage horaire" if w.clipped else ""
        print(f"- {w.start:%Y-%m-%d %H:%M %Z} → {w.end:%H:%M %Z} "
              f"({minutes // 60}h{minutes % 60:02d}) | {w.phase}{clipped}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a surf-window ICS calendar from tide ICS events.")
    parser.add_argument("--source", required=True, help="Source tide calendar URL or local .ics file. webcal:// is accepted.")
    parser.add_argument("--output", default="surf-calendar.ics", help="Output .ics file path.")
    parser.add_argument("--timezone", default="Europe/Paris", help="Timezone used for naive source dates.")
    parser.add_argument("--calendar-name", default="Surf — fenêtres marées", help="Calendar display name.")
    parser.add_argument("--lower-height-m", type=float, default=1.65, help="Lower bound in source tide height datum (m).")
    parser.add_argument("--upper-height-m", type=float, default=2.40, help="Upper bound in source tide height datum (m).")
    parser.add_argument("--optimal-height-m", type=float, default=2.10, help="Indicative optimal height for event descriptions only (m).")
    parser.add_argument("--min-duration-minutes", type=int, default=60, help="Ignore derived windows shorter than this duration.")
    parser.add_argument("--day-start-hour", type=float, default=7.0, help="Earliest allowed daily event start hour.")
    parser.add_argument("--day-end-hour", type=float, default=22.0, help="Latest allowed daily event end hour.")
    parser.add_argument("--preview", type=int, default=8, help="Number of tides/windows to preview.")
    args = parser.parse_args(argv)

    try:
        if not args.lower_height_m <= args.optimal_height_m <= args.upper_height_m:
            raise ValueError("optimal_height_m must lie within the lower/upper range.")
        tides, ignored = parse_tides(load_ics(args.source), args.timezone)
        if len(tides) < 2:
            raise RuntimeError(f"Only {len(tides)} high/low tide event(s) found. Need at least 2.")
        missing = sum(t.height_m is None for t in tides)
        if missing:
            raise RuntimeError(
                f"{missing}/{len(tides)} marées sans hauteur exploitable : "
                "calendrier non publié pour éviter des créneaux incomplets."
            )
        windows = build_surf_windows(
            tides, lower_height_m=args.lower_height_m, upper_height_m=args.upper_height_m,
            min_duration_minutes=args.min_duration_minutes,
            day_start_hour=args.day_start_hour, day_end_hour=args.day_end_hour,
        )
        if not windows:
            raise RuntimeError("Aucun créneau exploitable dans la plage de hauteurs spécifiée.")
        cal = generate_calendar(
            windows, args.calendar_name, args.timezone,
            lower_height_m=args.lower_height_m, upper_height_m=args.upper_height_m,
            optimal_height_m=args.optimal_height_m,
            min_duration_minutes=args.min_duration_minutes,
            day_start_hour=args.day_start_hour, day_end_hour=args.day_end_hour,
        )
        output_path = Path(args.output).expanduser().resolve()
        output_path.write_bytes(cal.to_ical())
        print(
            f"\nOK — {len(tides)} marées lues, {len(ignored)} événements ignorés, "
            f"{len(windows)} créneaux surf entre {args.day_start_hour:g}h et {args.day_end_hour:g}h, "
            f"plage {args.lower_height_m:.2f}–{args.upper_height_m:.2f} m, durée variable, "
            f"minimum {args.min_duration_minutes} min."
        )
        print(f"Fichier créé : {output_path}")
        print_preview(tides, ignored, windows, args.preview)
        return 0
    except Exception as exc:
        print(f"\nERREUR : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
