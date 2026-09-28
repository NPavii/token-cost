#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Считает расход токенов с момента прошлого вызова и стоимость в рублях.

Два режима:

1. Kimi Work (без аргументов путей): скрипт сам находит wire.jsonl текущей
   сессии Kimi Work по имени рабочей папки, состояние и журнал кладёт в неё.

2. Standalone (любой другой агент): передайте явные пути —
       python3 tokencost.py --journal agent.log --state .cost-state.json \
           --md "!CostLog.md" --log "описание задачи"

   Журнал агента — JSONL, по строке на вызов модели:
       {"usage":{"inputOther":N,"output":N,"inputCacheRead":N,
                 "inputCacheCreation":N},"time":1790000000000}
   (поле time в миллисекундах эпохи — опционально, нужно для длительности)

Скрипт печатает 3 строки (Вход / Выход / Рубли), а с ключом --log дописывает
строку в журнал задач (!CostLog.md) и обновляет блок «Итого». Ставки — в
config.json рядом со скриптом или в файле из --rates (рублей за 1 млн токенов).
"""
import argparse
import glob
import json
import os
import re
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "rub_per_million": {
        "input": 276.0,
        "output": 1380.0,
        "cache_read": 27.6,
        "cache_creation": 276.0,
    }
}

USAGE_RE = re.compile(r'"usage":\{([^}]*)\}')
TIME_RE = re.compile(r'"time":(\d+)')

LOG_HEADER = """# Журнал расхода токенов

Оценка стоимости — API-эквивалент Kimi K3 (вход 276 ₽/млн, выход 1 380 ₽/млн, кэш 27,6 ₽/млн).
Ставки меняются в config.json рядом со скриптом (или в файле из --rates).

| Когда | Задача | Вход | Выход | Рубли | Длительность |
|---|---|---:|---:|---:|---|
"""

TOTALS_MARKER = "## Итого"

# пути по умолчанию (режим Kimi Work); переопределяются аргументами
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
STATE_PATH = os.path.join(os.getcwd(), ".tokencost-state.json")
LOG_PATH = os.path.join(os.getcwd(), "!CostLog.md")
SESSIONS_BASE = r"D:\KimiData\daimon-share\daimon\runtime\kimi-code\home\sessions"


def load_config(rates_path):
    try:
        with open(rates_path, encoding="utf-8") as f:
            cfg = json.load(f)
        rates = dict(DEFAULT_CONFIG["rub_per_million"])
        rates.update(cfg.get("rub_per_million", {}))
        return rates
    except Exception:
        return dict(DEFAULT_CONFIG["rub_per_million"])


def find_wire():
    """Находит wire.jsonl текущей сессии Kimi Work: среди сессий текущего
    рабочего проекта (wd_<имя>_) берёт самый свежий по mtime, иначе — самый
    свежий файл вообще. Возвращает None, если сессии не найдены."""
    pattern = os.path.join(SESSIONS_BASE, "*", "*", "agents", "main", "wire.jsonl")
    candidates = []
    for path in glob.glob(pattern):
        try:
            candidates.append((os.path.getmtime(path), path))
        except OSError:
            continue
    if not candidates:
        return None

    workspace = os.path.basename(os.getcwd()).lower()
    matched = [
        (mtime, path) for mtime, path in candidates
        if os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(path))))).lower()
        .startswith("wd_" + workspace)
    ]
    pool = matched if matched else candidates
    pool.sort(key=lambda item: item[0])
    return pool[-1][1]


def load_state(state_path):
    try:
        with open(state_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state_path, state):
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def sum_usage(path, offset):
    """Суммирует usage и время (мс) первой/последней записи в сегменте."""
    totals = {"inputOther": 0, "output": 0, "inputCacheRead": 0, "inputCacheCreation": 0}
    first_time = None
    last_time = None
    prev_block = None
    try:
        # бинарный режим: f.tell() после seek/чтения надёжен,
        # при текстовом чтении с errors="ignore" tell() врёт
        with open(path, "rb") as f:
            f.seek(offset)
            while True:
                raw = f.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="ignore")
                if '"usage"' not in line:
                    continue
                for match in USAGE_RE.finditer(line):
                    try:
                        block = json.loads("{" + match.group(1) + "}")
                    except ValueError:
                        continue
                    # wire.jsonl пишет usage дважды на вызов (две записи
                    # с идентичным блоком) — дубликат пропускаем
                    if block == prev_block:
                        continue
                    prev_block = block
                    for key in totals:
                        totals[key] += block.get(key, 0)
                time_match = TIME_RE.search(line)
                if time_match:
                    moment = int(time_match.group(1))
                    if first_time is None:
                        first_time = moment
                    last_time = moment
            offset = f.tell()
    except OSError:
        pass
    return totals, offset, first_time, last_time


def format_tokens(value):
    return "{:,}".format(value).replace(",", " ")


def format_rubles(value):
    return "{:,.2f}".format(value).replace(",", " ").replace(".", ",")


def format_duration(first_moment, last_moment):
    if first_moment is None or last_moment is None:
        return "—"
    seconds = max(0, round((last_moment - first_moment) / 1000))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return "{} ч {} мин".format(hours, minutes)
    if minutes:
        return "{} мин {} с".format(minutes, seconds)
    return "{} с".format(seconds)


def compute_totals(log_path):
    """Суммирует строки таблицы журнала: (задач, вход, выход, рубли)."""
    tasks = 0
    tokens_in = 0
    tokens_out = 0
    rubles = 0.0
    if not os.path.exists(log_path):
        return tasks, tokens_in, tokens_out, rubles
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("|") or line.startswith("|---") or "Когда" in line:
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) < 5:
                continue
            try:
                tokens_in += int(cells[2].replace(" ", ""))
                tokens_out += int(cells[3].replace(" ", ""))
                rubles += float(cells[4].replace(" ", "").replace(",", "."))
                tasks += 1
            except ValueError:
                continue
    return tasks, tokens_in, tokens_out, rubles


def refresh_totals(log_path):
    """Переписывает блок «Итого» в конце журнала по текущим строкам."""
    tasks, tokens_in, tokens_out, rubles = compute_totals(log_path)
    block = "\n{marker}\n\nЗадач: {tasks} · Вход: {tin} · Выход: {tout} · Рубли: {rub} ₽\n".format(
        marker=TOTALS_MARKER,
        tasks=tasks,
        tin=format_tokens(tokens_in),
        tout=format_tokens(tokens_out),
        rub=format_rubles(rubles),
    )
    content = ""
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            content = f.read()
        marker_at = content.find("\n" + TOTALS_MARKER)
        if marker_at != -1:
            content = content[:marker_at] + "\n"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(content.rstrip("\n") + block)


def append_log(log_path, task, tokens_in, tokens_out, rubles, duration):
    if not os.path.exists(log_path):
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(LOG_HEADER)
    moment = datetime.now().strftime("%d.%m.%Y %H:%M")
    row = "| {} | {} | {} | {} | {} | {} |\n".format(
        moment,
        task.replace("|", "/"),
        format_tokens(tokens_in),
        format_tokens(tokens_out),
        format_rubles(rubles),
        duration,
    )
    # вставляем строку ПЕРЕД блоком «Итого», а не в конец файла:
    # иначе refresh_totals() обрежет её по маркеру и запись потеряется
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            content = f.read()
    else:
        content = LOG_HEADER
    marker_at = content.find("\n" + TOTALS_MARKER)
    if marker_at != -1:
        new_content = content[:marker_at].rstrip("\n") + "\n" + row + content[marker_at:]
    else:
        if not content.endswith("\n"):
            content += "\n"
        new_content = content + row
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    refresh_totals(log_path)


def main():
    parser = argparse.ArgumentParser(description="Подсчёт токенов задачи и стоимости в рублях")
    parser.add_argument("--log", default=None,
                        help="краткое описание задачи — дописать строку в журнал !CostLog.md")
    parser.add_argument("--total", action="store_true",
                        help="показать итоги журнала и обновить блок «Итого»")
    parser.add_argument("--journal", default=None,
                        help="путь к файлу журнала вызовов (JSONL с usage); "
                             "по умолчанию — автопоиск сессии Kimi Work")
    parser.add_argument("--state", default=None,
                        help="файл состояния дельты (по умолчанию .tokencost-state.json в cwd)")
    parser.add_argument("--md", default=None,
                        help="путь к журналу задач (по умолчанию !CostLog.md в cwd)")
    parser.add_argument("--rates", default=None,
                        help="config.json со ставками (по умолчанию config.json рядом со скриптом)")
    args = parser.parse_args()

    global CONFIG_PATH, STATE_PATH, LOG_PATH
    CONFIG_PATH = args.rates or CONFIG_PATH
    state_path = args.state or STATE_PATH
    log_path = args.md or LOG_PATH

    rates = load_config(CONFIG_PATH)
    wire = args.journal or find_wire()
    if wire is None:
        print("Вход: 0 токенов")
        print("Выход: 0 токенов")
        print("Рубли: 0,00 ₽")
        return

    state = load_state(state_path)
    prev = state if state.get("path") == wire else None
    offset = 0 if prev is None else prev.get("offset", 0)
    totals, new_offset, first_time, last_time = sum_usage(wire, offset)
    save_state(state_path, {"path": wire, "offset": new_offset})

    tokens_in = totals["inputOther"] + totals["inputCacheRead"] + totals["inputCacheCreation"]
    tokens_out = totals["output"]

    rubles = (
        totals["inputOther"] * rates["input"]
        + totals["inputCacheRead"] * rates["cache_read"]
        + totals["inputCacheCreation"] * rates["cache_creation"]
        + totals["output"] * rates["output"]
    ) / 1_000_000.0

    duration = format_duration(first_time, last_time)

    if args.log and (tokens_in or tokens_out):
        append_log(log_path, args.log, tokens_in, tokens_out, rubles, duration)
    elif args.total:
        refresh_totals(log_path)

    if args.total:
        total_tasks, total_in, total_out, total_rub = compute_totals(log_path)
        print("Итого: {} задач · Вход: {} · Выход: {} · Рубли: {} ₽".format(
            total_tasks, format_tokens(total_in), format_tokens(total_out), format_rubles(total_rub)))

    print("Вход: {} токенов".format(format_tokens(tokens_in)))
    print("Выход: {} токенов".format(format_tokens(tokens_out)))
    print("Рубли: {} ₽".format(format_rubles(rubles)))


if __name__ == "__main__":
    main()
