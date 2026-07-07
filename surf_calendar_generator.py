#!/usr/bin/env python3
"""
Generate a derived surf-window calendar from a tide ICS/webcal feed.

Rule:
  surf event starts 2h after each HIGH/LOW tide event
  surf event ends 2h before the following HIGH/LOW tide event
  surf events are clipped to the useful daily time range, default 07:00-22:00
  surf events shorter than the minimum duration, default 60 minutes, are ignored

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


@dataclass(frozen=True)
class SurfWindow:
    start: datetime
    end: datetime
    source: TideEvent
    next_tide: TideEvent
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
        tides.append(TideEvent(start=start, summary=summary, kind=kind, uid=uid, raw_description=description))

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
    start_offset_hours: float = 2.0,
    end_offset_hours: float = 2.0,
    min_duration_minutes: int = 60,
    day_start_hour: float = 7.0,
    day_end_hour: float = 22.0,
) -> list[SurfWindow]:
    if not (0 <= day_start_hour < 24):
        raise ValueError("day_start_hour must be between 0 and 24, excluded 24.")
    if not (0 < day_end_hour <= 24):
        raise ValueError("day_end_hour must be between 0 and 24, included 24.")
    if day_end_hour <= day_start_hour:
        raise ValueError("day_end_hour must be greater than day_start_hour.")

    windows: list[SurfWindow] = []
    start_offset = timedelta(hours=start_offset_hours)
    end_offset = timedelta(hours=end_offset_hours)
    min_duration = timedelta(minutes=min_duration_minutes)

    for current_tide, next_tide in zip(tides, tides[1:]):
        raw_start = current_tide.start + start_offset
        raw_end = next_tide.start - end_offset

        for start, end, was_clipped in iter_daily_intersections(
            raw_start,
            raw_end,
            day_start_hour=day_start_hour,
            day_end_hour=day_end_hour,
        ):
            if end - start >= min_duration:
                windows.append(
                    SurfWindow(
                        start=start,
                        end=end,
                        source=current_tide,
                        next_tide=next_tide,
                        clipped=was_clipped,
                    )
                )

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
            window.start.isoformat(),
            window.end.isoformat(),
        ]
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest() + "@surf-calendar"


def generate_calendar(windows: Iterable[SurfWindow], calendar_name: str, timezone_name: str) -> Calendar:
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
        event.add("summary", f"Surf — après {format_kind(window.source.kind)}")
        event.add("location", "Biarritz")
        description_lines = [
            "Créneau généré automatiquement à partir du calendrier des marées.",
            "",
            f"Marée source : {window.source.summary} — {window.source.start.strftime('%d/%m/%Y %H:%M %Z')}",
            f"Marée suivante : {window.next_tide.summary} — {window.next_tide.start.strftime('%d/%m/%Y %H:%M %Z')}",
            "",
            "Règle : début = marée source + 2h ; fin = marée suivante - 2h.",
            "Limite horaire appliquée : créneau conservé uniquement entre 07:00 et 22:00.",
        ]
        if window.clipped:
            description_lines.append("Ce créneau a été tronqué pour respecter la limite 07:00-22:00.")
        event.add("description", "\n".join(description_lines))
        cal.add_component(event)

    return cal


def print_preview(tides: list[TideEvent], ignored: list[tuple[datetime, str]], windows: list[SurfWindow], n: int) -> None:
    print("\nPremières marées utiles détectées :")
    for tide in tides[:n]:
        print(f"- {tide.start:%Y-%m-%d %H:%M %Z} | {format_kind(tide.kind)} | {tide.summary}")

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
            f"({total_minutes // 60}h{total_minutes % 60:02d}) | après {format_kind(window.source.kind)}{clipped_marker}"
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a surf-window ICS calendar from tide ICS events.")
    parser.add_argument("--source", required=True, help="Source tide calendar URL or local .ics file. webcal:// is accepted.")
    parser.add_argument("--output", default="surf-calendar.ics", help="Output .ics file path.")
    parser.add_argument("--timezone", default="Europe/Paris", help="Timezone used for naive source dates.")
    parser.add_argument("--calendar-name", default="Surf — fenêtres marées", help="Calendar display name.")
    parser.add_argument("--start-offset-hours", type=float, default=2.0, help="Hours after each tide for event start.")
    parser.add_argument("--end-offset-hours", type=float, default=2.0, help="Hours before following tide for event end.")
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

        windows = build_surf_windows(
            tides,
            start_offset_hours=args.start_offset_hours,
            end_offset_hours=args.end_offset_hours,
            min_duration_minutes=args.min_duration_minutes,
            day_start_hour=args.day_start_hour,
            day_end_hour=args.day_end_hour,
        )
        cal = generate_calendar(windows, args.calendar_name, args.timezone)
        output_path = Path(args.output).expanduser().resolve()
        output_path.write_bytes(cal.to_ical())

        print(
            f"\nOK — {len(tides)} marées haute/basse lues, {len(ignored)} événements ignorés, "
            f"{len(windows)} créneaux surf générés entre {args.day_start_hour:g}h et {args.day_end_hour:g}h "
            f"avec durée minimale {args.min_duration_minutes} min."
        )
        print(f"Fichier créé : {output_path}")
        print_preview(tides, ignored, windows, args.preview)
        return 0
    except Exception as exc:  # keep CLI readable for non-dev use
        print(f"\nERREUR : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
