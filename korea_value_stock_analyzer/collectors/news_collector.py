"""뉴스 수집기 (네이버 뉴스 검색 API, 선택)

NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 이 설정된 경우에만 동작함
수집 결과는 '언론 보도'로만 다루며 사실 여부를 판단하지 않음
"""
from __future__ import annotations

import html
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from core import config

API = "https://openapi.naver.com/v1/search/news.json"


class NewsCollector:
    def __init__(self, display: int = 30, interval: float = 0.15):
        self.cid = config.env("NAVER_CLIENT_ID")
        self.secret = config.env("NAVER_CLIENT_SECRET")
        self.display = display
        self.interval = interval

    @property
    def available(self) -> bool:
        return bool(self.cid and self.secret)

    def search(self, query: str, must_contain: list[str] | None = None) -> list[dict[str, Any]]:
        if not self.available:
            return []
        time.sleep(self.interval)
        r = requests.get(API, params={"query": query, "display": self.display, "sort": "sim"},
                         headers={"X-Naver-Client-Id": self.cid, "X-Naver-Client-Secret": self.secret}, timeout=15)
        r.raise_for_status()
        out = []
        for it in r.json().get("items", []):
            title = html.unescape(re.sub(r"<[^>]+>", "", it.get("title", "")))
            if must_contain and not all(m.replace(" ", "") in title.replace(" ", "") for m in must_contain):
                continue
            try:
                d = parsedate_to_datetime(it.get("pubDate"))
                ds, yr = d.date().isoformat(), d.year
            except (TypeError, ValueError):
                ds, yr = None, None
            out.append({"title": title, "link": it.get("originallink") or it.get("link"), "date": ds, "year": yr})
        return out
