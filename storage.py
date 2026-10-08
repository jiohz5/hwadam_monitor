"""일반 설정과 텔레그램 비밀값을 분리해 저장한다."""
from dataclasses import asdict, fields
import json
import os
from pathlib import Path
import re
import tempfile

from model import WatchConfig


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


class SettingsStore:
    def __init__(self, base: Path):
        self.path = Path(base) / "settings.json"

    def load(self):
        if not self.path.exists():
            return WatchConfig(), None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            values = {key: value for key, value in raw.items() if key in {item.name for item in fields(WatchConfig)}}
            for key in ("time_specs", "mono_specs"):
                if key in values:
                    values[key] = tuple(values[key])
            config = WatchConfig(**values).validate()
            return config, None
        except (ValueError, TypeError, AttributeError, OSError):
            return WatchConfig(), "설정 파일을 읽지 못해 기본값을 사용합니다. 조건을 확인한 뒤 저장하세요."

    def save(self, config: WatchConfig):
        config.validate()
        atomic_write(self.path, json.dumps(asdict(config), ensure_ascii=False, indent=2))


def load_telegram_secrets(base: Path, *, environ=None):
    values = {}
    root = Path(base).parent
    for directory in (root / "cgv_grok_new", root / "hwadam_claude", Path(base)):
        path = directory / "helper_secrets.env"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and not key.startswith("#"):
                values[key] = value.strip().strip('"').strip("'")
    environment = os.environ if environ is None else environ
    values.update({key: value for key, value in environment.items() if value})
    def pick(suffix):
        return next((values[prefix + suffix] for prefix in ("HWADAM_CODEX_TG_", "HWADAM_TG_", "CGV_TG_")
                     if values.get(prefix + suffix)), "")
    return pick("TOKEN"), pick("CHAT_ID")


def save_telegram_secrets(base: Path, token: str, chat_id: str):
    if "\n" in token + chat_id or "\r" in token + chat_id:
        raise ValueError("텔레그램 설정에 줄바꿈을 넣을 수 없습니다")
    atomic_write(Path(base) / "helper_secrets.env",
                 f"HWADAM_CODEX_TG_TOKEN={token.strip()}\nHWADAM_CODEX_TG_CHAT_ID={chat_id.strip()}\n")


def sanitize(text: str):
    text = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{8,}\b", "<봇 토큰 숨김>", str(text))
    return re.sub(r"(orderGroupId=)[^&\s]+", r"\1<주문번호 숨김>", text)
