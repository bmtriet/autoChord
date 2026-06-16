#!/usr/bin/env python3
"""
Learn left-hand chord voicings from a MIDI controller, then auto-accompany
right-hand melody notes by sending learned chords to GarageBand or another DAW.
"""

from __future__ import annotations
 
import argparse
import html
import http.server
import json
import queue
import random
import re
import signal
import socketserver
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


try:
    import mido
except ImportError:  # pragma: no cover - friendly CLI error
    mido = None


DEFAULT_SPLIT = 60
DEFAULT_CHORD_WINDOW = 0.090
DEFAULT_LEFT_PATTERN_WINDOW = 0.800
DEFAULT_NEW_CHORD_GAP = 0.650
DEFAULT_PAIR_WINDOW = 1.200
DEFAULT_CHORD_DURATION = 1.250
DEFAULT_ARP_STEP = 0.140
DEFAULT_NOTE_LENGTH = 0.180
DEFAULT_PROGRESSION_GAP = 0.650
DEFAULT_CHART_PERIOD = 1.750

NOTE_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
NOTE_TO_PC = {
    "C": 0,
    "B#": 0,
    "C#": 1,
    "Db": 1,
    "D": 2,
    "D#": 3,
    "Eb": 3,
    "E": 4,
    "Fb": 4,
    "E#": 5,
    "F": 5,
    "F#": 6,
    "Gb": 6,
    "G": 7,
    "G#": 8,
    "Ab": 8,
    "A": 9,
    "A#": 10,
    "Bb": 10,
    "B": 11,
    "Cb": 11,
}


@dataclass(frozen=True)
class ChordExample:
    notes: Tuple[int, ...]
    intervals: Tuple[int, ...]
    pattern: Tuple[Tuple[int, float, int], ...]
    bass_pc: int
    melody_pc: int
    previous_bass_pc: Optional[int]
    velocity: int
    duration: float
    count: int = 1
    token: Optional[str] = None



@dataclass(frozen=True)
class LyricChordEvent:
    time_s: float
    chord: str
    lyric: str
    section: str = "verse"


@dataclass(frozen=True)
class ScheduledMidiEvent:
    time_s: float
    kind: str
    note: int
    velocity: int = 0


def require_mido() -> None:
    if mido is None:
        raise SystemExit(
            "Missing dependency. Run: python3 -m pip install -r requirements.txt"
        )


def note_name(note: int) -> str:
    return f"{NOTE_NAMES[note % 12]}{(note // 12) - 1}"


def chord_name(notes: Iterable[int]) -> str:
    pcs = sorted({n % 12 for n in notes})
    if not pcs:
        return "none"
    return "/".join(NOTE_NAMES[p] for p in pcs)


def normalize_chord(notes: Iterable[int]) -> Tuple[int, ...]:
    return tuple(sorted(set(notes)))


def chord_signature(notes: Iterable[int]) -> Tuple[int, Tuple[int, ...]]:
    chord = normalize_chord(notes)
    bass = chord[0] % 12
    intervals = tuple((n - chord[0]) % 12 for n in chord)
    return bass, intervals


def hand_for_note(note: int, split: int) -> str:
    return "left" if note < split else "right"


def midi_event_dict(msg, started_at: float, now: float, split: int, source: str) -> Optional[dict]:
    base = {
        "t": round(now - started_at, 4),
        "source": source,
        "channel": getattr(msg, "channel", None),
    }
    if msg.type in ("note_on", "note_off"):
        velocity = getattr(msg, "velocity", 0)
        event_type = "note_on" if msg.type == "note_on" and velocity > 0 else "note_off"
        return {
            **base,
            "event": event_type,
            "note": msg.note,
            "name": note_name(msg.note),
            "pc": msg.note % 12,
            "velocity": velocity,
            "hand": hand_for_note(msg.note, split),
        }
    if msg.type == "control_change":
        return {
            **base,
            "event": "control_change",
            "control": msg.control,
            "value": msg.value,
        }
    if msg.type == "pitchwheel":
        return {
            **base,
            "event": "pitchwheel",
            "pitch": msg.pitch,
        }
    return None


class JsonlLogger:
    def __init__(self, path: Optional[str]):
        self.path = Path(path) if path else None
        self.file = None

    def __enter__(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.file = self.path.open("w", encoding="utf-8")
        return self

    def write(self, item: dict) -> None:
        if self.file:
            self.file.write(json.dumps(item, ensure_ascii=True) + "\n")
            self.file.flush()

    def __exit__(self, _exc_type, _exc, _tb):
        if self.file:
            self.file.close()


class BpmTracker:
    def __init__(self, base_bpm: float, min_bpm: float, max_bpm: float, smoothing: float):
        self.base_bpm = base_bpm
        self.min_bpm = min_bpm
        self.max_bpm = max_bpm
        self.smoothing = max(0.0, min(0.95, smoothing))
        self.last_note_time: Optional[float] = None
        self.bpm: Optional[float] = None

    def feed_note_on(self, now: float) -> Optional[float]:
        if self.last_note_time is None:
            self.last_note_time = now
            return self.bpm
        interval = now - self.last_note_time
        self.last_note_time = now
        if interval < 0.10 or interval > 2.40:
            return self.bpm

        candidates = []
        for subdivision in (1, 2, 3, 4, 6, 8):
            beat_seconds = interval * subdivision
            if beat_seconds <= 0:
                continue
            bpm = 60.0 / beat_seconds
            if self.min_bpm <= bpm <= self.max_bpm:
                candidates.append(bpm)
        if not candidates:
            return self.bpm

        target = self.bpm if self.bpm is not None else self.base_bpm
        estimate = min(candidates, key=lambda value: abs(value - target))
        if self.bpm is None:
            self.bpm = estimate
        else:
            self.bpm = (self.bpm * self.smoothing) + (estimate * (1.0 - self.smoothing))
        return self.bpm

    def time_scale(self) -> float:
        if self.bpm is None or self.bpm <= 0:
            return 1.0
        return max(0.60, min(1.60, self.base_bpm / self.bpm))


class ChordLearner:
    def __init__(self, split: int, chord_window: float, left_pattern_window: float, new_chord_gap: float, pair_window: float):
        self.split = split
        self.chord_window = chord_window
        self.left_pattern_window = left_pattern_window
        self.new_chord_gap = new_chord_gap
        self.pair_window = pair_window
        self.active_left: Dict[int, Tuple[int, float]] = {}
        self.pending_left: List[Tuple[int, int, float]] = []
        self.left_events: List[Tuple[int, int, float]] = []
        self.last_chord: Optional[Tuple[Tuple[int, ...], Tuple[Tuple[int, float, int], ...], float, int, Optional[int], int]] = None
        self.current_bass_pc: Optional[int] = None
        self.examples: Counter[Tuple[int, Optional[int], Tuple[int, ...], Tuple[int, ...], Tuple[Tuple[int, float, int], ...], int, int]] = Counter()

    def feed(self, msg, now: float) -> Optional[str]:
        if msg.type not in ("note_on", "note_off"):
            return None

        velocity = getattr(msg, "velocity", 0)
        is_on = msg.type == "note_on" and velocity > 0
        is_left = msg.note < self.split

        if is_left:
            if is_on:
                self._maybe_start_new_left_pattern(msg.note, now)
                self.active_left[msg.note] = (velocity, now)
                self.pending_left.append((msg.note, velocity, now))
                self.left_events.append((msg.note, velocity, now))
                return self._maybe_capture_chord(now)
            self.active_left.pop(msg.note, None)
            return None

        if is_on:
            return self._pair_melody(msg.note, velocity, now)

        return None

    def _maybe_start_new_left_pattern(self, note: int, now: float) -> None:
        if not self.left_events:
            return
        last_time = self.left_events[-1][2]
        pattern_age = now - self.left_events[0][2]
        current_low = min(event_note for event_note, _vel, _t in self.left_events)
        starts_lower_bass = note <= current_low - 5 and pattern_age > 0.250
        if now - last_time > self.new_chord_gap or starts_lower_bass:
            self.pending_left = []
            self.left_events = []

    def _maybe_capture_chord(self, now: float) -> Optional[str]:
        self.pending_left = [
            item for item in self.pending_left if now - item[2] <= self.chord_window
        ]
        self.left_events = [
            item for item in self.left_events if now - item[2] <= self.left_pattern_window
        ]
        recent_notes = normalize_chord(note for note, _vel, _t in self.pending_left)
        pattern_notes = normalize_chord(note for note, _vel, _t in self.left_events)
        active_notes = normalize_chord(self.active_left.keys())
        notes = pattern_notes if len(pattern_notes) >= 2 else recent_notes if len(recent_notes) >= 2 else active_notes
        if len(notes) < 2:
            return None

        first_time = self.left_events[0][2] if self.left_events else now
        pattern = tuple(
            (note, round(event_time - first_time, 3), vel)
            for note, vel, event_time in self.left_events
            if note in notes
        )
        velocity = round(sum(vel for note, vel, _t in self.left_events if note in notes) / max(1, len(pattern)))
        bass_pc, _intervals = chord_signature(notes)
        previous_bass_pc = self.current_bass_pc
        self.last_chord = (notes, pattern, now, velocity, previous_bass_pc, bass_pc)
        self.current_bass_pc = bass_pc
        return f"Captured chord {chord_name(notes)} [{', '.join(note_name(n) for n in notes)}]"

    def _pair_melody(self, note: int, velocity: int, now: float) -> Optional[str]:
        if self.last_chord is None:
            return None
        notes, pattern, chord_time, chord_velocity, previous_bass_pc, bass_pc = self.last_chord
        if now - chord_time > self.pair_window:
            return None

        _bass_pc, intervals = chord_signature(notes)
        key = (
            note % 12,
            previous_bass_pc,
            notes,
            intervals,
            pattern,
            bass_pc,
            round((velocity + chord_velocity) / 2),
        )
        self.examples[key] += 1
        return f"Learned melody {note_name(note)} -> {chord_name(notes)}"

    def to_model(self) -> dict:
        examples = []
        for (melody_pc, previous_bass_pc, notes, intervals, pattern, bass_pc, velocity), count in self.examples.items():
            examples.append(
                asdict(
                    ChordExample(
                        notes=notes,
                        intervals=intervals,
                        pattern=pattern,
                        bass_pc=bass_pc,
                        melody_pc=melody_pc,
                        previous_bass_pc=previous_bass_pc,
                        velocity=velocity,
                        duration=DEFAULT_CHORD_DURATION,
                        count=count,
                    )
                )
            )
        return {
            "version": 1,
            "split": self.split,
            "left_pattern_window": self.left_pattern_window,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "examples": examples,
        }


class Accompanist:
    def __init__(self, model: dict, output, channel: int, duration: float, comp: str, arp_step: float, note_length: float, retrigger: float, randomize: bool, style: str = "yiruma", transpose: int = 0):
        self.output = output
        self.channel = channel
        self.duration = duration
        self.comp = comp
        self.arp_step = arp_step
        self.note_length = note_length
        self.retrigger = retrigger
        self.randomize = randomize
        self.style = style
        self.transpose = transpose
        self.last_trigger_time: Optional[float] = None
        self.previous_bass_pc: Optional[int] = None
        self.active_chord: List[int] = []
        self.pending_events: List[ScheduledMidiEvent] = []
        self.time_scale = 1.0
        self.current_example: Optional[ChordExample] = None
        self.current_pattern_start = 0.0
        self.by_melody_and_prev: Dict[Tuple[int, Optional[int]], List[ChordExample]] = defaultdict(list)
        self.by_melody: Dict[int, List[ChordExample]] = defaultdict(list)
        self.all_examples: List[ChordExample] = []
        self._load(model)

    def set_time_scale(self, scale: float) -> None:
        self.time_scale = max(0.60, min(1.60, scale))

    def _load(self, model: dict) -> None:
        for item in model.get("examples", []):
            ex = ChordExample(
                notes=tuple(item["notes"]),
                intervals=tuple(item["intervals"]),
                pattern=tuple((event[0], float(event[1]), event[2]) for event in item.get("pattern", [])),
                bass_pc=item["bass_pc"],
                melody_pc=item["melody_pc"],
                previous_bass_pc=item.get("previous_bass_pc"),
                velocity=item.get("velocity", 80),
                duration=item.get("duration", self.duration),
                count=item.get("count", 1),
                token=item.get("token"),
            )
            self.by_melody_and_prev[(ex.melody_pc, ex.previous_bass_pc)].append(ex)
            self.by_melody[ex.melody_pc].append(ex)
            self.all_examples.append(ex)

    def choose(self, melody_note: int) -> Optional[ChordExample]:
        return self.choose_with_reason(melody_note)[0]

    def choose_with_reason(self, melody_note: int) -> Tuple[Optional[ChordExample], str, int]:
        melody_pc = melody_note % 12
        candidates = self.by_melody_and_prev.get((melody_pc, self.previous_bass_pc))
        reason = "melody+previous"
        if not candidates:
            candidates = self.by_melody.get(melody_pc)
            reason = "melody"
        if not candidates:
            candidates = self.all_examples
            reason = "fallback_any"
        if not candidates:
            return None, "none", 0

        if self.randomize:
            weighted = []
            for ex in candidates:
                weighted.extend([ex] * max(1, min(ex.count, 8)))
            return random.choice(weighted), reason, len(candidates)
        return max(candidates, key=lambda ex: (ex.count, len(ex.pattern), -len(ex.notes))), reason, len(candidates)

    def play_for_melody(self, melody_note: int, now: float) -> Tuple[Optional[str], Optional[dict]]:
        if self.last_trigger_time is not None and now - self.last_trigger_time < self.retrigger:
            return None, None
        ex, reason, candidate_count = self.choose_with_reason(melody_note)
        if ex is None:
            return None, None
        self.stop_active()

        # Rebuild chord dynamically if using preset style
        if self.comp == "style" or (self.comp == "learned" and not ex.pattern):
            tok = ex.token or chord_name(ex.notes)
            try:
                ex = chart_token_to_example(tok, ex.duration, "verse", style=self.style)
                ex = ChordExample(
                    notes=ex.notes,
                    intervals=ex.intervals,
                    pattern=ex.pattern,
                    bass_pc=ex.bass_pc,
                    melody_pc=melody_note % 12,
                    previous_bass_pc=self.previous_bass_pc,
                    velocity=ex.velocity,
                    duration=ex.duration,
                    count=1,
                    token=tok,
                )
            except Exception:
                pass

        if self.transpose != 0:
            try:
                transposed_notes = tuple(n + self.transpose for n in ex.notes)
                transposed_pattern = tuple((n + self.transpose, t, v) for n, t, v in ex.pattern)
                ex = ChordExample(
                    notes=transposed_notes,
                    intervals=ex.intervals,
                    pattern=transposed_pattern,
                    bass_pc=(ex.bass_pc + self.transpose) % 12,
                    melody_pc=ex.melody_pc,
                    previous_bass_pc=ex.previous_bass_pc,
                    velocity=ex.velocity,
                    duration=ex.duration,
                    count=ex.count,
                    token=ex.token,
                )
            except Exception:
                pass

        if self.comp == "block":
            self._play_block(ex)
        else:
            self._schedule_pattern(ex, now)
        self.previous_bass_pc = ex.bass_pc
        self.last_trigger_time = now
        decision = {
            "event": "auto_chord",
            "melody_note": melody_note,
            "melody_name": note_name(melody_note),
            "chosen_chord": chord_name(ex.notes),
            "chosen_notes": list(ex.notes),
            "chosen_note_names": [note_name(n) for n in ex.notes],
            "pattern": [list(item) for item in ex.pattern],
            "reason": reason,
            "candidate_count": candidate_count,
            "previous_bass_pc": ex.previous_bass_pc,
            "bass_pc": ex.bass_pc,
            "count": ex.count,
        }
        return f"Auto chord {chord_name(ex.notes)} for melody {note_name(melody_note)} [{reason}, candidates={candidate_count}]", decision

    def play_example(self, ex: ChordExample, now: float, label: str, section: str = "verse") -> dict:
        self.stop_active()

        # Rebuild dynamically if using preset style and has token
        if ex.token and (self.comp == "style" or self.style in {"yiruma", "richard_clayderman", "ludovico_einaudi"}):
            try:
                ex = chart_token_to_example(ex.token, ex.duration, section, style=self.style)
                ex = ChordExample(
                    notes=ex.notes,
                    intervals=ex.intervals,
                    pattern=ex.pattern,
                    bass_pc=ex.bass_pc,
                    melody_pc=ex.melody_pc,
                    previous_bass_pc=self.previous_bass_pc,
                    velocity=ex.velocity,
                    duration=ex.duration,
                    count=ex.count,
                    token=ex.token,
                )
            except Exception:
                pass

        if self.transpose != 0:
            try:
                transposed_notes = tuple(n + self.transpose for n in ex.notes)
                transposed_pattern = tuple((n + self.transpose, t, v) for n, t, v in ex.pattern)
                ex = ChordExample(
                    notes=transposed_notes,
                    intervals=ex.intervals,
                    pattern=transposed_pattern,
                    bass_pc=(ex.bass_pc + self.transpose) % 12,
                    melody_pc=ex.melody_pc,
                    previous_bass_pc=ex.previous_bass_pc,
                    velocity=ex.velocity,
                    duration=ex.duration,
                    count=ex.count,
                    token=ex.token,
                )
            except Exception:
                pass

        if self.comp == "block":
            self._play_block(ex)
        else:
            self._schedule_pattern(ex, now)
        self.previous_bass_pc = ex.bass_pc
        self.last_trigger_time = now
        return {
            "event": "auto_chord",
            "melody_note": None,
            "melody_name": None,
            "chosen_chord": chord_name(ex.notes),
            "chosen_notes": list(ex.notes),
            "chosen_note_names": [note_name(n) for n in ex.notes],
            "pattern": [list(item) for item in ex.pattern],
            "reason": label,
            "candidate_count": 1,
            "previous_bass_pc": ex.previous_bass_pc,
            "bass_pc": ex.bass_pc,
            "count": ex.count,
        }

    def _play_block(self, ex: ChordExample) -> None:
        self.active_chord = list(ex.notes)
        velocity = max(1, min(127, ex.velocity))
        for note in self.active_chord:
            self.output.send(mido.Message("note_on", note=note, velocity=velocity, channel=self.channel))

    def _schedule_pattern(self, ex: ChordExample, now: float) -> None:
        self.pending_events = []
        self._append_pattern(ex, now)
        self.current_example = ex
        self.current_pattern_start = now

    def _append_pattern(self, ex: ChordExample, now: float) -> None:
        pattern = ex.pattern if (self.comp in {"learned", "style"} and ex.pattern) else self._power_pattern(ex.notes, ex.velocity)
        held_notes: List[int] = []
        for note, offset, velocity in pattern:
            out_note = max(0, min(127, int(note)))
            out_vel = max(1, min(127, int(velocity or ex.velocity)))
            held_notes.append(out_note)
            self.pending_events.append(
                ScheduledMidiEvent(time_s=now + (offset * self.time_scale), kind="note_on", note=out_note, velocity=out_vel)
            )
        release_time = now + (max(ex.duration, self.note_length) * self.time_scale)
        for out_note in sorted(set(held_notes)):
            self.pending_events.append(
                ScheduledMidiEvent(time_s=release_time, kind="note_off", note=out_note, velocity=0)
            )
        self.active_chord = sorted(set(held_notes))

    def _power_pattern(self, notes: Tuple[int, ...], velocity: int) -> Tuple[Tuple[int, float, int], ...]:
        ordered = sorted(notes)
        root = ordered[0]
        fifth = next((n for n in ordered[1:] if (n - root) % 12 == 7), ordered[-1])
        octave = root + 12 if root + 12 <= 127 else root
        pattern_notes = [root, fifth, octave, fifth]
        return tuple((note, idx * self.arp_step, velocity) for idx, note in enumerate(pattern_notes))

    def tick(self, now: float) -> None:
        if self.current_example is not None:
            pattern_dur = self.current_example.duration * self.time_scale
            if now >= self.current_pattern_start + pattern_dur - 0.005:
                next_start = self.current_pattern_start + pattern_dur
                if next_start < now:
                    next_start = now
                self.current_pattern_start = next_start
                self._append_pattern(self.current_example, next_start)

        due = [item for item in self.pending_events if item.time_s <= now + 0.0005]
        self.pending_events = [item for item in self.pending_events if item.time_s > now + 0.0005]
        for event in sorted(due, key=lambda item: (item.time_s, 0 if item.kind == "note_off" else 1, item.note)):
            if event.kind == "note_on":
                self.output.send(
                    mido.Message("note_on", note=event.note, velocity=event.velocity, channel=self.channel)
                )
            else:
                self.output.send(
                    mido.Message("note_off", note=event.note, velocity=0, channel=self.channel)
                )

    def stop_active(self) -> None:
        for note in self.active_chord:
            self.output.send(mido.Message("note_off", note=note, velocity=0, channel=self.channel))
        self.active_chord = []
        for event in self.pending_events:
            self.output.send(mido.Message("note_off", note=event.note, velocity=0, channel=self.channel))
        self.pending_events = []
        self.current_example = None


def fuzzy_pick(name: Optional[str], choices: List[str], label: str) -> str:
    if not choices:
        raise SystemExit(f"No MIDI {label} ports found.")
    if not name:
        return choices[0]
    lowered = name.lower()
    matches = [choice for choice in choices if lowered in choice.lower()]
    if not matches:
        raise SystemExit(
            f"Could not find MIDI {label} port matching {name!r}.\nAvailable: "
            + ", ".join(choices)
        )
    return matches[0]


def cmd_ports(_args) -> None:
    require_mido()
    print("MIDI inputs:")
    for name in mido.get_input_names():
        print(f"  - {name}")
    print("\nMIDI outputs:")
    for name in mido.get_output_names():
        print(f"  - {name}")


def cmd_train(args) -> None:
    require_mido()
    input_name = fuzzy_pick(args.input, mido.get_input_names(), "input")
    learner = ChordLearner(args.split, args.chord_window, args.left_pattern_window, args.new_chord_gap, args.pair_window)
    stopped = False

    def handle_stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, handle_stop)
    print(f"Listening on input: {input_name}")
    print(f"Split point: {args.split} ({note_name(args.split)}). Press Ctrl+C to save.")

    started_at = time.monotonic()
    with JsonlLogger(args.event_log) as event_log, mido.open_input(input_name) as port:
        while not stopped:
            now = time.monotonic()
            for msg in port.iter_pending():
                event = midi_event_dict(msg, started_at, now, args.split, "input")
                if event:
                    event_log.write(event)
                    if args.print_notes:
                        print(format_input_event(event, -1))
                line = learner.feed(msg, now)
                if line and args.verbose:
                    print(line)
            time.sleep(0.003)

    model = learner.to_model()
    out_path = Path(args.model)
    out_path.write_text(json.dumps(model, indent=2), encoding="utf-8")
    print(f"\nSaved {len(model['examples'])} learned examples to {out_path}")


def cmd_play(args) -> None:
    require_mido()
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))
    input_name = fuzzy_pick(args.input, mido.get_input_names(), "input")
    progression: List[ChordExample] = []
    lyric_progression: List[Tuple[LyricChordEvent, ChordExample]] = []
    progression_index = 0
    style_val = getattr(args, "style", "yiruma")
    if args.mode == "progression":
        if not args.progression_log:
            raise SystemExit("--mode progression requires --progression-log train_notes.jsonl")
        progression = progression_from_log(args.progression_log, args.split or model.get("split", DEFAULT_SPLIT), args.progression_gap)
        if not progression:
            raise SystemExit(f"No usable left-hand progression found in {args.progression_log}")
        if args.progression_length:
            progression = progression[: args.progression_length]
        progression_index = max(0, args.progression_start - 1)
        print(f"Loaded {len(progression)} progression chords from {args.progression_log}")
        print("Progression preview:")
        for idx, ex in enumerate(progression[:16], 1):
            duration = args.progression_period or (ex.duration * args.progression_tempo)
            print(f"  {idx:02d}. {chord_name(ex.notes):10s} {duration:.2f}s  {_format_pattern(ex.pattern)}")
    elif args.mode == "chart":
        if not args.chart_file:
            raise SystemExit("--mode chart requires --chart-file chart.txt")
        progression = chart_examples_from_file(args.chart_file, args.chart_period, style=style_val)
        if not progression:
            raise SystemExit(f"No usable chord tokens found in {args.chart_file}")
        progression_index = max(0, args.chart_start - 1)
        print(f"Loaded {len(progression)} chart chords from {args.chart_file}")
        print("Chart preview:")
        for idx, ex in enumerate(progression[:16], 1):
            print(f"  {idx:02d}. {chord_name(ex.notes):10s} {ex.duration:.2f}s  {_format_pattern(ex.pattern)}")
    elif args.mode == "lyric":
        if not args.lyric_file:
            raise SystemExit("--mode lyric requires --lyric-file lyric_timeline.txt")
        lyric_progression = lyric_events_to_examples(
            lyric_events_from_file(args.lyric_file, args.chart_period),
            args.chart_period,
            style=style_val
        )
        if not lyric_progression:
            raise SystemExit(f"No usable lyric events found in {args.lyric_file}")
        progression = [item[1] for item in lyric_progression]
        progression_index = max(0, args.chart_start - 1)
        print(f"Loaded {len(lyric_progression)} lyric events from {args.lyric_file}")
        print("Lyric preview:")
        for idx, (event, ex) in enumerate(lyric_progression[:16], 1):
            print(
                f"  {idx:02d}. {event.time_s:6.2f}s {event.chord:6s} "
                f"{event.lyric}"
            )
 
    if args.output:
        output_context = mido.open_output(args.output)
        output_label = args.output
    else:
        output_context = mido.open_output(args.virtual_name, virtual=True)
        output_label = f"{args.virtual_name} (virtual)"
 
    stopped = False
 
    def handle_stop(_signum, _frame):
        nonlocal stopped
        stopped = True
 
    signal.signal(signal.SIGINT, handle_stop)
    print(f"Listening on input: {input_name}")
    print(f"Sending chords to: {output_label}")
    print(f"Mode: {args.mode}; trigger: {args.progression_trigger}; control note: {note_name(args.control_note)} ({args.control_note})")
    if args.mode in {"progression", "chart", "lyric"} and args.progression_trigger == "control-note":
        print(f"Muted control note: {note_name(args.control_note)} ({args.control_note})")
        if args.learn_control_note:
            print("Learning control note: press the key you want to use for NEXT CHORD once.")
        print("Melody forwarding: ON through virtual output.")
        print("If C#2 still makes sound, GarageBand is also listening directly to the controller.")
    if args.mode in {"progression", "chart", "lyric"} and args.progression_trigger == "pitch-up":
        print(f"Pitch trigger threshold: {args.pitch_threshold}")
    if args.mode in {"progression", "chart", "lyric"} and args.progression_trigger == "cc-down":
        print(
            f"CC trigger: CC {args.cc_control} arms at >= {args.cc_arm_value}, "
            f"advances at <= {args.cc_trigger_value}"
        )
    if args.track_bpm:
        print(f"BPM tracking: ON, base={args.base_bpm:g}, range={args.bpm_min:g}-{args.bpm_max:g}")
    print("Press Ctrl+C to stop.")
 
    split = args.split if args.split is not None else model.get("split", DEFAULT_SPLIT)
    started_at = time.monotonic()
    with JsonlLogger(args.event_log) as event_log, mido.open_input(input_name) as in_port, output_context as out_port:
        accompanist = Accompanist(
            model,
            out_port,
            args.channel,
            args.duration,
            args.comp,
            args.arp_step,
            args.note_length,
            args.retrigger,
            args.randomize,
            style=style_val,
            transpose=args.transpose,
        )
        progression_started = False
        next_progression_time: Optional[float] = None
        pedal_was_down = False
        pitch_trigger_armed = True
        cc_trigger_armed = False
        learned_control_note = not args.learn_control_note
        bpm_tracker = (
            BpmTracker(args.base_bpm, args.bpm_min, args.bpm_max, args.bpm_smoothing)
            if args.track_bpm
            else None
        )
        current_bpm: Optional[float] = None
 
        def advance_progression(now: float, trigger_label: str, melody_note: Optional[int] = None):
            nonlocal progression_index, progression_started, next_progression_time, current_bpm
            current_idx = progression_index % len(progression)
            ex = progression[current_idx]
            progression_index += 1
            if bpm_tracker:
                accompanist.set_time_scale(bpm_tracker.time_scale())
            lyric_text = None
            lyric_section = "verse"
            if lyric_progression:
                lyric_text = lyric_progression[current_idx][0].lyric
                lyric_section = lyric_progression[current_idx][0].section
            decision = accompanist.play_example(ex, now, f"{trigger_label}:{progression_index}", section=lyric_section)
            if melody_note is not None:
                decision["melody_note"] = melody_note
                decision["melody_name"] = note_name(melody_note)
            if lyric_progression:
                decision["lyric"] = lyric_text
                decision["section"] = lyric_section
            decision["t"] = round(now - started_at, 4)
            if current_bpm:
                decision["bpm"] = round(current_bpm, 1)
                decision["time_scale"] = round(accompanist.time_scale, 3)
            event_log.write(decision)
            progression_started = True
            duration = args.progression_period or (ex.duration * args.progression_tempo * accompanist.time_scale)
            next_progression_time = now + duration
            if args.verbose:
                note_part = f" for melody {note_name(melody_note)}" if melody_note is not None else ""
                lyric_part = f" | {lyric_text}" if lyric_text else ""
                bpm_part = f" @{current_bpm:.0f}bpm" if current_bpm else ""
                if args.mode == "lyric" and lyric_text:
                    section_part = f"[{lyric_section}] " if lyric_section else ""
                    print(f"{progression_index:02d}. {section_part}{chord_name(ex.notes):8s}{bpm_part} | {lyric_text}")
                else:
                    print(f"Progression {trigger_label} chord {progression_index:02d} {chord_name(ex.notes)}{bpm_part}{note_part}{lyric_part}")
        while not stopped:
            now = time.monotonic()
            for msg in in_port.iter_pending():
                suppress_control_note = (
                    args.mode in {"progression", "chart", "lyric"}
                    and args.progression_trigger == "control-note"
                    and getattr(msg, "note", None) == args.control_note
                )
                event = midi_event_dict(msg, started_at, now, split, "input")
                if event:
                    event_log.write(event)
                    if args.print_notes:
                        print(format_input_event(event, args.control_note))
                velocity = getattr(msg, "velocity", 0)
                if (
                    args.mode in {"progression", "chart", "lyric"}
                    and args.progression_trigger == "control-note"
                    and args.learn_control_note
                    and not learned_control_note
                    and msg.type == "note_on"
                    and velocity > 0
                    and getattr(msg, "note", None) is not None
                ):
                    args.control_note = msg.note
                    learned_control_note = True
                    print(f"Learned control note: {note_name(args.control_note)} ({args.control_note}). Press it again to advance chords.")
                    continue
                if suppress_control_note:
                    if msg.type == "note_on" and velocity > 0:
                        advance_progression(now, "control-note")
                    continue
                if args.mode in {"progression", "chart", "lyric"} and msg.type == "control_change" and msg.control == 64:
                    pedal_down = msg.value >= 64
                    if args.progression_trigger == "pedal" and pedal_down and not pedal_was_down:
                        advance_progression(now, "pedal")
                    pedal_was_down = pedal_down
                    continue
                if (
                    args.mode in {"progression", "chart", "lyric"}
                    and args.progression_trigger == "cc-down"
                    and msg.type == "control_change"
                    and msg.control == args.cc_control
                ):
                    value = getattr(msg, "value", 0)
                    if value >= args.cc_arm_value:
                        cc_trigger_armed = True
                    elif value <= args.cc_trigger_value and cc_trigger_armed:
                        advance_progression(now, "cc-down")
                        cc_trigger_armed = False
                    continue
                if (
                    args.mode in {"progression", "chart", "lyric"}
                    and args.progression_trigger == "pitch-up"
                    and msg.type == "pitchwheel"
                ):
                    pitch = getattr(msg, "pitch", 0)
                    if pitch <= args.pitch_reset:
                        pitch_trigger_armed = True
                    elif pitch >= args.pitch_threshold and pitch_trigger_armed:
                        advance_progression(now, "pitch-up")
                        pitch_trigger_armed = False
                    continue
                if msg.type == "note_on" and velocity > 0 and msg.note >= split:
                    line, decision = None, None
                    if bpm_tracker and not suppress_control_note:
                        current_bpm = bpm_tracker.feed_note_on(now)
                    if args.mode in {"progression", "chart", "lyric"} and args.progression_trigger == "clock":
                        if not progression_started:
                            advance_progression(now, "clock-start", msg.note)
                    elif args.mode in {"progression", "chart", "lyric"} and args.progression_trigger == "note":
                        if (
                            accompanist.last_trigger_time is not None
                            and now - accompanist.last_trigger_time < args.retrigger
                        ):
                            line, decision = None, None
                        else:
                            advance_progression(now, "note", msg.note)
                            line, decision = None, None
                    elif args.mode not in {"progression", "chart", "lyric"}:
                        line, decision = accompanist.play_for_melody(msg.note, now)
                    if decision:
                        decision["t"] = round(now - started_at, 4)
                        event_log.write(decision)
                    if line and args.verbose:
                        print(line)
                    if args.mode in {"progression", "chart", "lyric"}:
                        out_port.send(msg)
                elif (
                    args.mode in {"progression", "chart", "lyric"}
                    and msg.type in ("note_off", "note_on")
                    and getattr(msg, "note", 127) >= split
                ):
                    out_port.send(msg)
                elif msg.type in ("note_off", "note_on") and getattr(msg, "note", 127) < split:
                    # In performance mode, left hand can still override naturally.
                    out_port.send(msg)
            if (
                args.mode in {"progression", "chart", "lyric"}
                and args.progression_trigger == "clock"
                and progression_started
                and next_progression_time is not None
                and now >= next_progression_time
            ):
                advance_progression(now, "clock")
            accompanist.tick(time.monotonic())
            time.sleep(0.003)
        accompanist.stop_active()


def cmd_record(args) -> None:
    require_mido()
    input_name = fuzzy_pick(args.input, mido.get_input_names(), "input")
    stopped = False

    def handle_stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, handle_stop)
    print(f"Recording input: {input_name}")
    print(f"Writing note log to: {args.out}")
    print(f"Split point: {args.split} ({note_name(args.split)}). Press Ctrl+C to stop.")

    counts = Counter()
    started_at = time.monotonic()
    with JsonlLogger(args.out) as event_log, mido.open_input(input_name) as port:
        while not stopped:
            now = time.monotonic()
            for msg in port.iter_pending():
                event = midi_event_dict(msg, started_at, now, args.split, "input")
                if not event:
                    continue
                event_log.write(event)
                hand = event.get("hand", "misc")
                counts[(hand, event["event"])] += 1
                print(format_input_event(event, -1))
            time.sleep(0.003)
    print("\nRecorded:")
    for (hand, event), count in sorted(counts.items()):
        print(f"  {hand:5s} {event:8s}: {count}")


def _load_jsonl(path: str) -> List[dict]:
    rows = []
    with Path(path).open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def progression_from_log(path: str, split: int, gap: float) -> List[ChordExample]:
    rows = _load_jsonl(path)
    left_ons = [
        row for row in rows
        if row.get("event") == "note_on"
        and row.get("note", 127) < split
        and row.get("velocity", 0) > 0
    ]
    clusters: List[List[dict]] = []
    current: List[dict] = []
    last_time: Optional[float] = None
    for row in left_ons:
        if last_time is None or row["t"] - last_time <= gap:
            current.append(row)
        else:
            clusters.append(current)
            current = [row]
        last_time = row["t"]
    if current:
        clusters.append(current)

    usable_clusters: List[List[dict]] = []
    for cluster in clusters:
        span = cluster[-1]["t"] - cluster[0]["t"]
        if len(cluster) < 2 or span > 1.35:
            continue
        notes = normalize_chord(row["note"] for row in cluster)
        if len(notes) < 2:
            continue
        usable_clusters.append(cluster)

    examples: List[ChordExample] = []
    previous_bass_pc: Optional[int] = None
    for idx, cluster in enumerate(usable_clusters):
        notes = normalize_chord(row["note"] for row in cluster)
        bass_pc, intervals = chord_signature(notes)
        first_time = cluster[0]["t"]
        if idx + 1 < len(usable_clusters):
            duration = max(0.25, usable_clusters[idx + 1][0]["t"] - first_time)
        elif idx > 0:
            duration = max(0.25, first_time - usable_clusters[idx - 1][0]["t"])
        else:
            duration = DEFAULT_CHORD_DURATION
        pattern = tuple(
            (row["note"], round(row["t"] - first_time, 3), row["velocity"])
            for row in cluster
        )
        velocity = round(sum(row["velocity"] for row in cluster) / len(cluster))
        examples.append(
            ChordExample(
                notes=notes,
                intervals=intervals,
                pattern=pattern,
                bass_pc=bass_pc,
                melody_pc=-1,
                previous_bass_pc=previous_bass_pc,
                velocity=velocity,
                duration=duration,
                count=1,
            )
        )
        previous_bass_pc = bass_pc
    return examples


def parse_chart_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.replace("|", " ")
        for token in line.split():
            if token in {"-", "_"}:
                continue
            tokens.append(token)
    return tokens


def chart_token_to_example(token: str, duration: float, section: str = "verse", style: str = "yiruma") -> ChordExample:
    cleaned = token.strip()
    if not cleaned:
        raise ValueError("Empty chord token")
    root = cleaned[0].upper()
    if len(cleaned) >= 2 and cleaned[1] in {"#", "b"}:
        root += cleaned[1]
        quality = cleaned[2:]
    else:
        quality = cleaned[1:]
    if root not in NOTE_TO_PC:
        raise ValueError(f"Unsupported chord root: {token}")
    is_minor = quality.lower().startswith("m") and not quality.lower().startswith("maj")
    root_pc = NOTE_TO_PC[root]
    base_root = 36 + root_pc
    while base_root < 36:
        base_root += 12
    bass_fifth = base_root + 7
    bass_octave = base_root + 12
    right_root = 60 + root_pc
    while right_root > 67:
        right_root -= 12
    while right_root < 55:
        right_root += 12
    right_third = right_root + (3 if is_minor else 4)
    right_fifth = right_root + 7
    right_ninth = right_root + 14
    top_root = right_root + 12
    notes = normalize_chord([base_root, bass_fifth, bass_octave, right_third, right_fifth, top_root, right_ninth])
    
    # Normalize section
    sec = section.lower().strip()
    if sec in {"dk", "diep khuc", "chorus"}:
        sec_type = "chorus"
    elif sec in {"intro", "dao dau", "opening", "dạo đầu"}:
        sec_type = "intro"
    else:
        sec_type = "verse"

    # Pattern selection based on style and section
    style_lower = style.lower().strip()
    
    if style_lower == "richard_clayderman" or style_lower == "clayderman":
        if sec_type == "intro":
            pattern = (
                (base_root, 0.00, 75),
                (bass_octave, 0.05, 70),
                (right_third, 0.30, 58),
                (right_fifth, 0.45, 60),
                (top_root, 0.60, 62),
                (right_ninth, 0.80, 56),
            )
        elif sec_type == "chorus":
            pattern = (
                (base_root, 0.00, 90),
                (bass_octave, 0.05, 85),
                (bass_fifth, 0.20, 70),
                (right_third, 0.40, 74),
                (right_fifth, 0.45, 72),
                (top_root, 0.50, 76),
                (right_third, 0.80, 80),
                (right_fifth, 0.80, 80),
                (top_root, 0.80, 82),
                (right_ninth, 0.80, 78),
                (right_fifth, 1.10, 66),
                (right_third, 1.25, 64),
                (bass_fifth, 1.45, 68),
            )
        else: # verse/default
            pattern = (
                (base_root, 0.00, 80),
                (bass_octave, 0.05, 75),
                (right_third, 0.40, 64),
                (right_fifth, 0.40, 64),
                (top_root, 0.40, 66),
                (right_third, 0.80, 58),
                (right_fifth, 0.80, 58),
                (top_root, 0.80, 60),
                (bass_fifth, 1.20, 50),
            )
    elif style_lower == "ludovico_einaudi" or style_lower == "einaudi":
        if sec_type == "intro":
            pattern = (
                (base_root, 0.00, 68),
                (bass_fifth, 0.25, 60),
                (bass_octave, 0.50, 62),
                (bass_fifth, 0.75, 58),
                (base_root, 1.00, 64),
                (bass_fifth, 1.25, 56),
                (bass_octave, 1.50, 58),
                (bass_fifth, 1.75, 54),
            )
        elif sec_type == "chorus":
            pattern = (
                (base_root, 0.00, 84),
                (bass_fifth, 0.15, 72),
                (bass_octave, 0.30, 75),
                (right_third, 0.45, 68),
                (right_fifth, 0.60, 70),
                (top_root, 0.75, 74),
                (right_ninth, 0.90, 66),
                (top_root, 1.05, 68),
                (right_fifth, 1.20, 62),
                (right_third, 1.35, 60),
                (bass_octave, 1.50, 64),
                (bass_fifth, 1.65, 58),
            )
        else: # verse/default
            pattern = (
                (base_root, 0.00, 74),
                (bass_fifth, 0.20, 62),
                (bass_octave, 0.40, 65),
                (right_third, 0.60, 58),
                (right_fifth, 0.80, 60),
                (right_third, 1.00, 56),
                (bass_octave, 1.20, 58),
                (bass_fifth, 1.40, 54),
            )
    else: # yiruma (default)
        if sec_type == "intro":
            pattern = (
                (base_root, 0.00, 70),
                (bass_fifth, 0.30, 55),
                (bass_octave, 0.60, 58),
                (right_third, 0.90, 52),
                (right_fifth, 1.20, 54),
                (bass_octave, 1.50, 48),
            )
        elif sec_type == "chorus":
            pattern = (
                (base_root, 0.00, 86),
                (bass_octave, 0.12, 72),
                (bass_fifth, 0.30, 62),
                (right_third, 0.48, 66),
                (right_fifth, 0.60, 64),
                (top_root, 0.72, 70),
                (right_ninth, 0.98, 58),
                (top_root, 1.20, 62),
                (right_fifth, 1.42, 52),
                (right_third, 1.58, 48),
            )
        else: # verse/default
            pattern = (
                (base_root, 0.00, 76),
                (bass_fifth, 0.22, 58),
                (bass_octave, 0.44, 62),
                (right_third, 0.66, 54),
                (right_fifth, 0.88, 56),
                (top_root, 1.10, 60),
                (right_ninth, 1.32, 46),
                (right_fifth, 1.54, 42),
            )

    intervals = tuple((n - base_root) % 12 for n in notes)
    return ChordExample(
        notes=notes,
        intervals=intervals,
        pattern=pattern,
        bass_pc=root_pc,
        melody_pc=-1,
        previous_bass_pc=None,
        velocity=pattern[0][2] if pattern else 74,
        duration=duration,
        count=1,
        token=token,
    )


def chart_examples_from_file(path: str, duration: float, style: str = "yiruma") -> List[ChordExample]:
    text = Path(path).read_text(encoding="utf-8")
    tokens = parse_chart_tokens(text)
    examples: List[ChordExample] = []
    previous_bass_pc: Optional[int] = None
    for token in tokens:
        ex = chart_token_to_example(token, duration, style=style)
        ex = ChordExample(
            notes=ex.notes,
            intervals=ex.intervals,
            pattern=ex.pattern,
            bass_pc=ex.bass_pc,
            melody_pc=ex.melody_pc,
            previous_bass_pc=previous_bass_pc,
            velocity=ex.velocity,
            duration=ex.duration,
            count=ex.count,
            token=token,
        )
        examples.append(ex)
        previous_bass_pc = ex.bass_pc
    return examples


def parse_timecode(raw: str) -> float:
    token = raw.strip()
    if ":" in token:
        minute_part, second_part = token.split(":", 1)
        return int(minute_part) * 60 + float(second_part)
    return float(token)


def lyric_events_from_file(path: str, duration: float) -> List[LyricChordEvent]:
    events: List[LyricChordEvent] = []
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 3)
        if len(parts) not in {3, 4}:
            raise ValueError(f"Expected 'time|chord|lyric' or 'time|chord|section|lyric' format, got: {line}")
        time_s = parse_timecode(parts[0])
        chord = parts[1].strip()
        if len(parts) == 4:
            section = parts[2].strip() or "verse"
            lyric = parts[3].strip()
        else:
            section = "verse"
            lyric = parts[2].strip()
        events.append(LyricChordEvent(time_s=time_s, chord=chord, lyric=lyric, section=section))
    events.sort(key=lambda item: item.time_s)
    if not events:
        return events
    normalized: List[LyricChordEvent] = []
    first = events[0].time_s
    for event in events:
        normalized.append(
            LyricChordEvent(
                time_s=max(0.0, event.time_s - first),
                chord=event.chord,
                lyric=event.lyric,
                section=event.section,
            )
        )
    return normalized


def lyric_events_to_examples(events: List[LyricChordEvent], default_duration: float, style: str = "yiruma") -> List[Tuple[LyricChordEvent, ChordExample]]:
    items: List[Tuple[LyricChordEvent, ChordExample]] = []
    previous_bass_pc: Optional[int] = None
    for idx, event in enumerate(events):
        if idx + 1 < len(events):
            duration = max(0.25, events[idx + 1].time_s - event.time_s)
        else:
            duration = default_duration
        ex = chart_token_to_example(event.chord, duration, event.section, style=style)
        ex = ChordExample(
            notes=ex.notes,
            intervals=ex.intervals,
            pattern=ex.pattern,
            bass_pc=ex.bass_pc,
            melody_pc=ex.melody_pc,
            previous_bass_pc=previous_bass_pc,
            velocity=ex.velocity,
            duration=duration,
            count=ex.count,
            token=event.chord,
        )
        items.append((event, ex))
        previous_bass_pc = ex.bass_pc
    return items


def hopamchuan_lines_from_html(html_text: str) -> List[str]:
    match = re.search(r'<div id="song-lyric".*?<div id="song-leftover-space"', html_text, re.S)
    if not match:
        raise ValueError("Could not locate song lyric block in HTML")
    block = match.group(0)
    line_matches = re.findall(r'<div class="chord_lyric_line(?: [^"]*)?">(.*?)</div>', block, re.S)
    lines: List[str] = []
    chord_pattern = re.compile(
        r'<span class="hopamchuan_chord_inline"><i>\[</i><span class="hopamchuan_chord">([^<]+)</span><i>\]</i></span>'
    )
    lyric_pattern = re.compile(r'<span class="hopamchuan_lyric">([^<]*)</span>')
    for line_html in line_matches:
        if "empty_line" in line_html:
            lines.append("")
            continue
        rendered = chord_pattern.sub(lambda m: f"[{m.group(1)}]", line_html)
        rendered = lyric_pattern.sub(lambda m: m.group(1), rendered)
        rendered = re.sub(r"<[^>]+>", "", rendered)
        rendered = html.unescape(rendered).replace("\xa0", " ").strip()
        lines.append(rendered)
    return lines


def chord_events_from_inline_line(line: str) -> List[Tuple[str, str]]:
    events: List[Tuple[str, str]] = []
    prefix_match = re.match(r"([^\[]*)", line)
    prefix = prefix_match.group(1).strip() if prefix_match else ""
    for index, (chord, lyric) in enumerate(re.findall(r"\[([^\]]+)\]([^\[]*)", line)):
        lyric_text = lyric.strip()
        if index == 0 and prefix:
            lyric_text = f"{prefix} {lyric_text}".strip()
        events.append((chord.strip(), lyric_text or "..."))
    return events


def lyric_events_from_inline_lines(lines: List[str], period: float) -> List[LyricChordEvent]:
    events: List[LyricChordEvent] = []
    current_time = 0.0
    section = "verse"
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        normalized = stripped.lower().replace("đ", "d")
        if normalized.startswith("dk") or normalized.startswith("diep khuc"):
            section = "chorus"
            continue
        if "[" not in stripped or "]" not in stripped:
            continue
        for chord, lyric in chord_events_from_inline_line(stripped):
            events.append(LyricChordEvent(time_s=current_time, chord=chord, lyric=lyric, section=section))
            current_time += period
    return events


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Codex Piano Chord Learner"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


def format_input_event(event: dict, control_note: int) -> str:
    marker = ""
    if event.get("note") == control_note:
        marker = " CONTROL"
    if event["event"] in {"note_on", "note_off"}:
        return (
            f"{event['t']:8.3f}s {event['hand']:5s} {event['event']:14s} "
            f"{event['name']:4s} note={event['note']:3d} vel={event['velocity']:3d}{marker}"
        )
    if event["event"] == "control_change":
        return (
            f"{event['t']:8.3f}s ctrl  control_change CC={event['control']:3d} "
            f"value={event['value']:3d}"
        )
    if event["event"] == "pitchwheel":
        return f"{event['t']:8.3f}s bend  pitchwheel      pitch={event['pitch']:5d}"
    return f"{event['t']:8.3f}s {event['event']}"


def _format_pattern(pattern: Iterable[Iterable]) -> str:
    parts = []
    for item in pattern:
        note, offset, velocity = item
        parts.append(f"{note_name(int(note))}@{float(offset):.2f}s/v{int(velocity)}")
    return ", ".join(parts) if parts else "(no pattern)"


def cmd_analyze(args) -> None:
    if args.model:
        model = json.loads(Path(args.model).read_text(encoding="utf-8"))
        examples = model.get("examples", [])
        print(f"Model: {args.model}")
        print(f"Examples: {len(examples)}")
        print(f"Split: {model.get('split', DEFAULT_SPLIT)} ({note_name(model.get('split', DEFAULT_SPLIT))})")
        print(f"Examples with learned pattern: {sum(1 for ex in examples if ex.get('pattern'))}")

        chord_counts = Counter()
        melody_chords: Dict[int, Counter] = defaultdict(Counter)
        transitions = Counter()
        pattern_lengths = Counter()
        for ex in examples:
            chord = chord_name(ex.get("notes", []))
            count = ex.get("count", 1)
            chord_counts[chord] += count
            melody_chords[ex["melody_pc"]][chord] += count
            transitions[(ex.get("previous_bass_pc"), ex.get("bass_pc"))] += count
            pattern_lengths[len(ex.get("pattern", []))] += count

        print("\nTop chords learned:")
        for chord, count in chord_counts.most_common(12):
            print(f"  {chord:18s} {count}")

        print("\nMelody note ambiguity:")
        for pc, counter in sorted(melody_chords.items()):
            total = sum(counter.values())
            top = ", ".join(f"{chord}:{count}" for chord, count in counter.most_common(6))
            warning = "  <-- ambiguous" if len(counter) >= args.ambiguous_threshold else ""
            print(f"  {NOTE_NAMES[pc]:2s} total={total:3d} chords={len(counter):2d}: {top}{warning}")

        print("\nPattern lengths:")
        for length, count in sorted(pattern_lengths.items()):
            print(f"  {length} note-ons: {count}")

        print("\nTop bass transitions:")
        for (prev_pc, bass_pc), count in transitions.most_common(12):
            prev = "None" if prev_pc is None else NOTE_NAMES[int(prev_pc)]
            bass = "None" if bass_pc is None else NOTE_NAMES[int(bass_pc)]
            print(f"  {prev:4s} -> {bass:4s}: {count}")

        if args.show_examples:
            print("\nExample details:")
            for ex in sorted(examples, key=lambda item: item.get("count", 1), reverse=True)[: args.show_examples]:
                print(
                    f"  melody={NOTE_NAMES[ex['melody_pc']]:2s} chord={chord_name(ex['notes']):18s} "
                    f"count={ex.get('count', 1):2d} pattern={_format_pattern(ex.get('pattern', []))}"
                )

    if args.log:
        rows = _load_jsonl(args.log)
        note_rows = [row for row in rows if row.get("event") in ("note_on", "note_off")]
        decision_rows = [row for row in rows if row.get("event") == "auto_chord"]
        print(f"\nLog: {args.log}")
        print(f"Note events: {len(note_rows)}")
        print(f"Auto chord decisions: {len(decision_rows)}")
        note_counts = Counter((row.get("hand"), row.get("name"), row.get("event")) for row in note_rows)
        print("\nMost common note events:")
        for (hand, name, event), count in note_counts.most_common(20):
            print(f"  {hand:5s} {name:4s} {event:8s}: {count}")
        if decision_rows:
            print("\nAuto chord decisions:")
            for row in decision_rows[: args.show_decisions]:
                print(
                    f"  {row['t']:8.3f}s melody={row['melody_name']:4s} -> "
                    f"{row['chosen_chord']:18s} reason={row['reason']} candidates={row['candidate_count']}"
                )


def cmd_import_hopamchuan(args) -> None:
    html_text = fetch_text(args.url)
    lines = hopamchuan_lines_from_html(html_text)
    inline_lines = [line for line in lines if line.strip()]
    events = lyric_events_from_inline_lines(inline_lines, args.period)

    inline_path = Path(args.inline_out)
    timeline_path = Path(args.timeline_out)
    inline_path.write_text("\n".join(inline_lines) + "\n", encoding="utf-8")
    timeline_lines = ["# time|chord|section|lyric"]
    for event in events:
        minutes = int(event.time_s // 60)
        seconds = event.time_s - (minutes * 60)
        timeline_lines.append(f"{minutes:02d}:{seconds:05.2f}|{event.chord}|{event.section}|{event.lyric}")
    timeline_path.write_text("\n".join(timeline_lines) + "\n", encoding="utf-8")

    print(f"Fetched {len(inline_lines)} lyric lines from {args.url}")
    print(f"Wrote inline chord text to {inline_path}")
    print(f"Wrote {len(events)} lyric chord events to {timeline_path}")


class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.input_port_name = None
        self.output_port_name = None
        self.split = DEFAULT_SPLIT
        self.comp = "style"
        self.style = "yiruma"
        self.mode = "lyric"
        self.transpose = 0
        
        self.progression_tempo = 1.0
        self.progression_period = None
        self.retrigger = 0.450
        self.model_path = "my_style.json"
        
        # Options to support all play triggers in UI mode
        self.progression_trigger = "cc-down"
        self.control_note = 21
        self.learn_control_note = False
        self.pitch_threshold = 6000
        self.pitch_reset = 1000
        self.cc_control = 1
        self.cc_arm_value = 60
        self.cc_trigger_value = 30

        
        self.is_playing = False
        self.playback_thread = None
        self.playback_stop_event = threading.Event()
        self.next_triggered = False
        
        # State of progression playback
        self.progression = []
        self.lyric_progression = []
        self.progression_index = 0
        self.current_bpm = None
        self.time_scale = 1.0
        
        # HopAmChuan scraper state
        self.fetched_url = None
        self.fetched_inline_lines = []
        self.fetched_events = []
        
        # Event subscriptions (for SSE)
        self.subscribers = []
        self.subscribers_lock = threading.Lock()


global_state = AppState()


class MidiOutputWrapper:
    def __init__(self, target_output, state: AppState, source: str):
        self.target_output = target_output
        self.state = state
        self.source = source
        
    def send(self, msg) -> None:
        self.target_output.send(msg)
        # Broadcast note-on and note-off events to SSE UI
        event = midi_event_dict(msg, time.monotonic(), time.monotonic(), self.state.split, self.source)
        if event:
            broadcast_sse(self.state, "midi_event", event)


def broadcast_sse(state: AppState, event_type: str, data: dict):
    payload = json.dumps({"event": event_type, "data": data})
    msg = f"data: {payload}\n\n"
    with state.subscribers_lock:
        closed = []
        for queue_obj in state.subscribers:
            try:
                queue_obj.put_nowait(msg)
            except Exception:
                closed.append(queue_obj)
        for queue_obj in closed:
            if queue_obj in state.subscribers:
                state.subscribers.remove(queue_obj)


def start_midi_engine():
    with global_state.lock:
        if global_state.is_playing:
            return
        global_state.is_playing = True
        global_state.playback_stop_event.clear()
        global_state.next_triggered = False
        global_state.playback_thread = threading.Thread(target=midi_engine_loop, args=(global_state,), daemon=True)
        global_state.playback_thread.start()
    broadcast_sse(global_state, "status", {"is_playing": True})


def stop_midi_engine():
    with global_state.lock:
        if not global_state.is_playing:
            return
        global_state.playback_stop_event.set()
        thread = global_state.playback_thread
    if thread:
        thread.join(timeout=2.0)
    with global_state.lock:
        global_state.is_playing = False
        global_state.playback_thread = None
    broadcast_sse(global_state, "status", {"is_playing": False})


def midi_engine_loop(state: AppState):
    try:
        try:
            model = json.loads(Path(state.model_path).read_text(encoding="utf-8"))
        except Exception:
            model = {"examples": [], "split": state.split}
            
        input_name = state.input_port_name
        output_name = state.output_port_name
        
        if not input_name:
            broadcast_sse(state, "error", {"message": "No MIDI input port selected."})
            return
            
        resolved_in = fuzzy_pick(input_name, mido.get_input_names(), "input")
        if output_name:
            resolved_out = fuzzy_pick(output_name, mido.get_output_names(), "output")
            raw_out = mido.open_output(resolved_out)
        else:
            raw_out = mido.open_output("ChordLearner Out Web", virtual=True)
            
        out_port = MidiOutputWrapper(raw_out, state, "output")
        started_at = time.monotonic()
        
        with mido.open_input(resolved_in) as in_port, raw_out:
            accompanist = Accompanist(
                model,
                out_port,
                0, # channel
                DEFAULT_CHORD_DURATION,
                state.comp,
                DEFAULT_ARP_STEP,
                DEFAULT_NOTE_LENGTH,
                state.retrigger,
                False, # randomize
                style=state.style,
            )
            
            progression_started = False
            next_progression_time: Optional[float] = None
            pedal_was_down = False
            pitch_trigger_armed = True
            cc_trigger_armed = False
            local_idx = 0
            
            def trigger_next(now_t: float, trigger_lbl: str, note_val: Optional[int] = None):
                nonlocal local_idx, progression_started, next_progression_time
                with state.lock:
                    prog_len = len(state.progression)
                    if prog_len == 0:
                        return
                    current_idx = state.progression_index % prog_len
                    ex = state.progression[current_idx]
                    state.progression_index += 1
                    local_idx = state.progression_index
                    
                    lyric_text = None
                    lyric_section = "verse"
                    if len(state.lyric_progression) > current_idx:
                        lyric_text = state.lyric_progression[current_idx][0].lyric
                        lyric_section = state.lyric_progression[current_idx][0].section
                        
                accompanist.comp = state.comp
                accompanist.style = state.style
                accompanist.retrigger = state.retrigger
                accompanist.transpose = state.transpose
                
                decision = accompanist.play_example(ex, now_t, f"{trigger_lbl}:{local_idx}", section=lyric_section)
                if note_val is not None:
                    decision["melody_note"] = note_val
                    decision["melody_name"] = note_name(note_val)
                    
                decision["lyric"] = lyric_text
                decision["section"] = lyric_section
                decision["t"] = round(now_t - started_at, 4)
                decision["progression_index"] = local_idx
                
                broadcast_sse(state, "decision", decision)
                progression_started = True
                
                duration = ex.duration * state.progression_tempo * accompanist.time_scale
                next_progression_time = now_t + duration

            while not state.playback_stop_event.is_set():
                now = time.monotonic()
                with state.lock:
                    manual_next = state.next_triggered
                    if manual_next:
                        state.next_triggered = False
                if manual_next:
                    trigger_next(now, "web-ui")
                    
                learned_control_note = not state.learn_control_note
                
                for msg in in_port.iter_pending():
                    suppress_control_note = (
                        state.mode in {"progression", "chart", "lyric"}
                        and state.progression_trigger == "control-note"
                        and getattr(msg, "note", None) == state.control_note
                    )
                    
                    event = midi_event_dict(msg, started_at, now, state.split, "input")
                    if event:
                        broadcast_sse(state, "midi_event", event)
                        
                    velocity = getattr(msg, "velocity", 0)
                    
                    if (
                        state.mode in {"progression", "chart", "lyric"}
                        and state.progression_trigger == "control-note"
                        and state.learn_control_note
                        and not learned_control_note
                        and msg.type == "note_on"
                        and velocity > 0
                        and getattr(msg, "note", None) is not None
                    ):
                        state.control_note = msg.note
                        learned_control_note = True
                        broadcast_sse(state, "log", {"message": f"Learned control note: {note_name(state.control_note)} ({state.control_note})"})
                        continue
                        
                    if suppress_control_note:
                        if msg.type == "note_on" and velocity > 0:
                            trigger_next(now, "control-note")
                        continue
                        
                    if state.mode in {"progression", "chart", "lyric"} and msg.type == "control_change" and msg.control == 64:
                        pedal_down = msg.value >= 64
                        if state.progression_trigger == "pedal" and pedal_down and not pedal_was_down:
                            trigger_next(now, "pedal")
                        pedal_was_down = pedal_down
                        continue
                        
                    if (
                        state.mode in {"progression", "chart", "lyric"}
                        and state.progression_trigger == "cc-down"
                        and msg.type == "control_change"
                        and msg.control == state.cc_control
                    ):
                        value = getattr(msg, "value", 0)
                        if value >= state.cc_arm_value:
                            cc_trigger_armed = True
                        elif value <= state.cc_trigger_value and cc_trigger_armed:
                            trigger_next(now, "cc-down")
                            cc_trigger_armed = False
                        continue
                        
                    if (
                        state.mode in {"progression", "chart", "lyric"}
                        and state.progression_trigger == "pitch-up"
                        and msg.type == "pitchwheel"
                    ):
                        pitch = getattr(msg, "pitch", 0)
                        if pitch <= state.pitch_reset:
                            pitch_trigger_armed = True
                        elif pitch >= state.pitch_threshold and pitch_trigger_armed:
                            trigger_next(now, "pitch-up")
                            pitch_trigger_armed = False
                        continue
                        
                    if msg.type == "note_on" and velocity > 0 and msg.note >= state.split:
                        if state.mode in {"progression", "chart", "lyric"}:
                            if state.progression_trigger == "clock":
                                if not progression_started:
                                    trigger_next(now, "clock-start", msg.note)
                            elif state.progression_trigger == "note":
                                if accompanist.last_trigger_time is None or now - accompanist.last_trigger_time >= state.retrigger:
                                    trigger_next(now, "note", msg.note)
                        else:
                            accompanist.comp = state.comp
                            accompanist.style = state.style
                            accompanist.retrigger = state.retrigger
                            accompanist.transpose = state.transpose
                            line, decision = accompanist.play_for_melody(msg.note, now)
                            if decision:
                                decision["t"] = round(now - started_at, 4)
                                broadcast_sse(state, "decision", decision)
                        raw_out.send(msg)
                    elif msg.type in ("note_off", "note_on") and getattr(msg, "note", 127) >= state.split:
                        raw_out.send(msg)
                    elif msg.type in ("note_off", "note_on") and getattr(msg, "note", 127) < state.split:
                        raw_out.send(msg)
                        
                if state.mode in {"progression", "chart", "lyric"} and state.progression_trigger == "clock" and progression_started:
                    if next_progression_time is not None and now >= next_progression_time:
                        trigger_next(now, "clock")
                        
                accompanist.tick(time.monotonic())
                time.sleep(0.003)
                
            accompanist.stop_active()
    except Exception as e:
        broadcast_sse(state, "error", {"message": f"Engine error: {str(e)}"})
    finally:
        with state.lock:
            state.is_playing = False
        broadcast_sse(state, "status", {"is_playing": False})


class UIRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/events":
            self.handle_sse()
            return
            
        if parsed.path == "/api/ports":
            self.send_json({
                "inputs": mido.get_input_names() if mido else [],
                "outputs": mido.get_output_names() if mido else []
            })
            return
            
        ui_dir = Path(__file__).parent / "ui"
        if parsed.path == "/":
            file_path = ui_dir / "index.html"
        else:
            file_path = ui_dir / parsed.path.lstrip("/")
            
        try:
            resolved = file_path.resolve()
            if resolved.is_file() and resolved.relative_to(ui_dir.resolve()):
                content_type = "text/html"
                if file_path.suffix == ".css":
                    content_type = "text/css"
                elif file_path.suffix == ".js":
                    content_type = "application/javascript"
                elif file_path.suffix == ".json":
                    content_type = "application/json"
                elif file_path.suffix in {".png", ".jpg", ".jpeg"}:
                    content_type = f"image/{file_path.suffix[1:]}"
                    
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resolved.read_bytes())
                return
        except Exception:
            pass
            
        self.send_error(404, "File Not Found")
        
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else ""
        
        try:
            data = json.loads(body) if body else {}
        except Exception:
            self.send_error(400, "Invalid JSON")
            return
            
        if parsed.path == "/api/settings":
            with global_state.lock:
                if "input_port" in data:
                    global_state.input_port_name = data["input_port"]
                if "output_port" in data:
                    global_state.output_port_name = data["output_port"]
                if "split" in data:
                    global_state.split = int(data["split"])
                if "comp" in data:
                    global_state.comp = data["comp"]
                if "style" in data:
                    global_state.style = data["style"]
                if "mode" in data:
                    global_state.mode = data["mode"]
                if "tempo" in data:
                    global_state.progression_tempo = float(data["tempo"])
                if "period" in data:
                    global_state.progression_period = float(data["period"]) if data["period"] is not None else None
                if "retrigger" in data:
                    global_state.retrigger = float(data["retrigger"])
                if "progression_trigger" in data:
                    global_state.progression_trigger = data["progression_trigger"]
                if "control_note" in data:
                    global_state.control_note = int(data["control_note"])
                if "learn_control_note" in data:
                    global_state.learn_control_note = bool(data["learn_control_note"])
                if "cc_control" in data:
                    global_state.cc_control = int(data["cc_control"])
                if "cc_arm_value" in data:
                    global_state.cc_arm_value = int(data["cc_arm_value"])
                if "cc_trigger_value" in data:
                    global_state.cc_trigger_value = int(data["cc_trigger_value"])
                if "pitch_threshold" in data:
                    global_state.pitch_threshold = int(data["pitch_threshold"])
                if "pitch_reset" in data:
                    global_state.pitch_reset = int(data["pitch_reset"])
                if "transpose" in data:
                    global_state.transpose = int(data["transpose"])

            
            if global_state.is_playing:
                stop_midi_engine()
                start_midi_engine()
                
            self.send_json({"status": "ok"})
            return
            
        if parsed.path == "/api/fetch-hopamchuan":
            url = data.get("url")
            if not url:
                self.send_error(400, "Missing url parameter")
                return
            try:
                html_text = fetch_text(url)
                lines = hopamchuan_lines_from_html(html_text)
                inline_lines = [l for l in lines if l.strip()]
                events = lyric_events_from_inline_lines(inline_lines, DEFAULT_CHART_PERIOD)
                
                with global_state.lock:
                    global_state.fetched_url = url
                    global_state.fetched_inline_lines = inline_lines
                    global_state.fetched_events = events
                    global_state.progression = [
                        chart_token_to_example(e.chord, DEFAULT_CHART_PERIOD, e.section, style=global_state.style)
                        for e in events
                    ]
                    global_state.lyric_progression = lyric_events_to_examples(events, DEFAULT_CHART_PERIOD, style=global_state.style)
                    global_state.progression_index = 0
                    
                self.send_json({
                    "status": "ok",
                    "inline_lines": inline_lines,
                    "events": [asdict(e) for e in events]
                })
                broadcast_sse(global_state, "timeline", {
                    "url": url,
                    "inline_lines": inline_lines,
                    "events": [asdict(e) for e in events]
                })
            except Exception as e:
                self.send_json({"status": "error", "message": str(e)}, status_code=500)
            return
            
        if parsed.path == "/api/control":
            cmd = data.get("command")
            if cmd == "play":
                if not global_state.is_playing:
                    start_midi_engine()
            elif cmd == "stop":
                if global_state.is_playing:
                    stop_midi_engine()
            elif cmd == "next":
                with global_state.lock:
                    global_state.next_triggered = True
            elif cmd == "reset":
                with global_state.lock:
                    global_state.progression_index = 0
                broadcast_sse(global_state, "reset", {"index": 0})
            elif cmd == "jump":
                idx = data.get("index", 0)
                with global_state.lock:
                    global_state.progression_index = idx
                broadcast_sse(global_state, "reset", {"index": idx})
            else:
                self.send_error(400, "Unknown command")
                return
                
            self.send_json({"status": "ok", "is_playing": global_state.is_playing})
            return
            
        self.send_error(404, "Endpoint Not Found")
        
    def send_json(self, data: dict, status_code: int = 200):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))
        
    def handle_sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        
        q = queue.Queue()
        with global_state.subscribers_lock:
            global_state.subscribers.append(q)
            
        try:
            with global_state.lock:
                initial_state = {
                    "is_playing": global_state.is_playing,
                    "input_port": global_state.input_port_name,
                    "output_port": global_state.output_port_name,
                    "style": global_state.style,
                    "comp": global_state.comp,
                    "mode": global_state.mode,
                    "split": global_state.split,
                    "progression_index": global_state.progression_index,
                    "fetched_url": global_state.fetched_url,
                    "fetched_inline_lines": global_state.fetched_inline_lines,
                    "fetched_events": [asdict(e) for e in global_state.fetched_events] if global_state.fetched_events else [],
                    "progression_trigger": global_state.progression_trigger,
                    "control_note": global_state.control_note,
                    "learn_control_note": global_state.learn_control_note,
                    "cc_control": global_state.cc_control,
                    "cc_arm_value": global_state.cc_arm_value,
                    "cc_trigger_value": global_state.cc_trigger_value,
                    "pitch_threshold": global_state.pitch_threshold,
                    "pitch_reset": global_state.pitch_reset,
                    "transpose": global_state.transpose,
                }
            self.wfile.write(f"data: {json.dumps({'event': 'state', 'data': initial_state})}\n\n".encode('utf-8'))
            self.wfile.flush()
            
            while True:
                try:
                    msg = q.get(timeout=1.0)
                    self.wfile.write(msg.encode('utf-8'))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with global_state.subscribers_lock:
                if q in global_state.subscribers:
                    global_state.subscribers.remove(q)


def cmd_ui(args) -> None:
    require_mido()
    
    global_state.model_path = args.model
    global_state.split = args.split
    global_state.comp = args.comp
    global_state.style = args.style
    global_state.transpose = args.transpose
    
    global_state.progression_trigger = args.progression_trigger
    global_state.control_note = args.control_note
    global_state.learn_control_note = args.learn_control_note
    global_state.pitch_threshold = args.pitch_threshold
    global_state.pitch_reset = args.pitch_reset
    global_state.cc_control = args.cc_control
    global_state.cc_arm_value = args.cc_arm_value
    global_state.cc_trigger_value = args.cc_trigger_value
    
    if mido:
        inputs = mido.get_input_names()
        outputs = mido.get_output_names()
        if getattr(args, "input", None) and inputs:
            try:
                global_state.input_port_name = fuzzy_pick(args.input, inputs, "input")
            except SystemExit:
                pass
        elif inputs:
            global_state.input_port_name = inputs[0]
            
        if getattr(args, "output", None) and outputs:
            try:
                global_state.output_port_name = fuzzy_pick(args.output, outputs, "output")
            except SystemExit:
                pass
        elif outputs:
            global_state.output_port_name = outputs[0]
        
    server_address = ('', args.port)
    
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        
    httpd = ThreadingHTTPServer(server_address, UIRequestHandler)
    print(f"Starting Piano Chord Learner UI at http://localhost:{args.port}/")
    
    def open_browser():
        time.sleep(1.0)
        webbrowser.open(f"http://localhost:{args.port}/")
        
    threading.Thread(target=open_browser, daemon=True).start()
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        global_state.playback_stop_event.set()
        if global_state.playback_thread and global_state.playback_thread.is_alive():
            global_state.playback_thread.join()
        httpd.server_close()



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Learn and auto-play piano chords from MIDI.")
    sub = parser.add_subparsers(required=True)

    ports = sub.add_parser("ports", help="List MIDI input and output ports.")
    ports.set_defaults(func=cmd_ports)

    train = sub.add_parser("train", help="Learn chord examples from your MIDI performance.")
    train.add_argument("--input", help="MIDI input name or substring. Defaults to first input.")
    train.add_argument("--model", default="my_style.json", help="Path to save the learned model.")
    train.add_argument("--split", type=int, default=DEFAULT_SPLIT, help="Notes below this are chords.")
    train.add_argument("--chord-window", type=float, default=DEFAULT_CHORD_WINDOW)
    train.add_argument("--left-pattern-window", type=float, default=DEFAULT_LEFT_PATTERN_WINDOW, help="Seconds of left-hand note-ons to learn as an arpeggio pattern.")
    train.add_argument("--new-chord-gap", type=float, default=DEFAULT_NEW_CHORD_GAP, help="Start a new left-hand pattern after this much silence or a lower bass change.")
    train.add_argument("--pair-window", type=float, default=DEFAULT_PAIR_WINDOW)
    train.add_argument("--event-log", help="Write every MIDI note event seen during training to a JSONL file.")
    train.add_argument("--print-notes", action="store_true", help="Print every MIDI note event while training.")
    train.add_argument("--verbose", action="store_true")
    train.set_defaults(func=cmd_train)

    play = sub.add_parser("play", help="Auto-play learned chords while you play melody.")
    play.add_argument("--input", help="MIDI input name or substring. Defaults to first input.")
    play.add_argument("--output", help="Existing MIDI output name or substring.")
    play.add_argument("--model", default="my_style.json", help="Path to learned model JSON.")
    play.add_argument("--split", type=int, help="Override model split point.")
    play.add_argument("--channel", type=int, default=0, help="MIDI output channel, 0-15.")
    play.add_argument("--duration", type=float, default=DEFAULT_CHORD_DURATION)
    play.add_argument("--mode", choices=["melody", "progression", "chart", "lyric"], default="melody", help="melody guesses chords from notes; progression uses a learned left-hand sequence; chart follows a fixed chord chart; lyric follows timed lyric events.")
    play.add_argument("--progression-log", help="JSONL note log from training/recording, used by --mode progression.")
    play.add_argument("--progression-gap", type=float, default=DEFAULT_PROGRESSION_GAP, help="Max seconds between left-hand notes in one progression chord.")
    play.add_argument("--progression-start", type=int, default=1, help="1-based chord number to start from in progression mode.")
    play.add_argument("--progression-trigger", choices=["clock", "note", "pedal", "control-note", "pitch-up", "cc-down"], default="cc-down", help="clock starts on first melody note; note advances on melody triggers; pedal/control-note/pitch-up/cc-down advance manually.")
    play.add_argument("--progression-tempo", type=float, default=1.0, help="Multiplier for learned progression durations. Lower is faster, higher is slower.")
    play.add_argument("--progression-period", type=float, help="Fixed seconds per chord in progression clock mode.")
    play.add_argument("--progression-length", type=int, help="Use only the first N chords from the extracted progression as a loop.")
    play.add_argument("--chart-file", help="Plain-text chord chart used by --mode chart.")
    play.add_argument("--chart-start", type=int, default=1, help="1-based chord number to start from in chart mode.")
    play.add_argument("--chart-period", type=float, default=DEFAULT_CHART_PERIOD, help="Seconds per chord in chart mode before tempo scaling.")
    play.add_argument("--lyric-file", help="Timed lyric file in 'time|chord|lyric' format used by --mode lyric.")
    play.add_argument("--control-note", type=int, default=21, help="MIDI note used to advance when --progression-trigger control-note.")
    play.add_argument("--learn-control-note", action="store_true", help="Use the first played note as the control note for this run.")
    play.add_argument("--pitch-threshold", type=int, default=6000, help="Pitchwheel value that triggers next chord in --progression-trigger pitch-up mode.")
    play.add_argument("--pitch-reset", type=int, default=1000, help="Pitchwheel must come back below this value before it can trigger again.")
    play.add_argument("--cc-control", type=int, default=1, help="Control Change number used in --progression-trigger cc-down mode.")
    play.add_argument("--cc-arm-value", type=int, default=60, help="CC value that arms the next trigger in cc-down mode.")
    play.add_argument("--cc-trigger-value", type=int, default=30, help="CC value at or below which the armed cc-down trigger advances the chord.")
    play.add_argument("--comp", choices=["learned", "power", "block", "style"], default="learned", help="Accompaniment style. learned uses trained left-hand patterns; style uses preset arpeggios; old models fall back to power.")
    play.add_argument("--style", choices=["yiruma", "richard_clayderman", "ludovico_einaudi"], default="yiruma", help="Chord style preset when comp is 'style' or progression/chart/lyric modes are active.")
    play.add_argument("--transpose", type=int, default=0, help="Transpose key up or down by semitones.")

    play.add_argument("--arp-step", type=float, default=DEFAULT_ARP_STEP, help="Seconds between generated power-chord arpeggio notes.")
    play.add_argument("--note-length", type=float, default=DEFAULT_NOTE_LENGTH, help="Seconds each arpeggio note is held.")
    play.add_argument("--retrigger", type=float, default=0.450, help="Minimum seconds before a melody note may trigger a new chord.")
    play.add_argument("--randomize", action="store_true", help="Randomly choose among matching examples. Default is deterministic top match.")
    play.add_argument("--track-bpm", action="store_true", help="Estimate BPM from melody note timing and scale accompaniment timing in real time.")
    play.add_argument("--base-bpm", type=float, default=72.0, help="Reference BPM for unscaled accompaniment patterns.")
    play.add_argument("--bpm-min", type=float, default=48.0, help="Minimum accepted tracked BPM.")
    play.add_argument("--bpm-max", type=float, default=132.0, help="Maximum accepted tracked BPM.")
    play.add_argument("--bpm-smoothing", type=float, default=0.78, help="Higher values make BPM tracking steadier but slower to react.")
    play.add_argument("--event-log", help="Write input notes and auto-chord decisions to a JSONL file.")
    play.add_argument("--print-notes", action="store_true", help="Print every MIDI note event while playing.")
    play.add_argument("--virtual-output", action="store_true", help="Deprecated: virtual output is automatic when --output is omitted.")
    play.add_argument("--virtual-name", default="ChordLearner Out")
    play.add_argument("--verbose", action="store_true")
    play.set_defaults(func=cmd_play)

    record = sub.add_parser("record", help="Log every MIDI key press/release without learning or playing chords.")
    record.add_argument("--input", help="MIDI input name or substring. Defaults to first input.")
    record.add_argument("--out", default="session_notes.jsonl", help="JSONL file to write.")
    record.add_argument("--split", type=int, default=DEFAULT_SPLIT, help="Notes below this are labelled left hand.")
    record.set_defaults(func=cmd_record)

    analyze = sub.add_parser("analyze", help="Analyze a learned model and/or a JSONL note log.")
    analyze.add_argument("--model", help="Path to learned model JSON.")
    analyze.add_argument("--log", help="Path to JSONL event log from record/train/play.")
    analyze.add_argument("--ambiguous-threshold", type=int, default=3, help="Flag melody notes mapped to this many chords.")
    analyze.add_argument("--show-examples", type=int, default=12, help="Show top learned examples.")
    analyze.add_argument("--show-decisions", type=int, default=40, help="Show first N auto-chord decisions from log.")
    analyze.set_defaults(func=cmd_analyze)

    importer = sub.add_parser("import-hopamchuan", help="Fetch a HopAmChuan URL and convert it into inline chord text and lyric timeline files.")
    importer.add_argument("--url", required=True, help="HopAmChuan song URL.")
    importer.add_argument("--inline-out", default="song_inline_chords.txt", help="Output file for inline chord lyrics.")
    importer.add_argument("--timeline-out", default="song_lyric_timeline.txt", help="Output file for estimated lyric timeline.")
    importer.add_argument("--period", type=float, default=DEFAULT_CHART_PERIOD, help="Estimated seconds per chord event in generated timeline.")
    ui = sub.add_parser("ui", help="Start the local Web UI to configure settings and play chords interactively.")
    ui.add_argument("--model", default="my_style.json", help="Path to learned model JSON.")
    ui.add_argument("--port", type=int, default=8000, help="Local port to run the web server.")
    ui.add_argument("--split", type=int, default=DEFAULT_SPLIT, help="Split point between left and right hand.")
    ui.add_argument("--comp", choices=["learned", "power", "block", "style"], default="style", help="Default accompaniment style.")
    ui.add_argument("--style", choices=["yiruma", "richard_clayderman", "ludovico_einaudi"], default="yiruma", help="Default style preset.")
    
    ui.add_argument("--input", help="MIDI input name or substring. Defaults to first input.")
    ui.add_argument("--output", help="Existing MIDI output name or substring.")
    ui.add_argument("--transpose", type=int, default=0, help="Transpose key up or down by semitones.")
    
    ui.add_argument("--progression-trigger", choices=["clock", "note", "pedal", "control-note", "pitch-up", "cc-down"], default="cc-down", help="clock starts on first melody note; note advances on melody triggers; pedal/control-note/pitch-up/cc-down advance manually.")
    ui.add_argument("--cc-control", type=int, default=1, help="Control Change number used in --progression-trigger cc-down mode.")
    ui.add_argument("--cc-arm-value", type=int, default=60, help="CC value that arms the next trigger in cc-down mode.")
    ui.add_argument("--cc-trigger-value", type=int, default=30, help="CC value at or below which the armed cc-down trigger advances the chord.")
    ui.add_argument("--control-note", type=int, default=21, help="MIDI note used to advance when --progression-trigger control-note.")
    ui.add_argument("--learn-control-note", action="store_true", help="Use the first played note as the control note for this run.")
    ui.add_argument("--pitch-threshold", type=int, default=6000, help="Pitchwheel value that triggers next chord in --progression-trigger pitch-up mode.")
    ui.add_argument("--pitch-reset", type=int, default=1000, help="Pitchwheel must come back below this value before it can trigger again.")
    
    ui.set_defaults(func=cmd_ui)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "output", None):
        require_mido()
        args.output = fuzzy_pick(args.output, mido.get_output_names(), "output")
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
