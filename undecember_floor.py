import os
import re
import json
import time
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import pytz
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# =========================
# 기본 설정
# =========================
BASE_URL = "https://ud.floor.line.games"
TARGET_URL_TEMPLATE = "https://ud.floor.line.games/kr/bbs/community/community_kr/{page}"
MAX_PAGE = int(os.getenv("MAX_PAGE", "3"))

TARGET_SHEET_NAME = os.getenv("TARGET_SHEET_NAME", "언디셈버_KR_플로어 동향")
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "").strip()

GOOGLE_SERVICE_ACCOUNT_JSON = (
    os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    or os.getenv("GOOGLE_CREDENTIALS", "").strip()
)

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
# 공지 제외 규칙
# =========================
NOTICE_PREFIXES = [
    "[공지사항]",
    "[알려진 현상]",
    "[업데이트]",
    "[이벤트]",
    "[모험일지]",
    "[코코의 편지]",
    "[콘텐츠 시간표]",
]


# =========================
# 키워드 분류 규칙
# =========================
POSITIVE_KEYWORDS = [
    "좋다", "좋아요", "재밌", "재미", "만족", "감사", "고맙", "최고", "잘했다", "왠일",
    "잘했", "호평", "추천", "대박", "갓겜", "멋지", "훌륭", "응원", "괜찮", "나쁘지", "혜자"
]

NEGATIVE_KEYWORDS = [
    "망", "망했", "망겜", "별로", "최악", "쓰레기", "노잼", "재미없", "불만", "불편",
    "운영", "잠수", "멈추", "멈춤", "튕김", "튕기", "팅겨", "오류", "버그", "렉",
    "문제", "짜증", "실망", "화난", "접는다", "도망", "환불", "안됩", "너프",
    "멈춰", "이따위", "튕겨", "팅김", "재접", "비싸", "비쌈", "과금", "말이되냐",
    "개선안됨", "안됨", "안된다", "터짐", "죽었", "욕", "핑계", "이따구", "창렬"
]

SUGGESTION_KEYWORDS = [
    "해주세요", "해줘", "부탁", "건의", "개선", "추가", "상향", "하향", "바꿔",
    "변경", "완화", "해주", "필요", "원합니다", "원해요", "고쳐", "수정",
    "해주면", "검토", "해줬으면", "풀어줘"
]


# =========================
# 유틸
# =========================
def now_kst() -> datetime:
    return datetime.now(KST)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


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
    if not time_text:
        return ""

    text = normalize_text(time_text)
    now = now_kst()

    m = re.match(r"(\d+)\s*분 전", text)
    if m:
        dt = now - timedelta(minutes=int(m.group(1)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    m = re.match(r"(\d+)\s*시간 전", text)
    if m:
        dt = now - timedelta(hours=int(m.group(1)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    m = re.match(r"(\d+)\s*일 전", text)
    if m:
        dt = now - timedelta(days=int(m.group(1)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    try:
        dt = date_parser.parse(text)

        if dt.tzinfo is None:
            dt = KST.localize(dt)

        return dt.strftime("%Y-%m-%d %H:%M:%S")

    except Exception:
        return text


def classify_post(title: str) -> str:
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
            matched.append(keyword)

    for keyword in NEGATIVE_KEYWORDS:
        if keyword.lower() in text:
            matched.append(keyword)

    for keyword in POSITIVE_KEYWORDS:
        if keyword.lower() in text:
            matched.append(keyword)

    matched = list(dict.fromkeys(matched))
    return ", ".join(matched)


def safe_execute(request, retries: int = 5):
    for attempt in range(retries):
        try:
            return request.execute()

        except HttpError as e:
            status = getattr(e.resp, "status", None)

            if status in [429, 500, 502, 503, 504]:
                wait = min(2 ** attempt, 30)

                logging.warning(
                    "Google API 일시 오류 발생 status=%s, %s초 후 재시도 (%s/%s)",
                    status,
                    wait,
                    attempt + 1,
                    retries
                )

                time.sleep(wait)
                continue

            raise

        except Exception:
            raise

    raise RuntimeError("Google API 재시도 후에도 실패했습니다.")


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


def fetch_board_html(session: requests.Session, page: int) -> str:
    url = TARGET_URL_TEMPLATE.format(page=page)

    logging.info("게시판 %s페이지 크롤링 시작: %s", page, url)

    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    return response.text


# =========================
# 게시글 파싱
# =========================
def parse_board_posts(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)

    posts = []
    seen_urls = set()

    meta_pattern = re.compile(
        r"""
        ^(?P<title>.+?)
        \s+
        (?P<view>\d+)
        (?:\s+(?P<like>\d+))?
        (?:\s+(?P<extra>\d+))?
        \s+
        \[(?P<guild>[^\]]+)\]
        \s+
        (?P<author>.+?)
        \s+
        (?P<time>\d+\s*분 전|\d+\s*시간 전|\d+\s*일 전|\d{4}\.\d{2}\.\d{2})
        $
        """,
        re.VERBOSE
    )

    for a in anchors:
        href = a.get("href", "").strip()

        if "/detail/" not in href and "/bbsCmn/detail/" not in href:
            continue

        raw_text = normalize_text(a.get_text(" ", strip=True))
        if not raw_text:
            continue

        if any(raw_text.startswith(prefix) for prefix in NOTICE_PREFIXES):
            continue

        url = resolve_url(href)

        if not url or url in seen_urls:
            continue

        seen_urls.add(url)

        post_id = extract_post_id(url)
        if not post_id:
            continue

        m = meta_pattern.match(raw_text)

        if m:
            title = normalize_text(m.group("title"))
            time_text = normalize_text(m.group("time"))

        else:
            time_match = re.search(
                r"(\d+\s*분 전|\d+\s*시간 전|\d+\s*일 전|\d{4}\.\d{2}\.\d{2})$",
                raw_text
            )

            time_text = time_match.group(1).strip() if time_match else ""

            title = raw_text
            if time_text:
                title = raw_text[:time_match.start()].strip()

            title = re.sub(
                r"""
                \s+\d+
                (?:\s+\d+)?
                (?:\s+\d+)?
                \s+\[[^\]]+\]
                \s+.+?
                \s*(\d+\s*분 전|\d+\s*시간 전|\d+\s*일 전|\d{4}\.\d{2}\.\d{2})?$
                """,
                "",
                title,
                flags=re.VERBOSE
            ).strip()

        title = normalize_text(title)

        if not title:
            continue

        sentiment = classify_post(title)
        matched_keywords = find_matched_keywords(title)

        posts.append({
            "post_id": post_id,
            "title": title,
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
        raise ValueError("GOOGLE_CREDENTIALS 또는 GOOGLE_SERVICE_ACCOUNT_JSON 환경변수가 비어 있습니다.")

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

    creds = Credentials.from_service_account_info(
        info,
        scopes=SCOPES
    )

    return build("sheets", "v4", credentials=creds)


def ensure_sheet_and_header(service):
    spreadsheet = safe_execute(
        service.spreadsheets().get(
            spreadsheetId=SPREADSHEET_ID
        )
    )

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

        safe_execute(
            service.spreadsheets().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body=body
            )
        )

    result = safe_execute(
        service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{TARGET_SHEET_NAME}'!A1:F2"
        )
    )

    values = result.get("values", [])

    if not values:
        header = [[
            "수집일자",
            "작성일자",
            "제목",
            "링크",
            "분류",
            "매칭 키워드"
        ]]

        safe_execute(
            service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"'{TARGET_SHEET_NAME}'!A1:F1",
                valueInputOption="RAW",
                body={"values": header}
            )
        )

        logging.info("헤더 생성 완료")


def get_existing_links(service) -> set:
    result = safe_execute(
        service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{TARGET_SHEET_NAME}'!D2:D"
        )
    )

    values = result.get("values", [])
    existing = set()

    for row in values:
        if row and row[0]:
            existing.add(str(row[0]).strip())

    logging.info("기존 링크 수: %d", len(existing))

    return existing


def append_rows(service, rows: List[List[str]]):
    if not rows:
        logging.info("추가할 신규 데이터 없음")
        return

    chunk_size = int(os.getenv("SHEETS_APPEND_CHUNK_SIZE", "50"))

    total = len(rows)

    for start in range(0, total, chunk_size):
        chunk = rows[start:start + chunk_size]

        safe_execute(
            service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=f"'{TARGET_SHEET_NAME}'!A:F",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": chunk}
            )
        )

        logging.info(
            "시트에 %d행 추가 완료 (%d/%d)",
            len(chunk),
            min(start + len(chunk), total),
            total
        )

        time.sleep(1)


# =========================
# 메인
# =========================
def run():
    logging.info("언디셈버 KR 플로어 동향 수집 시작")

    service = get_sheets_service()

    ensure_sheet_and_header(service)

    existing_links = get_existing_links(service)

    session = create_session()

    all_posts = []
    seen_urls = set()

    for page in range(1, MAX_PAGE + 1):
        try:
            html = fetch_board_html(session, page)
            posts = parse_board_posts(html)

            logging.info("%s페이지 파싱 게시글 수: %d", page, len(posts))

            for post in posts:
                if post["url"] in seen_urls:
                    continue

                seen_urls.add(post["url"])
                all_posts.append(post)

        except Exception as e:
            logging.exception("%s페이지 수집 실패: %s", page, e)

    logging.info("전체 파싱 게시글 수(중복 제거 후): %d", len(all_posts))

    new_posts = [
        p for p in all_posts
        if p["url"] not in existing_links
    ]

    logging.info("신규 게시글 수: %d", len(new_posts))

    collected_at = now_kst().strftime("%Y-%m-%d %H:%M:%S")

    rows = []

    for post in new_posts:
        written_at = post.get("time_iso_kst", "") or post.get("time_text", "")

        rows.append([
            collected_at,
            written_at,
            post["title"],
            post["url"],
            post["sentiment"],
            post["matched_keywords"],
        ])

    append_rows(service, rows)

    logging.info("작업 종료")


if __name__ == "__main__":
    run()
