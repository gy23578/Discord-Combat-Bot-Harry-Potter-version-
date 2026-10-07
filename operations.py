"""Small operational helpers; no Discord or gameplay dependencies."""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class SecretFormatter(logging.Formatter):
    def __init__(self, secret):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secret = secret

    def format(self, record):
        result = super().format(record)
        return result.replace(self.secret, "[REDACTED]") if self.secret else result


def configure_logging(token=None):
    directory = Path(__file__).resolve().parent / "logs"
    directory.mkdir(exist_ok=True, mode=0o700)
    formatter = SecretFormatter(token)
    console = logging.StreamHandler()
    file_handler = RotatingFileHandler(directory / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    for handler in (console, file_handler):
        handler.setFormatter(formatter)
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    levels = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}
    logging.basicConfig(level=levels.get(level, logging.INFO), handlers=[console, file_handler], force=True)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


class ProcessLock:
    """Prevent two processes sharing this application's SQLite/live state."""
    def __init__(self, path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.file = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.write("0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise SystemExit("Another bot process is already running for this application.")
        return self

    def __exit__(self, *args):
        self.file.close()
