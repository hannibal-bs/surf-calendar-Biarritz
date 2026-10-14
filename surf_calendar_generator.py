#!/usr/bin/env python3
"""
Generate a derived surf-window calendar from a tide ICS/webcal feed.

Rule:
  find crossings of a chosen predicted tide height (default 2.90 m)
  create a 90-minute surf event centered on each crossing, both rising/falling
  when high tide does not reach the target, create one fallback 90-minute
  surf event centered on high tide (not two events)
  surf events are clipped to the useful daily time range, default 07:00-22:00
  clipped events shorter than the minimum duration, default 60 minutes, are ignored

  IMPORTANT: tide-only approximation. No swell, wind or safe-access prediction.

Example:
  python surf_calendar_generator.py ^
    --source "webcal://webcaltides.org/tides/T232e6c9.ics?units=metric" ^
    --output surf-biarritz.ics ^
    --timezone Europe/Paris ^
    --preview 12
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
    center: datetime
    phase: str
    clipped: bool = False
    fallback: bool = False


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
    target_height_m: float = 2.90,
    window_duration_minutes: int = 90,
    min_duration_minutes: int = 60,
    day_start_hour: float = 7.0,
    day_end_hour: float = 22.0,
) -> list[SurfWindow]:
    """Find height crossings; fall back to high-water time below the target.

    Assumes a cosine-shaped rise/fall between successive high and low tides:
      h(u) = h0 + (h1 - h0) * (1 - cos(pi*u)) / 2, where 0 <= u <= 1.
    Inverts that formula to estimate the target crossing time.
    """
    if not (0 <= day_start_hour < 24 and 0 < day_end_hour <= 24):
        raise ValueError("Invalid daily start/end hours.")
    if day_end_hour <= day_start_hour:
        raise ValueError("day_end_hour must be greater than day_start_hour.")
    if not math.isfinite(target_height_m):
        raise ValueError("target_height_m must be finite.")
    if window_duration_minutes <= 0 or min_duration_minutes <= 0:
        raise ValueError("Window duration and minimum duration must be positive.")

    half_window = timedelta(minutes=window_duration_minutes / 2)
    minimum = timedelta(minutes=min_duration_minutes)
    windows: list[SurfWindow] = []

    for current_tide, next_tide in zip(tides, tides[1:]):
        h0, h1 = current_tide.height_m, next_tide.height_m
        if h0 is None or h1 is None or current_tide.kind == next_tide.kind or h0 == h1:
            continue

        fraction = (target_height_m - h0) / (h1 - h0)
        # If the target is never crossed (or merely touched at an extremum),
        # do not invent a surfing window.
        if not 0.0 < fraction < 1.0:
            continue

        origin_utc = current_tide.start.astimezone(timezone.utc)
        finish_utc = next_tide.start.astimezone(timezone.utc)
        span_seconds = (finish_utc - origin_utc).total_seconds()
        if span_seconds <= 0:
            continue

        share_of_half_cycle = math.acos(1 - 2 * fraction) / math.pi
        center_utc = origin_utc + timedelta(seconds=span_seconds * share_of_half_cycle)
        tz = current_tide.start.tzinfo
        center = center_utc.astimezone(tz)
        phase = "montante" if h1 > h0 else "descendante"

        # Perform the duration arithmetic in UTC, including across DST changes.
        raw_start = (center_utc - half_window).astimezone(tz)
        raw_end = (center_utc + half_window).astimezone(tz)

        for start, end, was_clipped in iter_daily_intersections(
            raw_start, raw_end,
            day_start_hour=day_start_hour,
            day_end_hour=day_end_hour,
        ):
            if end.astimezone(timezone.utc) - start.astimezone(timezone.utc) >= minimum:
                windows.append(
                    SurfWindow(
                        start=start, end=end, source=current_tide,
                        next_tide=next_tide, center=center,
                        phase=phase, clipped=was_clipped,
                    )
                )

    # One fallback window per low high tide, not one on each side of it.
    # At exactly target height, the high tide itself is the target crossing.
    for high_tide in tides:
        if (high_tide.kind != "high" or high_tide.height_m is None
                or high_tide.height_m > target_height_m):
            continue

        center = high_tide.start
        center_utc = center.astimezone(timezone.utc)
        tz = center.tzinfo
        raw_start = (center_utc - half_window).astimezone(tz)
        raw_end = (center_utc + half_window).astimezone(tz)
        for start, end, was_clipped in iter_daily_intersections(
            raw_start, raw_end,
            day_start_hour=day_start_hour,
            day_end_hour=day_end_hour,
        ):
            if end.astimezone(timezone.utc) - start.astimezone(timezone.utc) >= minimum:
                windows.append(
                    SurfWindow(
                        start=start, end=end, source=high_tide, next_tide=high_tide,
                        center=center, phase="pleine mer", clipped=was_clipped,
                        fallback=True,
                    )
                )

    windows.sort(key=lambda window: window.start.astimezone(timezone.utc))
    return windows


def format_kind(kind: str) -> str:
    if kind == "high":
        return "marée haute"
    if kind == "low":
        return "marée basse"
    return "marée"


def make_uid(window: SurfWindow) -> str:
    material = "|".join(
        [
            window.source.uid,
            window.next_tide.uid,
            "peak-fallback" if window.fallback else "height-crossing",
        ]
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest() + "@surf-calendar"


def generate_calendar(
    windows: Iterable[SurfWindow], calendar_name: str, timezone_name: str,
    target_height_m: float = 2.90, window_duration_minutes: int = 90,
) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//Surf Calendar Generator//Biarritz Tide Windows//FR")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", calendar_name)
    cal.add("x-wr-timezone", timezone_name)

    now = datetime.now(timezone.utc)
    for window in windows:
        event = Event()
        event.add("uid", make_uid(window))
        event.add("dtstamp", now)
        event.add("created", now)
        event.add("last-modified", now)
        event.add("dtstart", window.start)
        event.add("dtend", window.end)
        event.add(
            "summary",
            "Surf — Côte des Basques — repli (pleine mer)" if window.fallback
            else f"Surf — Côte des Basques — {window.phase}",
        )
        event.add("location", "Côte des Basques, Biarritz")
        if window.fallback:
            description_lines = [
                "Créneau de repli : la pleine mer reste sous la hauteur cible.",
                "",
                f"Hauteur cible provisoire : {target_height_m:.2f} m.",
                f"Pleine mer prévue : {window.source.height_m:.2f} m à "
                f"{window.center.strftime('%d/%m/%Y %H:%M %Z')}.",
                f"Fenêtre de {window_duration_minutes} minutes centrée sur la pleine mer.",
                "Ce créneau est une hypothèse à confirmer sur le spot, pas une garantie de bonnes vagues.",
            ]
        else:
            description_lines = [
                "Créneau indicatif calculé à partir de la hauteur de marée prédite.",
                "",
                f"Hauteur visée : {target_height_m:.2f} m (référentiel du flux source).",
                f"Passage estimé : {window.center.strftime('%d/%m/%Y %H:%M %Z')} ({window.phase}).",
                f"Marée précédente : {window.source.summary} — {window.source.start.strftime('%d/%m/%Y %H:%M %Z')}",
                f"Marée suivante : {window.next_tide.summary} — {window.next_tide.start.strftime('%d/%m/%Y %H:%M %Z')}",
                "",
                f"Calcul : interpolation sinusoïdale entre deux marées, fenêtre de {window_duration_minutes} minutes centrée sur le passage.",
            ]
        description_lines.extend([
            "Filtre horaire : 07:00–22:00 ; créneaux tronqués de moins de 60 min exclus.",
            "ATTENTION : ni la houle, ni le vent, ni la sécurité d'accès aux rochers ne sont évalués.",
        ])
        if window.clipped:
            description_lines.append("Fenêtre tronquée par les limites horaires.")
        event.add("description", "\n".join(description_lines))
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
    for window in windows[:n]:
        duration = window.end - window.start
        total_minutes = int(duration.total_seconds() // 60)
        clipped_marker = " | tronqué 07:00-22:00" if window.clipped else ""
        print(
            f"- {window.start:%Y-%m-%d %H:%M %Z} → {window.end:%H:%M %Z} "
            f"({total_minutes // 60}h{total_minutes % 60:02d}) | "
            f"{'repli pleine mer' if window.fallback else window.phase}, "
            f"passage {window.center:%H:%M}{clipped_marker}"
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a surf-window ICS calendar from tide ICS events.")
    parser.add_argument("--source", required=True, help="Source tide calendar URL or local .ics file. webcal:// is accepted.")
    parser.add_argument("--output", default="surf-calendar.ics", help="Output .ics file path.")
    parser.add_argument("--timezone", default="Europe/Paris", help="Timezone used for naive source dates.")
    parser.add_argument("--calendar-name", default="Surf — fenêtres marées", help="Calendar display name.")
    parser.add_argument("--target-height-m", type=float, default=2.90, help="Target tide height, meters in upstream feed's datum.")
    parser.add_argument("--window-duration-minutes", type=int, default=90, help="Duration centered on target height crossing.")
    parser.add_argument("--min-duration-minutes", type=int, default=60, help="Ignore derived windows shorter than this duration, default 60 minutes.")
    parser.add_argument("--day-start-hour", type=float, default=7.0, help="Earliest allowed daily event start hour, default 7.0.")
    parser.add_argument("--day-end-hour", type=float, default=22.0, help="Latest allowed daily event end hour, default 22.0.")
    parser.add_argument("--preview", type=int, default=8, help="Number of tides/windows to preview.")
    args = parser.parse_args(argv)

    try:
        ics_bytes = load_ics(args.source)
        tides, ignored = parse_tides(ics_bytes, args.timezone)
        if len(tides) < 2:
            raise RuntimeError(f"Only {len(tides)} high/low tide event(s) found. Need at least 2.")

        missing_heights = sum(tide.height_m is None for tide in tides)
        if missing_heights:
            raise RuntimeError(
                f"{missing_heights}/{len(tides)} marées sans hauteur exploitable : "
                "calendrier non publié pour éviter des créneaux incomplets."
            )

        windows = build_surf_windows(
            tides,
            target_height_m=args.target_height_m,
            window_duration_minutes=args.window_duration_minutes,
            min_duration_minutes=args.min_duration_minutes,
            day_start_hour=args.day_start_hour,
            day_end_hour=args.day_end_hour,
        )
        if not windows:
            raise RuntimeError("Aucun créneau exploitable (passage cible ou pleine mer de repli).")
        cal = generate_calendar(
            windows, args.calendar_name, args.timezone,
            target_height_m=args.target_height_m,
            window_duration_minutes=args.window_duration_minutes,
        )
        output_path = Path(args.output).expanduser().resolve()
        output_path.write_bytes(cal.to_ical())

        print(
            f"\nOK — {len(tides)} marées haute/basse lues, {len(ignored)} événements ignorés, "
            f"{len(windows)} créneaux surf générés entre {args.day_start_hour:g}h et {args.day_end_hour:g}h "
            f"autour de {args.target_height_m:.2f} m, fenêtre {args.window_duration_minutes} min, "
            f"durée minimale après troncature {args.min_duration_minutes} min."
        )
        print(f"Fichier créé : {output_path}")
        print_preview(tides, ignored, windows, args.preview)
        return 0
    except Exception as exc:  # keep CLI readable for non-dev use
        print(f"\nERREUR : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
