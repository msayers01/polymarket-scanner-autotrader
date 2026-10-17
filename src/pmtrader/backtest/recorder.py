"""Record raw CLOB market-channel messages to gzip JSONL for replay/backtests and markout studies.

Line format: {"t": <recv epoch s>, "m": <raw message string>}. Files rotate hourly.
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from typing import IO


class Recorder:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._fh: IO[str] | None = None
        self._hour = ""
        self.lines = 0

    def _rotate(self, t: float) -> IO[str]:
        hour = time.strftime("%Y%m%d-%H", time.gmtime(t))
        if hour != self._hour or self._fh is None:
            if self._fh is not None:
                self._fh.close()
            self._hour = hour
            self._fh = gzip.open(self.out_dir / f"clob-{hour}.jsonl.gz", "at", encoding="utf-8")  # noqa: SIM115
        return self._fh

    def __call__(self, recv: float, raw: str | bytes) -> None:
        if raw in ("PONG", b"PONG"):
            return
        text = raw.decode() if isinstance(raw, bytes) else raw
        self._rotate(recv).write(json.dumps({"t": round(recv, 4), "m": text}) + "\n")
        self.lines += 1

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def iter_recording(path: Path):  # type: ignore[no-untyped-def]
    """Yield (recv_ts, message_dict_or_list) from a recording file."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            yield row["t"], json.loads(row["m"])
