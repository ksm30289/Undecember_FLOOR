import os
import re
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional

import pytz
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# =========================
# 기본 설정
# =========================
BASE_URL = "https://ud.floor.line.games"
TARGET_URL = "https://ud.floor.line.games/kr/bbs/community/community_kr/1"

TARGET_SHEET_NAME = os.getenv("TARGET_SHEET_NAME", "언디셈버_KR_플로어 동향")
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

KST = pytz.timezone("Asia/Seoul")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# =========================
# 키워드 분류 규칙
# =========================
POSITIVE_KEYWORDS = [
    "좋다", "좋아요", "재밌", "재미", "만족", "감사", "고맙", "최고", "잘했다",
    "잘했", "호평", "추천", "대박", "갓겜", "멋지", "훌륭", "응원", "괜찮", "나쁘지"
]

NEGATIVE_KEYWORDS = [
    "망", "망했", "망겜", "별로", "최악", "쓰레기", "노잼", "재미없", "불만", "불편",
    "오류", "버그", "렉", "문제", "짜증", "실망", "화난", "접는다", "도망가", "환불",
    "비싸", "비쌈", "과금", "말이되냐", "개선안됨", "안됨", "안된다", "터짐", "죽었"
]

SUGGESTION_KEYWORDS = [
    "해주세요", "해줘", "부탁", "건의", "개선", "추가", "상향", "하향", "바꿔", "변경",
    "필요", "원합니다", "원해요", "고쳐", "수정", "해주면", "검토", "해줬으면", "풀어줘"
]


# =========================
# 유틸
# =========================
def now_kst() -> datetime:
    return datetime.now(KST)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def safe_int(text: str, default: int = 0) -> int:
    if text is None:
        return default
    m = re.search(r"\d+", str(text).replace(",", ""))
    return int(m.group(0)) if m else default


def resolve_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return BASE_URL + href


def extract_post_id(url: str) -> Optional[str]:
    m = re.search(r"/detail/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/bbsCmn/detail/(\d+)", url)
    if m:
        return m.group(1)
    return None


def parse_time_text_to_iso(time_text: str) -> str:
    """
    예시:
    - 1일 전
    - 3일 전
    - 2026.03.18
    - 2026.03.25
    """
    if not time_text:
        return ""

    text = normalize_text(time_text)
    now = now_kst()

    m = re.match(r"(\d+)\s*분 전", text)
    if m:
        dt = now - pytz.timedelta(minutes=int(m.group(1)))
        return dt.isoformat()

    m = re.match(r"(\d+)\s*시간 전", text)
    if m:
        dt = now - pytz.timedelta(hours=int(m.group(1)))
        return dt.isoformat()

    m = re.match(r"(\d+)\s*일 전", text)
    if m:
        from datetime import timedelta
        dt = now - timedelta(days=int(m.group(1)))
        return dt.isoformat()

    try:
        dt = date_parser.parse(text)
        if dt.tzinfo is None:
            dt = KST.localize(dt)
        return dt.isoformat()
    except Exception:
        return ""


def classify_post(title: str) -> str:
    """
    우선순위:
    1) 건의
    2) 부정
    3) 긍정
    4) 기타
    """
    text = normalize_text(title).lower()

    if any(keyword.lower() in text for keyword in SUGGESTION_KEYWORDS):
        return "건의"

    if any(keyword.lower() in text for keyword in NEGATIVE_KEYWORDS):
        return "부정"

    if any(keyword.lower() in text for keyword in POSITIVE_KEYWORDS):
        return "긍정"

    return "기타"


def find_matched_keywords(title: str) -> str:
    text = normalize_text(title).lower()
    matched = []

    for keyword in SUGGESTION_KEYWORDS:
        if keyword.lower() in text:
            matched.append(f"건의:{keyword}")

    for keyword in NEGATIVE_KEYWORDS:
        if keyword.lower() in text:
            matched.append(f"부정:{keyword}")

    for keyword in POSITIVE_KEYWORDS:
        if keyword.lower() in text:
            matched.append(f"긍정:{keyword}")

    # 중복 제거
    matched = list(dict.fromkeys(matched))
    return ", ".join(matched)


# =========================
# HTTP
# =========================
def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": BASE_URL
    })
    return session


def fetch_board_html(session: requests.Session) -> str:
    logging.info("게시판 1페이지 크롤링 시작: %s", TARGET_URL)
    response = session.get(TARGET_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


# =========================
# 게시글 파싱
# =========================
def parse_board_posts(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)

    posts = []
    seen_ids = set()

    for a in anchors:
        href = a.get("href", "").strip()
        if "/detail/" not in href and "/bbsCmn/detail/" not in href:
            continue

        title = normalize_text(a.get_text(" ", strip=True))
        if not title:
            continue

        url = resolve_url(href)
        post_id = extract_post_id(url)
        if not post_id or post_id in seen_ids:
            continue

        seen_ids.add(post_id)

        # 공지 여부
        is_notice = title.startswith("[")
        category = ""
        category_match = re.match(r"^\[([^\]]+)\]", title)
        if category_match:
            category = category_match.group(1).strip()

        parent_text = normalize_text(a.parent.get_text(" ", strip=True)) if a.parent else title
        line_text = parent_text

        # title 제거 후 뒤 메타 읽기
        tail = line_text.replace(title, "", 1).strip()

        # 일반글 패턴 예시
        # 담시즌 허수맥스딜좀 풀어줘요... 154 1 1 [니쿠니쿠우니] 화니쿤 1일 전
        # 무기 부분 전승 153 2 [언디레져렉션] 뭔셈버 1일 전
        # 운영자 보아라 190 1 1 [悪魔をも屠れる] 캣타워철거반 3일 전

        numbers = re.findall(r"\b\d+\b", tail)
        view_count = safe_int(numbers[0], 0) if len(numbers) >= 1 else 0

        # 숫자 패턴이 2개면 보통 조회수 + 댓글수
        # 숫자 패턴이 3개면 조회수 + 추천수 + 댓글수 or 조회수 + 댓글수 + 추천수
        comment_count = 0
        like_count = 0

        if len(numbers) == 2:
            comment_count = safe_int(numbers[1], 0)
        elif len(numbers) >= 3:
            like_count = safe_int(numbers[1], 0)
            comment_count = safe_int(numbers[2], 0)

        guild_name = ""
        guild_match = re.search(r"\[([^\]]+)\]", tail)
        if guild_match:
            guild_name = guild_match.group(1).strip()

        time_text = ""
        time_match = re.search(
            r"(\d+\s*분 전|\d+\s*시간 전|\d+\s*일 전|\d{4}\.\d{2}\.\d{2})",
            tail
        )
        if time_match:
            time_text = time_match.group(1).strip()

        author = ""
        if time_text:
            left = tail.split(time_text)[0].strip()
            if guild_name:
                left = left.replace(f"[{guild_name}]", "").strip()
            tokens = left.split()
            if tokens:
                author = tokens[-1].strip()

        sentiment = classify_post(title)
        matched_keywords = find_matched_keywords(title)

        posts.append({
            "post_id": post_id,
            "is_notice": "Y" if is_notice else "N",
            "category": category,
            "title": title,
            "view_count": view_count,
            "like_count": like_count,
            "comment_count": comment_count,
            "guild_name": guild_name,
            "author": author,
            "time_text": time_text,
            "time_iso_kst": parse_time_text_to_iso(time_text),
            "url": url,
            "sentiment": sentiment,
            "matched_keywords": matched_keywords,
        })

    return posts


# =========================
# 구글 시트
# =========================
def get_sheets_service():
    if not SPREADSHEET_ID:
        raise ValueError("GOOGLE_SPREADSHEET_ID 환경변수가 비어 있습니다.")
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON 환경변수가 비어 있습니다.")

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def ensure_sheet_and_header(service):
    spreadsheet = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheets = spreadsheet.get("sheets", [])
    sheet_names = [s["properties"]["title"] for s in sheets]

    if TARGET_SHEET_NAME not in sheet_names:
        logging.info("시트 생성: %s", TARGET_SHEET_NAME)
        body = {
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": TARGET_SHEET_NAME
                        }
                    }
                }
            ]
        }
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body=body
        ).execute()

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{TARGET_SHEET_NAME}'!A1:O2"
    ).execute()

    values = result.get("values", [])
    if not values:
        header = [[
            "post_id",
            "is_notice",
            "category",
            "title",
            "view_count",
            "like_count",
            "comment_count",
            "guild_name",
            "author",
            "time_text",
            "time_iso_kst",
            "url",
            "sentiment",
            "matched_keywords",
            "collected_at_kst"
        ]]

        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{TARGET_SHEET_NAME}'!A1:O1",
            valueInputOption="RAW",
            body={"values": header}
        ).execute()
        logging.info("헤더 생성 완료")


def get_existing_post_ids(service) -> set:
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{TARGET_SHEET_NAME}'!A2:A"
    ).execute()

    values = result.get("values", [])
    existing = set()

    for row in values:
        if row and row[0]:
            existing.add(str(row[0]).strip())

    logging.info("기존 post_id 수: %d", len(existing))
    return existing


def append_rows(service, rows: List[List[str]]):
    if not rows:
        logging.info("추가할 신규 데이터 없음")
        return

    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{TARGET_SHEET_NAME}'!A:O",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()

    logging.info("시트에 %d행 추가 완료", len(rows))


# =========================
# 메인
# =========================
def run():
    logging.info("언디셈버 KR 플로어 동향 수집 시작")

    service = get_sheets_service()
    ensure_sheet_and_header(service)
    existing_post_ids = get_existing_post_ids(service)

    session = create_session()
    html = fetch_board_html(session)
    posts = parse_board_posts(html)

    logging.info("파싱된 게시글 수: %d", len(posts))

    new_posts = [p for p in posts if p["post_id"] not in existing_post_ids]
    logging.info("신규 게시글 수: %d", len(new_posts))

    collected_at = now_kst().isoformat()

    rows = []
    for post in new_posts:
        rows.append([
            post["post_id"],
            post["is_notice"],
            post["category"],
            post["title"],
            post["view_count"],
            post["like_count"],
            post["comment_count"],
            post["guild_name"],
            post["author"],
            post["time_text"],
            post["time_iso_kst"],
            post["url"],
            post["sentiment"],
            post["matched_keywords"],
            collected_at,
        ])

    append_rows(service, rows)
    logging.info("작업 종료")


if __name__ == "__main__":
    run()
