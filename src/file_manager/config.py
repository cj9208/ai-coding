from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# repo-root anchoring per AGENTS.md; single definition in utils.paths so a
# launch from any cwd cannot create a second database elsewhere
from utils import paths


@dataclass(frozen=True)
class Settings:
    """运行配置。可用环境变量覆盖：FM_DATA_DIR / FM_DATABASE_URL / FM_MAX_UPLOAD_MB。"""

    data_dir: Path
    db_url: str
    max_upload_mb: int = 50
    extracted_text_cap: int = 200_000  # 抽取文本入库上限，防止超大 PDF 撑爆数据库

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("FM_DATA_DIR", paths.data_dir("file_manager")))
    db_url = os.environ.get(
        "FM_DATABASE_URL", f"sqlite:///{data_dir / 'file_manager.db'}"
    )
    max_upload_mb = int(os.environ.get("FM_MAX_UPLOAD_MB", "50"))
    return Settings(data_dir=data_dir, db_url=db_url, max_upload_mb=max_upload_mb)
