"""
한국 주식 뉴스 알림 시스템
- Google News RSS + 한경/연합뉴스 RSS 수집
- 키워드 기반 섹터/뉴스유형 분류
- 중복 방지 후 텔레그램 전송
"""
import json
import os
import time
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── 설정 ──────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
KST = timezone(timedelta(hours=9))
TIME_WINDOW = 35   # 분 (30분 간격 + 5분 버퍼)
MAX_SEND = 5       # 1회 최대 전송 건수
SENT_FILE = Path('sent_articles.json')

# ── 섹터 키워드 & 종목 ─────────────────────────────────────────────────────────
SECTORS = {
    '반도체/AI': {
        'keywords': [
            '삼성전자', 'SK하이닉스', '한미반도체', 'HPSP', '리노공업',
            '반도체', 'HBM', 'AI칩', '파운드리', '엔비디아', 'TSMC', 'D램', 'NAND',
        ],
        'stocks': {
            '삼성전자': '005930', 'SK하이닉스': '000660',
            '한미반도체': '042700', 'HPSP': '403870', '리노공업': '058470',
        },
    },
    '이차전지/EV': {
        'keywords': [
            'LG에너지솔루션', '삼성SDI', '에코프로', '포스코퓨처엠', '엘앤에프',
            '배터리', '이차전지', '전기차', 'EV', '양극재', '음극재', '전해질',
        ],
        'stocks': {
            'LG에너지솔루션': '373220', '삼성SDI': '006400',
            '에코프로': '086520', '포스코퓨처엠': '003670', '엘앤에프': '066970',
        },
    },
    '방산/우주': {
        'keywords': [
            '한화에어로스페이스', 'LIG넥스원', '현대로템', '한국항공우주',
            '방산', 'K방산', '방위산업', '미사일', '레이더', '위성', '무기수출',
        ],
        'stocks': {
            '한화에어로스페이스': '012450', 'LIG넥스원': '079550',
            '현대로템': '064350', '한국항공우주': '047810',
        },
    },
    '바이오/제약': {
        'keywords': [
            '삼성바이오로직스', '셀트리온', '알테오젠', '유한양행', '한미약품',
            'FDA', '임상', '신약', '바이오시밀러', '항체', '승인', '허가',
        ],
        'stocks': {
            '삼성바이오로직스': '207940', '셀트리온': '068270',
            '알테오젠': '196170', '유한양행': '000100', '한미약품': '128940',
        },
    },
}

NEWS_TYPES = {
    '정책/규제': [
        '정책', '규제', '법안', '지원', '보조금', 'IRA', '반도체법', '수출통제',
        '공정위', '금융위', '산업부', '제재', '금지령',
    ],
    '지정학/외교': [
        '관세', '무역전쟁', '무역갈등', '분쟁', '협약', '미중', '외교', '전쟁',
        '협정', '수출계약', '방산수출',
    ],
    '실적/공시': [
        '실적', '수주', '계약체결', '어닝', '공시', '증자', '분할', '인수합병',
        '영업이익', '매출', '서프라이즈', '쇼크', '대규모계약',
    ],
    '시장급변': [
        '급등', '급락', '폭등', '폭락', '서킷브레이커', '사이드카',
        '외국인순매수', '외국인순매도', '환율급등', '금리인상',
    ],
}

# 노이즈 필터
NOISE_PATTERNS = [
    '[광고]', '[PR]', 'AD:', '분석리포트', '목표주가 유지',
    '투자의견 유지', '소폭 조정', '주간 증시 전망', '투자자 유의',
]

# ── RSS 피드 목록 ──────────────────────────────────────────────────────────────
def get_feeds():
    base = 'https://news.google.com/rss/search?hl=ko&gl=KR&ceid=KR:ko&q='
    google_queries = [
        ('반도체/AI',    '반도체+OR+SK하이닉스+OR+삼성전자+OR+HBM+OR+AI칩'),
        ('이차전지/EV',  '이차전지+OR+배터리+OR+에코프로+OR+LG에너지솔루션'),
        ('방산/우주',    '방산+OR+한화에어로스페이스+OR+LIG넥스원+OR+방위산업'),
        ('바이오/제약',  '바이오+OR+FDA+OR+임상+OR+셀트리온+OR+삼성바이오'),
        ('시장/거시',    '코스피+OR+코스닥+OR+관세+OR+무역+OR+외국인+OR+환율'),
    ]
    feeds = [(name, base + q) for name, q in google_queries]
    feeds += [
        ('한국경제', 'https://www.hankyung.com/feed/all-news'),
        ('연합뉴스',  'https://www.yna.co.kr/rss/news.xml'),
        ('매일경제', 'https://www.mk.co.kr/rss/40300001/'),
        ('이데일리', 'https://rss.edaily.co.kr/edaily_stock.xml'),
    ]
    return feeds


# ── 유틸 ──────────────────────────────────────────────────────────────────────
def parse_pub_time(entry) -> datetime | None:
    for attr in ('published_parsed', 'updated_parsed'):
        t = getattr(entry, attr, None)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return None


def is_recent(pub_time: datetime | None) -> bool:
    if not pub_time:
        return False
    return (datetime.now(timezone.utc) - pub_time).total_seconds() <= TIME_WINDOW * 60


def is_noise(title: str) -> bool:
    return any(p in title for p in NOISE_PATTERNS)


def classify(title: str, summary: str = '') -> tuple[list, list, list]:
    text = title + ' ' + summary
    sectors, stocks, types = [], [], []

    for sector, info in SECTORS.items():
        if any(kw in text for kw in info['keywords']):
            sectors.append(sector)
            for name, code in info['stocks'].items():
                if name in text:
                    stocks.append(f'{name}({code})')

    for news_type, keywords in NEWS_TYPES.items():
        if any(kw in text for kw in keywords):
            types.append(news_type)

    return sectors, list(dict.fromkeys(stocks)), types


# ── 중복 관리 ──────────────────────────────────────────────────────────────────
def load_sent() -> set:
    if SENT_FILE.exists():
        data = json.loads(SENT_FILE.read_text(encoding='utf-8'))
        return set(data.get('urls', []))
    return set()


def save_sent(urls: set):
    url_list = list(urls)[-2000:]  # 최근 2000개만 유지
    SENT_FILE.write_text(
        json.dumps({'urls': url_list}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


# ── 텔레그램 ──────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    try:
        resp = requests.post(url, json={
            'chat_id': CHAT_ID,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': False,
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f'텔레그램 전송 오류: {e}')
        return False


def format_msg(title, link, pub_time, sectors, stocks, types) -> str:
    sector_str = ' | '.join(sectors) if sectors else '시장'
    type_str = ' · '.join(types) if types else '일반'
    kst_str = pub_time.astimezone(KST).strftime('%H:%M') if pub_time else '?'
    stocks_str = ', '.join(stocks) if stocks else '해당없음'
    return (
        f'🔔 <b>[{sector_str}]</b> {type_str}\n'
        f'📰 {title}\n'
        f'📌 관련 종목: {stocks_str}\n'
        f'🔗 {link}\n'
        f'⏰ 발행: {kst_str} KST'
    )


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    sent_urls = load_sent()
    candidates = []

    for source, feed_url in get_feeds():
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                url = entry.get('link', '').strip()
                title = entry.get('title', '').strip()
                summary = entry.get('summary', '')

                if not url or not title:
                    continue
                if url in sent_urls:
                    continue
                if is_noise(title):
                    continue

                pub_time = parse_pub_time(entry)
                if not is_recent(pub_time):
                    continue

                sectors, stocks, types = classify(title, summary)
                if not sectors:
                    continue

                candidates.append({
                    'title': title,
                    'url': url,
                    'pub_time': pub_time,
                    'sectors': sectors,
                    'stocks': stocks,
                    'types': types,
                    'score': len(sectors) * 3 + len(types) * 2 + len(stocks),
                })

        except Exception as e:
            print(f'[{source}] 피드 오류: {e}')

    # 점수 높은 순 정렬, 제목 앞 20자 기준 중복 제거
    seen = set()
    unique = []
    for c in sorted(candidates, key=lambda x: -x['score']):
        key = c['title'][:20]
        if key not in seen:
            seen.add(key)
            unique.append(c)

    to_send = unique[:MAX_SEND]
    new_urls = set()

    for item in to_send:
        msg = format_msg(
            item['title'], item['url'], item['pub_time'],
            item['sectors'], item['stocks'], item['types'],
        )
        if send_telegram(msg):
            new_urls.add(item['url'])
            print(f"전송: {item['title'][:50]}...")
        else:
            print(f"전송 실패: {item['title'][:50]}")

    if new_urls:
        save_sent(sent_urls | new_urls)
        print(f'총 {len(new_urls)}건 전송 완료')
    else:
        print('전송할 뉴스 없음')


if __name__ == '__main__':
    main()
