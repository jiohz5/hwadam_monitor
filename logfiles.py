"""한국 날짜별 실행 로그와 원본을 보존하는 기존 로그 분리 도구."""
from dataclasses import dataclass
import datetime as dt
import logging
from pathlib import Path
import re
import sys
import uuid

from gui import KST
from storage import atomic_write, sanitize


class DailyLogHandler(logging.Handler):
    """처리 시각이 아닌 이벤트의 한국 시각으로 파일을 선택한다."""

    def __init__(self, directory: Path, run_id: str):
        super().__init__()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._day = None
        self._stream = None

    def emit(self, record):
        # Handler.handle의 잠금은 파일 교체와 쓰기를 한 작업으로 보호한다.
        if self._closed:
            return
        timestamp = dt.datetime.fromtimestamp(record.created, KST)
        day = timestamp.date().isoformat()
        event = str(getattr(record, "event", "APP"))
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", event):
            event = "APP"
        message = record.getMessage()
        if record.exc_info and record.exc_info[0]:
            message += f" error_type={record.exc_info[0].__name__}"
        # Formatter의 예외/스택 자동 첨부를 사용하지 않아 원문 비밀값을 남기지 않는다.
        line = (f"{timestamp.isoformat(sep=' ', timespec='milliseconds')} {record.levelname} "
                f"run={self.run_id} event={event} {message}")
        try:
            if self._day != day:
                self._discard_stream()
                self._stream = (self.directory / f"hwadam-{day}.log").open("a", encoding="utf-8")
                self._day = day
            self._stream.write(sanitize(line) + "\n")
            self._stream.flush()
        except OSError:
            self._discard_stream()
            self.handleError(record)

    def handleError(self, record):
        # 기본 훅의 traceback/원문 record 출력 대신 안전한 오류 유형만 알린다.
        if logging.raiseExceptions and sys.stderr is not None:
            error_type = type(sys.exc_info()[1]).__name__
            try:
                sys.stderr.write(f"화담 로그 파일 기록 실패 error_type={error_type}\n")
            except (OSError, ValueError):
                pass

    def _discard_stream(self):
        stream = self._stream
        self._stream = None
        self._day = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def flush(self):
        self.acquire()
        try:
            if self._stream is not None:
                try:
                    self._stream.flush()
                except OSError:
                    self._discard_stream()
                    self.handleError(None)
        finally:
            self.release()

    def close(self):
        self.acquire()
        try:
            self._discard_stream()
            super().close()
        finally:
            self.release()


def create_app_logger(base_dir: Path) -> tuple[logging.Logger, DailyLogHandler]:
    """앱 실행마다 독립된 로거와 종료 시 닫을 핸들러를 반환한다."""
    identity = uuid.uuid4().hex
    logger = logging.getLogger(f"hwadam.{identity}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = DailyLogHandler(Path(base_dir) / "logs", identity[:8])
    logger.addHandler(handler)
    return logger, handler


@dataclass(frozen=True)
class LegacySplitSummary:
    source_paths: tuple[Path, ...]
    output_paths: tuple[Path, ...]
    record_count: int
    line_count: int


def split_legacy_logs(base_dir: Path) -> LegacySplitSummary:
    """hwadam.log와 숫자 백업을 날짜별 스냅샷으로 분리한다.

    호출자가 기존 로그 기록을 중단한 뒤 명시적으로 실행한다. 원본은 보존하고
    파생 파일은 매번 원자적으로 덮어써 동일 입력을 재실행해도 중복되지 않는다.
    """
    directory = Path(base_dir) / "logs"
    rotations = []
    for path in directory.glob("hwadam.log.*"):
        match = re.fullmatch(r"hwadam\.log\.(\d+)", path.name)
        if match and path.is_file():
            rotations.append((int(match[1]), path))
    sources = [path for _, path in sorted(rotations, key=lambda item: item[0], reverse=True)]
    current = directory / "hwadam.log"
    if current.is_file():
        sources.append(current)

    groups = {}
    day = "undated"
    records = lines = 0
    for source in sources:
        for line in source.read_text(encoding="utf-8-sig").splitlines(keepends=True):
            match = re.match(r"^(\d{4}-\d{2}-\d{2})[ T]\d{2}:\d{2}:\d{2}", line)
            if match:
                try:
                    day = dt.date.fromisoformat(match[1]).isoformat()
                    records += 1
                except ValueError:
                    pass
            groups.setdefault(day, []).append(line)
            lines += 1

    outputs = []
    for day, content in groups.items():
        target = directory / "legacy" / f"{day}.log"
        atomic_write(target, "".join(content))
        outputs.append(target)
    return LegacySplitSummary(tuple(sources), tuple(outputs), records, lines)
