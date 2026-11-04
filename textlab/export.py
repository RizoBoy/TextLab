"""Portable, offline reports and lossless research data exports."""
from __future__ import annotations

import csv
import html
import json
import os
import tempfile
from pathlib import Path

from .core import full_report

METHODS = """Активное время включает обдумывание в редакторе и исключает ручную паузу, потерю фокуса и переключение тем.
CPM = введенные с клавиатуры символы / активные минуты; WPM = CPM / 5 (условное слово).
Вставка из буфера, отмена и повтор не увеличивают число набранных символов. Итоговый объем включает их.
Процент исправлений = число удаленных символов / число всех вставленных символов × 100; это доля редактирования, а не ошибок.
Цикл исправлений объединяет удаления в одном активном отрезке с интервалом не более 2 секунд.
Hold Time = keyup − keydown одной клавиши; Flight Time = следующий keydown − предыдущий keyup.
Flight Time может быть отрицательным при перекрытии нажатий. Модификаторы, сочетания Ctrl и автоповторы исключены из Hold/Flight.
Пауза — интервал между соседними текстовыми/редактирующими keydown в одном активном отрезке; автоповторы включены.
Паузы до первого и после последнего нажатия не классифицируются. Положение определяется по курсору и следующему символу.
Индекс когнитивных задержек = сумма макропауз / активное время × 100. Это описательный показатель, не клиническая оценка.
TTR = число уникальных слов без учета регистра / число слов; лемматизация не применяется.
Слова: последовательности букв (включая дефис/апостроф внутри) или чисел; предложения разделяются . ! ? … и переводом строки.
Временные метки фиксируются монотонными часами при обработке событий приложением; наносекундная единица не означает аппаратной точности.
События регистрируются только в редакторе активной сессии. Незавершенные пары клавиш не участвуют в Hold Time."""

PROFILE_LABELS = {
    "session_label": "Идентификатор сессии", "age": "Возраст", "gender": "Пол",
    "handedness": "Ведущая рука", "touch_typing": "Слепая печать",
    "fatigue": "Усталость (0–10)", "stress": "Стресс (0–10)", "time_of_day": "Время суток",
    "caffeine": "Кофеин", "sedatives": "Седативные вещества", "notes": "Примечания",
}


def _safe(value) -> str:
    return html.escape(str(value))


def fmt(value, digits=1) -> str:
    return "—" if value is None else f"{value:.{digits}f}".replace(".", ",")


def atomic_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
            name = handle.name
            handle.write(text)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def export_json(snapshot: dict, path: Path) -> None:
    atomic_text(path, json.dumps({**snapshot, "analysis": full_report(snapshot), "methodology": METHODS}, ensure_ascii=False, indent=2, allow_nan=False))


def csv_safe(value):
    # Prevent imported text from becoming spreadsheet formulas.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def export_csv(snapshot: dict, path: Path) -> None:
    fields = ["session_id", "seq", "type", "t_ns", "active_ns", "topic", "segment",
              "keycode", "keysym", "char", "pair_id", "repeat", "eligible", "os_time_ms",
              "modifiers", "cursor", "selection", "offset", "inserted", "removed",
              "source", "position", "reason", "previous_topic", "payload_json"]
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
        writer.writeheader()
        for event in snapshot["events"]:
            row = {key: csv_safe(event.get(key, "")) for key in fields}
            if event.get("selection") is not None:
                row["selection"] = json.dumps(event["selection"])
            row["session_id"] = snapshot["session_id"]
            row["payload_json"] = json.dumps(event, ensure_ascii=False)
            writer.writerow(row)


def speed_svg(series: list[dict]) -> str:
    width, height, left, top = 760, 220, 58, 18
    plot_w, plot_h = 678, 154
    max_x = max(1, max((p["end_s"] for p in series), default=1))
    max_y = max(10, max((p["cpm"] for p in series), default=0) * 1.15)
    lines = []
    for i in range(5):
        y = top + plot_h * i / 4
        lines.append(f'<line x1="{left}" x2="{left+plot_w}" y1="{y}" y2="{y}" stroke="#e3e9ed"/><text x="48" y="{y+4}" text-anchor="end">{max_y*(1-i/4):.0f}</text>')
    points = [(left + ((p["start_s"]+p["end_s"])/2) / max_x * plot_w, top + plot_h * (1-p["cpm"]/max_y)) for p in series]
    path = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    line = f'<polyline fill="none" stroke="#117b76" stroke-width="3" points="{path}"/>'
    if len(points) == 1:
        line += f'<circle cx="{points[0][0]}" cy="{points[0][1]}" r="4" fill="#117b76"/>'
    return f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Скорость ввода во времени"><g font-family="sans-serif" font-size="11" fill="#667684">{"".join(lines)}{line}<text x="58" y="199">0 с</text><text x="735" y="199" text-anchor="end">{max_x:.1f} с активного времени</text><text x="58" y="12">зн/мин</text></g></svg>'


def histogram_svg(bins: list[dict]) -> str:
    top, base, height = 25, 175, 150
    maximum = max(1, max((b["count"] for b in bins), default=0))
    shapes = []
    for index, item in enumerate(bins):
        x = 65 + index * 170
        h = item["count"] / maximum * height
        upper = f'{item["to_ms"]/1000:g}' if item["to_ms"] else "∞"
        label = f'{item["from_ms"]/1000:g}–{upper} с'
        shapes.append(f'<rect x="{x}" y="{base-h}" width="110" height="{h}" rx="4" fill="#117b76"/><text x="{x+55}" y="{base-h-7}" text-anchor="middle">{item["count"]}</text><text x="{x+55}" y="200" text-anchor="middle">{label}</text>')
    return f'<svg viewBox="0 0 760 220" role="img" aria-label="Распределение длительности пауз"><g font-family="sans-serif" font-size="12" fill="#526574">{"".join(shapes)}</g></svg>'


def html_report(snapshot: dict) -> str:
    report = full_report(snapshot)
    m = report["overall"]
    settings = snapshot["settings"]
    profile_rows = "".join(f"<tr><th>{_safe(label)}</th><td>{_safe(snapshot['profile'].get(key, '—'))}</td></tr>" for key, label in PROFILE_LABELS.items())
    stats = [
        ("Активное время, с", fmt(m["active_seconds"])), ("Символов в итоговом тексте", m["characters"]),
        ("Слов / предложений", f'{m["words"]} / {m["sentences"]}'),
        ("Скорость, зн/мин", fmt(m["cpm"])), ("Скорость, усл. слов/мин", fmt(m["wpm"])),
        ("Итоговые слова / активные минуты", fmt(m["net_words_per_minute"])),
        ("Паузы, с", fmt(m["pause_seconds"])), ("Микро- / макропауз", f'{m["micro_pause_count"]} / {m["macro_pause_count"]}'),
        ("Индекс когнитивных задержек, %", fmt(m["cognitive_delay_index"])),
        ("Исправления, % удаленных / вставленных", fmt(m["correction_percent"])),
        ("Операций / циклов исправлений", f'{m["correction_operations"]} / {m["correction_cycles"]}'),
        ("Backspace / Delete", f'{m["backspace_count"]} / {m["delete_count"]}'),
        ("Удалений выделенного фрагмента", m["selection_deletions"]),
        ("Набрано / вставлено из буфера, символов", f'{m["typed_characters"]} / {m["pasted_characters"]}'),
        ("Hold Time: среднее / медиана / p95, мс", " / ".join(fmt(m["hold"][k]) for k in ("mean_ms", "median_ms", "p95_ms"))),
        ("Flight Time: среднее / медиана / p95, мс", " / ".join(fmt(m["flight"][k]) for k in ("mean_ms", "median_ms", "p95_ms"))),
        ("Полных / незавершенных пар клавиш", f'{m["hold"]["count"]} / {m["unpaired_key_downs"]}'),
        ("Лексическое разнообразие TTR", fmt(m["ttr"], 3)),
        ("Средняя длина слова, символов", fmt(m["mean_word_length"])),
        ("Средняя длина предложения, слов", fmt(m["mean_sentence_length"])),
    ]
    stats_rows = "".join(f"<tr><th>{_safe(label)}</th><td>{_safe(value)}</td></tr>" for label, value in stats)
    topic_rows = "".join(f'<tr><td>{_safe(t["title"])}</td><td>{fmt(t["active_seconds"])}</td><td>{t["characters"]}</td><td>{fmt(t["cpm"])}</td><td>{fmt(t["pause_seconds"])}</td><td>{fmt(t["ttr"],3)}</td></tr>' for t in report["topics"])
    texts = "".join(f'<h3>{_safe(title)}</h3><pre>{_safe(text) or "Текст не введен"}</pre>' for title, text in zip(settings["topics"], snapshot["texts"]))
    positions = {"within_word": "внутри слова", "between_words": "между словами", "before_punctuation": "перед пунктуацией", "other": "прочее"}
    pauses_at = "; ".join(f"{label}: {m['pause_positions'].get(key, 0)}" for key, label in positions.items())
    status = {"completed": "Завершена", "interrupted": "Прервана — отчет по последнему автосохранению"}.get(snapshot["status"], snapshot["status"])
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Отчет TextLab — {_safe(settings["title"])}</title>
<style>body{{font:15px/1.6 "Segoe UI",Arial,sans-serif;color:#203244;background:#edf2f4;margin:0}}main{{max-width:960px;margin:32px auto;padding:42px;background:#fff}}h1{{font-size:30px;line-height:1.2}}h2{{margin-top:36px;font-size:22px}}h3{{font-size:17px}}.brand{{color:#117b76;letter-spacing:3px;font-weight:700}}.sub{{color:#667684}}table{{width:100%;border-collapse:collapse;margin:15px 0}}th,td{{border:1px solid #dbe3e9;padding:9px 12px;text-align:left}}th{{font-weight:600;background:#f1f6f7}}thead th{{background:#203c51;color:white}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;padding:16px;background:#f6f8f9}}svg{{width:100%;height:auto}}.methods{{white-space:pre-line;font-size:13px}}@media print{{body{{background:white}}main{{padding:0;margin:0;max-width:none}}tr,svg{{break-inside:avoid}}h2,h3{{break-after:avoid}}thead{{display:table-header-group}}}}@page{{size:A4;margin:18mm}}</style></head><body><main>
<div class="brand">TEXTLAB / ИССЛЕДОВАНИЕ ВВОДА</div><h1>{_safe(settings["title"])}</h1><p>{_safe(settings.get("description", ""))}</p>
<p class="sub">{_safe(status)}<br>Начало: {_safe(snapshot["started_at"])}<br>Завершение: {_safe(snapshot.get("finished_at") or "—")}<br>UUID: {_safe(snapshot["session_id"])}</p>
<h2>Профиль и условия</h2><table>{profile_rows}</table>
<p>Режим: {"по темам" if settings["mode"] == "topics" else "сплошной текст"}. Ограничения: время {settings.get("time_limit_s",0):g} с; символы {settings.get("target_characters",0)}; слова {settings.get("target_words",0)}. Ноль означает отсутствие ограничения.</p>
<h2>Метрики сессии</h2><table>{stats_rows}</table><p>Положение пауз: {_safe(pauses_at)}.</p>
<h2>Скорость набора</h2>{speed_svg(m["speed_series"])}<p class="sub">Интервалы по {m["speed_series"][0]["end_s"]-m["speed_series"][0]["start_s"]:g} с; последний интервал может быть короче. WPM = CPM / 5.</p>
<h2>Распределение пауз</h2>{histogram_svg(m["pause_histogram"])}
<h2>Сравнение тем</h2><table><thead><tr><th>Тема</th><th>Время, с</th><th>Символы</th><th>Зн/мин</th><th>Паузы, с</th><th>TTR</th></tr></thead><tbody>{topic_rows}</tbody></table>
<h2>Итоговые тексты</h2>{texts}<h2>Методика расчета</h2><p>Порог микропауз: {settings.get("micro_pause_ms",300):g} мс; макропауз: {settings.get("macro_pause_ms",2000):g} мс. Границы диапазонов включаются слева.</p><p class="methods">{_safe(METHODS)}</p>
<p class="sub">TextLab 1.0 · схема данных {snapshot["schema_version"]} · событий {len(snapshot["events"])}</p></main></body></html>'''


def export_html(snapshot: dict, path: Path) -> None:
    atomic_text(path, html_report(snapshot))
