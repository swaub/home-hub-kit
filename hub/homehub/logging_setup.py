import faulthandler
import json
import logging
import re
import sys
import warnings
from datetime import date, datetime
from pathlib import Path

FILE_PREFIX = "homehub-"
FILE_SUFFIX = ".log"
CRASH_FILE = "crash.log"
FILE_DATE = re.compile(r"^homehub-(\d{4}-\d{2}-\d{2})\.log$")
EVENT_LOGGER = "homehub.events"
SECRET_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-[redacted]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "ghp_[redacted]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd)\s*[=:]\s*\S+"), r"\1=[redacted]"),
]
_crash_file = None


def redact(text):
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def log_file_name(day):
    return f"{FILE_PREFIX}{day.isoformat()}{FILE_SUFFIX}"


def log_file_date(path):
    m = FILE_DATE.match(Path(path).name)
    return date.fromisoformat(m.group(1)) if m else None


def prune(directory, retention_days, today=None):
    today = today or date.today()
    removed = []
    for path in Path(directory).glob(f"{FILE_PREFIX}*{FILE_SUFFIX}"):
        day = log_file_date(path)
        if day is not None and (today - day).days >= retention_days:
            path.unlink()
            removed.append(path)
    return removed


class RedactingFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")

    def format(self, record):
        return redact(super().format(record))


class DailyFileHandler(logging.Handler):
    def __init__(self, directory, retention_days):
        super().__init__()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.day = None
        self.stream = None
        self.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        self.rollover(date.today())

    @property
    def path(self):
        return self.directory / log_file_name(self.day)

    def rollover(self, today):
        with self.lock:
            if self.day == today and self.stream is not None:
                return
            if self.stream is not None:
                self.stream.close()
            self.day = today
            self.stream = open(self.path, "a", encoding="utf-8")
            prune(self.directory, self.retention_days, today)

    def emit(self, record):
        try:
            self.rollover(date.today())
            line = self.format(record)
            with self.lock:
                self.stream.write(line + "\n")
                self.stream.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        with self.lock:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
        super().close()


def setup_logging(state_dir, retention_days=30, console=False, level=logging.INFO):
    directory = Path(state_dir).expanduser() / "logs"
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, (DailyFileHandler, logging.StreamHandler)) and getattr(handler, "homehub", False):
            root.removeHandler(handler)
            handler.close()
    file_handler = DailyFileHandler(directory, retention_days)
    file_handler.homehub = True
    root.addHandler(file_handler)
    if console:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.homehub = True
        stream_handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(stream_handler)
    root.setLevel(level)
    logging.captureWarnings(True)
    warnings.simplefilter("default")
    enable_crash_log(directory)
    return directory


def enable_crash_log(directory):
    global _crash_file
    path = Path(directory) / CRASH_FILE
    if _crash_file is not None and Path(_crash_file.name) == path and faulthandler.is_enabled():
        return path
    if _crash_file is not None:
        faulthandler.disable()
        _crash_file.close()
    _crash_file = open(path, "a", encoding="utf-8")
    faulthandler.enable(file=_crash_file, all_threads=True)
    return path


def log_event(name, payload):
    logging.getLogger(EVENT_LOGGER).info("%s %s", name, json.dumps(payload, default=str, ensure_ascii=False))


def parse_event_line(line):
    message = line.split(f" INFO {EVENT_LOGGER}: ", 1)
    if len(message) != 2:
        return None
    name, _, body = message[1].partition(" ")
    return name, json.loads(body)
