"""Acquisition and deterministic analysis. No GUI or third-party dependencies."""
from __future__ import annotations

import math
import re
import statistics
import time
import uuid
from collections import Counter
from datetime import datetime
from typing import Callable

SCHEMA_VERSION = 1
WORD_RE = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*|\d+(?:[.,]\d+)*", re.UNICODE)
MODIFIERS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
             "Meta_L", "Meta_R", "Win_L", "Win_R", "Caps_Lock", "Num_Lock"}
PUNCTUATION = set(",.!?;:…—–()[]{}\"«»")
PRESET_TOPICS = [
    "Опишите свой обычный день",
    "Расскажите о месте, в которое хочется вернуться",
    "Как технологии меняют повседневную жизнь?",
    "Вспомните событие, которое вас удивило",
    "Опишите процесс решения сложной задачи",
    "Каким вы представляете идеальный выходной?",
]


def words(text: str) -> list[str]:
    return WORD_RE.findall(text)


def linguistic(text: str) -> dict:
    tokens = words(text)
    sentences = [s for s in re.split(r"[.!?…]+(?:[\"»')\]]*)\s*|\n+", text) if words(s)]
    n = len(tokens)
    return {
        "characters": len(text),
        "characters_no_spaces": sum(not c.isspace() for c in text),
        "words": n,
        "sentences": len(sentences),
        "unique_words": len({w.casefold() for w in tokens}),
        "ttr": len({w.casefold() for w in tokens}) / n if n else 0.0,
        "mean_word_length": sum(len(w) for w in tokens) / n if n else 0.0,
        "mean_sentence_length": n / len(sentences) if sentences else 0.0,
    }


def validate_settings(settings: dict) -> None:
    if not str(settings.get("title", "")).strip():
        raise ValueError("Укажите название листа.")
    if settings.get("mode") not in {"continuous", "topics"}:
        raise ValueError("Выберите режим ввода.")
    topics = settings.get("topics", [])
    if not topics or any(not str(t).strip() for t in topics):
        raise ValueError("Добавьте хотя бы одну непустую тему.")
    if len(topics) > 50:
        raise ValueError("Допускается до 50 тем на одном листе.")
    for key in ("time_limit_s", "target_characters", "target_words"):
        value = settings.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("Ограничения должны быть неотрицательными числами; 0 — без ограничения.")
    micro, macro = settings.get("micro_pause_ms", 300), settings.get("macro_pause_ms", 2000)
    if not (math.isfinite(micro) and math.isfinite(macro) and 0 < micro < macro):
        raise ValueError("Пороги пауз должны удовлетворять условию 0 < микро < макро.")


def text_delta(before: str, after: str) -> tuple[int, str, str]:
    """Smallest contiguous replacement; adequate for Tk's atomic edit commands."""
    n = min(len(before), len(after))
    # Native string comparisons keep the common case (editing the end of a
    # long document) out of a per-character Python loop on the GUI thread.
    low, high = 0, n
    while low < high:
        mid = (low + high + 1) // 2
        if before[:mid] == after[:mid]:
            low = mid
        else:
            high = mid - 1
    left = low
    low, high = 0, n - left
    while low < high:
        mid = (low + high + 1) // 2
        if before[-mid:] == after[-mid:]:
            low = mid
        else:
            high = mid - 1
    right = low
    return left, before[left:len(before) - right if right else None], after[left:len(after) - right if right else None]


def pause_position(text: str, cursor: int, char: str) -> str:
    if char and char[0] in PUNCTUATION:
        return "before_punctuation"
    left = text[cursor - 1:cursor] if cursor else ""
    if (left and left.isspace()) or (char and char.isspace()):
        return "between_words"
    if left and left[-1] not in PUNCTUATION and char and char[0].isalnum():
        return "within_word"
    return "other"


class Recorder:
    def __init__(self, profile: dict, settings: dict, clock: Callable[[], int] = time.perf_counter_ns):
        validate_settings(settings)
        self.clock = clock
        self.origin = clock()
        self.session_id = str(uuid.uuid4())
        self.profile = dict(profile)
        self.profile["session_label"] = profile.get("session_label") or self.session_id
        self.settings = {**settings, "topics": list(settings["topics"])}
        self.started_at = datetime.now().astimezone().isoformat()
        self.finished_at = None
        self.status = "ready"
        self.topic = 0
        self.texts = ["" for _ in settings["topics"]]
        self.events: list[dict] = []
        self.intervals: list[dict] = []
        self.active_start: int | None = None
        self.active_total = 0
        self.segment = 0
        self.pressed: dict[int, int] = {}
        self.reason = ""
        self.end_stamp: int | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    def stamp(self) -> int:
        return self.end_stamp if self.end_stamp is not None else self.clock() - self.origin

    def active_ns(self, stamp: int | None = None) -> int:
        stamp = self.stamp() if stamp is None else stamp
        return self.active_total + (stamp - self.active_start if self.active_start is not None else 0)

    def event(self, kind: str, stamp: int | None = None, **data) -> dict:
        stamp = self.stamp() if stamp is None else stamp
        item = {"seq": len(self.events) + 1, "type": kind, "t_ns": stamp,
                "active_ns": self.active_ns(stamp), "topic": self.topic,
                "segment": self.segment, **data}
        self.events.append(item)
        return item

    def resume(self) -> None:
        if self.status not in {"ready", "paused"}:
            return
        self.segment += 1
        stamp = self.stamp()
        self.active_start = stamp
        self.status = "running"
        self.event("resume", stamp)

    def pause(self, reason: str = "manual", stamp: int | None = None) -> None:
        if not self.running:
            return
        stamp = self.stamp() if stamp is None else stamp
        self.event("pause", stamp, reason=reason)
        for keycode, pair_id in self.pressed.items():
            self.event("key_cancel", stamp, keycode=keycode, pair_id=pair_id, reason=reason)
        self.pressed.clear()
        self.intervals.append({"start_ns": self.active_start, "end_ns": stamp,
                               "topic": self.topic, "segment": self.segment})
        self.active_total += stamp - self.active_start
        self.active_start = None
        self.status = "paused"

    def switch_topic(self, index: int) -> None:
        if self.status == "completed":
            raise ValueError("Сессия уже завершена")
        if not 0 <= index < len(self.texts):
            raise ValueError("Неизвестная тема")
        self.pause("topic_change")
        previous = self.topic
        self.topic = index
        self.event("topic_change", previous_topic=previous)

    def key_down(self, keycode: int, keysym: str, char: str, cursor: int,
                 selection: list[int] | None = None, modifiers: int = 0,
                 os_time_ms: int = 0, stamp: int | None = None) -> None:
        if not self.running:
            return
        repeat = keycode in self.pressed
        eligible = keysym not in MODIFIERS and (bool(char and char.isprintable()) or keysym in {"BackSpace", "Delete", "Return", "KP_Enter", "Tab"})
        # Control shortcuts (except AltGr) do not represent ordinary typing.
        if modifiers & 4 and not modifiers & 0x20000:
            eligible = keysym in {"BackSpace", "Delete"}
        event = self.event("key_down", stamp, keycode=keycode, keysym=keysym, char=char,
                           cursor=cursor, selection=selection, modifiers=modifiers,
                           os_time_ms=os_time_ms, repeat=repeat, eligible=eligible,
                           position=pause_position(self.texts[self.topic], cursor, char))
        event["pair_id"] = self.pressed.get(keycode, event["seq"])
        if not repeat:
            self.pressed[keycode] = event["seq"]

    def key_up(self, keycode: int, keysym: str, os_time_ms: int = 0, stamp: int | None = None) -> None:
        if not self.running:
            return
        pair_id = self.pressed.pop(keycode, None)
        self.event("key_up", stamp, keycode=keycode, keysym=keysym,
                   os_time_ms=os_time_ms, pair_id=pair_id)

    def edit(self, after: str, source: str = "keyboard", cursor: int = 0,
             selection: list[int] | None = None, stamp: int | None = None) -> None:
        if not self.running:
            return
        before = self.texts[self.topic]
        if before == after:
            return
        offset, removed, inserted = text_delta(before, after)
        self.event("edit", stamp, offset=offset, removed=removed, inserted=inserted,
                   source=source, cursor=cursor, selection=selection)
        self.texts[self.topic] = after

    def finish(self, reason: str = "manual") -> None:
        if self.status == "completed":
            return
        self.pause("finish")
        self.reason = reason
        self.event("finish", reason=reason)
        self.end_stamp = self.stamp()
        self.status = "completed"
        self.finished_at = datetime.now().astimezone().isoformat()

    def limit_reached(self) -> str | None:
        settings = self.settings
        if settings.get("time_limit_s") and self.active_ns() >= settings["time_limit_s"] * 1e9:
            return "time_limit"
        if settings.get("target_characters") and sum(map(len, self.texts)) >= settings["target_characters"]:
            return "character_target"
        if settings.get("target_words") and sum(len(words(t)) for t in self.texts) >= settings["target_words"]:
            return "word_target"
        return None

    def snapshot(self, include_events: bool = True) -> dict:
        stamp = self.stamp()
        intervals = [dict(i) for i in self.intervals]
        if self.active_start is not None:
            intervals.append({"start_ns": self.active_start, "end_ns": stamp,
                              "topic": self.topic, "segment": self.segment})
        result = {
            "schema_version": SCHEMA_VERSION, "app_version": "1.0.0",
            "session_id": self.session_id, "profile": dict(self.profile),
            "settings": {**self.settings, "topics": list(self.settings["topics"])},
            "started_at": self.started_at, "finished_at": self.finished_at,
            "status": self.status, "finish_reason": self.reason,
            "clock": "time.perf_counter_ns", "clock_resolution_ns": time.get_clock_info("perf_counter").resolution * 1e9,
            "origin_perf_counter_ns": self.origin, "elapsed_ns": stamp,
            "active_ns": self.active_ns(stamp), "intervals": intervals,
            "texts": list(self.texts), "event_count": len(self.events),
        }
        if include_events:
            result["events"] = list(self.events)
        return result


def _distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"count": len(values),
            "mean_ms": statistics.fmean(values) if values else None,
            "median_ms": statistics.median(values) if values else None,
            "p95_ms": ordered[max(0, math.ceil(.95 * len(ordered)) - 1)] if ordered else None}


def analyze(snapshot: dict, topic: int | None = None) -> dict:
    """Reproducible report from raw data; every threshold comes from the protocol."""
    events = [e for e in snapshot["events"] if topic is None or e["topic"] == topic]
    texts = snapshot["texts"] if topic is None else [snapshot["texts"][topic]]
    # Analyze cards independently: joining cards must not create words/sentences.
    ling = [linguistic(t) for t in texts]
    tokens = [w for text in texts for w in words(text)]
    result = {key: sum(part[key] for part in ling) for key in ("characters", "characters_no_spaces", "words", "sentences")}
    result.update(unique_words=len({w.casefold() for w in tokens}),
                  ttr=len({w.casefold() for w in tokens}) / len(tokens) if tokens else 0,
                  mean_word_length=sum(map(len, tokens)) / len(tokens) if tokens else 0,
                  mean_sentence_length=len(tokens) / result["sentences"] if result["sentences"] else 0)
    intervals = [i for i in snapshot["intervals"] if topic is None or i["topic"] == topic]
    active_s = sum(i["end_ns"] - i["start_ns"] for i in intervals) / 1e9
    downs = [e for e in events if e["type"] == "key_down"]
    first_downs = [e for e in downs if not e["repeat"] and e["eligible"]]
    up_by_pair = {e["pair_id"]: e for e in events if e["type"] == "key_up" and e.get("pair_id")}
    holds, flights, pairs = [], [], []
    for down in (e for e in downs if not e["repeat"]):
        up = up_by_pair.get(down["pair_id"])
        if up and up["segment"] == down["segment"]:
            hold = (up["t_ns"] - down["t_ns"]) / 1e6
            if hold >= 0:
                if down["eligible"]:
                    holds.append(hold)
                pairs.append({"down_seq": down["seq"], "up_seq": up["seq"], "hold_ms": hold,
                              "keycode": down["keycode"], "keysym": down["keysym"], "eligible": down["eligible"]})
    for previous, current in zip(first_downs, first_downs[1:]):
        up = up_by_pair.get(previous["pair_id"])
        if up and current["segment"] == previous["segment"] == up["segment"]:
            flights.append((current["t_ns"] - up["t_ns"]) / 1e6)
    eligible = [e for e in downs if e["eligible"]]
    micro = snapshot["settings"].get("micro_pause_ms", 300)
    macro = snapshot["settings"].get("macro_pause_ms", 2000)
    pauses = []
    for previous, current in zip(eligible, eligible[1:]):
        gap = (current["t_ns"] - previous["t_ns"]) / 1e6
        if current["segment"] == previous["segment"] and gap >= micro:
            pauses.append({"previous_seq": previous["seq"], "next_seq": current["seq"],
                           "duration_ms": gap, "kind": "macro" if gap >= macro else "micro",
                           "position": current["position"], "topic": current["topic"]})
    edits = [e for e in events if e["type"] == "edit"]
    typed_chars = sum(len(e["inserted"]) for e in edits if e["source"] == "keyboard")
    inserted_chars = sum(len(e["inserted"]) for e in edits)
    deleted_chars = sum(len(e["removed"]) for e in edits)
    corrections = [e for e in edits if e["removed"]]
    # A cycle starts at a deletion; another deletion within 2 s in the same
    # segment remains in that cycle, including intervening replacement typing.
    cycles = 0
    previous = None
    for e in corrections:
        if previous is None or e["segment"] != previous["segment"] or e["t_ns"] - previous["t_ns"] > 2e9:
            cycles += 1
        previous = e
    pause_s = sum(p["duration_ms"] for p in pauses) / 1000
    macro_s = sum(p["duration_ms"] for p in pauses if p["kind"] == "macro") / 1000
    histogram = []
    edges = [micro, macro, max(macro * 2, 5000), max(macro * 5, 10000), float("inf")]
    for low, high in zip(edges, edges[1:]):
        histogram.append({"from_ms": low, "to_ms": None if math.isinf(high) else high,
                          "count": sum(low <= p["duration_ms"] < high for p in pauses)})
    # Active time, 5-second bins (widened only for very long sessions).
    bin_s = max(5, math.ceil(active_s / 720))
    count = max(1, math.ceil(active_s / bin_s))
    bins = [0] * count
    def local_active(t_ns: int) -> float:
        return sum(max(0, min(t_ns, i["end_ns"]) - i["start_ns"]) for i in intervals if i["start_ns"] < t_ns) / 1e9
    for e in edits:
        if e["source"] == "keyboard":
            seconds = e["active_ns"] / 1e9 if topic is None else local_active(e["t_ns"])
            bins[min(count - 1, int(seconds / bin_s))] += len(e["inserted"])
    speed = []
    for index, value in enumerate(bins):
        duration = min(bin_s, max(0, active_s - index * bin_s))
        cpm = value * 60 / duration if duration else 0
        speed.append({"start_s": index * bin_s, "end_s": index * bin_s + duration,
                      "characters": value, "cpm": cpm, "wpm": cpm / 5})
    result.update(
        active_seconds=active_s, elapsed_seconds=snapshot["elapsed_ns"] / 1e9,
        typed_characters=typed_chars, inserted_characters=inserted_chars, deleted_characters=deleted_chars,
        cpm=typed_chars * 60 / active_s if active_s else 0,
        wpm=typed_chars * 12 / active_s if active_s else 0,
        net_cpm=result["characters"] * 60 / active_s if active_s else 0,
        net_words_per_minute=result["words"] * 60 / active_s if active_s else 0,
        key_down_count=len(downs), backspace_count=sum(e["keysym"] == "BackSpace" for e in downs),
        delete_count=sum(e["keysym"] == "Delete" for e in downs),
        correction_operations=len(corrections), correction_cycles=cycles,
        selection_deletions=sum(bool(e.get("selection")) for e in corrections),
        correction_percent=deleted_chars / inserted_chars * 100 if inserted_chars else 0,
        pasted_characters=sum(len(e["inserted"]) for e in edits if e["source"] == "paste"),
        hold=_distribution(holds), flight=_distribution(flights),
        hold_samples_ms=holds, flight_samples_ms=flights, key_pairs=pairs,
        unpaired_key_downs=len(first_downs) - len(holds),
        pause_seconds=pause_s, macro_pause_seconds=macro_s,
        cognitive_delay_index=macro_s / active_s * 100 if active_s else 0,
        micro_pause_count=sum(p["kind"] == "micro" for p in pauses),
        macro_pause_count=sum(p["kind"] == "macro" for p in pauses),
        pause_positions=dict(Counter(p["position"] for p in pauses)),
        pauses=pauses, pause_histogram=histogram, speed_series=speed,
    )
    return result


def full_report(snapshot: dict) -> dict:
    return {"overall": analyze(snapshot),
            "topics": [{"index": i, "title": title, **analyze(snapshot, i)}
                       for i, title in enumerate(snapshot["settings"]["topics"])]}
