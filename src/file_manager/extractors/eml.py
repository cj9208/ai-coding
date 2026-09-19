from __future__ import annotations

from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from .base import ExtractionResult, Extractor


class EmlExtractor(Extractor):
    extensions = frozenset({"eml"})

    def extract(self, path: Path) -> ExtractionResult:
        msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        text = self._body(msg)
        extra = {
            "email_from": str(msg.get("from", "")),
            "email_to": str(msg.get("to", "")),
            "email_date": self._date(msg),
        }
        return ExtractionResult(
            title=str(msg.get("subject", "")) or None, text=text, extra=extra
        )

    @staticmethod
    def _content(part: Message) -> str | None:
        """取正文文本。

        get_content() 只在 EmailMessage 上存在（policy.default 解析出来的就是它），
        但类型标注是 Message，这里防御性地查一次方法。
        """
        get_content = getattr(part, "get_content", None)
        if get_content is None:
            return None
        try:
            value = get_content()
        except Exception:  # noqa: BLE001 - 未知内容类型不阻塞上传
            return None
        return value if isinstance(value, str) else None

    @classmethod
    def _body(cls, msg: Message) -> str | None:
        # 优先 text/plain，其次 text/html（不引入额外依赖）
        if msg.is_multipart():
            plain, html = None, None
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain" and plain is None:
                    plain = cls._content(part)
                elif ctype == "text/html" and html is None:
                    html = cls._content(part)
            return plain or html
        return cls._content(msg)

    @staticmethod
    def _date(msg: Message) -> str:
        raw = msg.get("date")
        if not raw:
            return ""
        try:
            return parsedate_to_datetime(str(raw)).isoformat()
        except (TypeError, ValueError):
            return str(raw)
