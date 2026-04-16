"""
아침 시황 보고서 (오전 7시 KST)
- 전일 미국 시장 (S&P500, NASDAQ, DOW, VIX)
- 환율 (KRW/USD)
- 글로벌 원자재 (금, 유가, 구리)
- 한국 주식 관련 뉴스 헤드라인
- PDF 생성 후 텔레그램 전송
"""
import os
import io
import time
import requests
import feedparser
import yfinance as yf
from datetime import datetime, timezone, timedelta
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── 설정 ──────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID   = os.environ['TELEGRAM_CHAT_ID']
KST       = timezone(timedelta(hours=9))

FONT_PATHS = [
    '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
    '/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf',
]

# ── 폰트 등록 ──────────────────────────────────────────────────────────────────
def register_fonts():
    regular = FONT_PATHS[0]
    bold    = FONT_PATHS[1]
    if Path(regular).exists():
        pdfmetrics.registerFont(TTFont('Korean', regular))
        pdfmetrics.registerFont(TTFont('KoreanBold', bold))
        return 'Korean', 'KoreanBold'
    return 'Helvetica', 'Helvetica-Bold'


# ── 시장 데이터 ────────────────────────────────────────────────────────────────
def fetch_market_data():
    tickers = {
        'S&P 500':  '^GSPC',
        'NASDAQ':   '^IXIC',
        'DOW':      '^DJI',
        'VIX':      '^VIX',
        'KRW/USD':  'KRW=X',
        '금(Gold)': 'GC=F',
        'WTI유가':  'CL=F',
        '구리':     'HG=F',
    }
    results = {}
    for name, sym in tickers.items():
        try:
            t = yf.Ticker(sym)
            hist = t.history(period='2d')
            if len(hist) >= 2:
                prev  = hist['Close'].iloc[-2]
                close = hist['Close'].iloc[-1]
                chg   = (close - prev) / prev * 100
                results[name] = {'close': close, 'chg': chg}
            elif len(hist) == 1:
                results[name] = {'close': hist['Close'].iloc[-1], 'chg': 0.0}
        except Exception as e:
            print(f'[{name}] 데이터 오류: {e}')
    return results


# ── 뉴스 수집 ──────────────────────────────────────────────────────────────────
def fetch_news(limit: int = 12):
    base = 'https://news.google.com/rss/search?hl=ko&gl=KR&ceid=KR:ko&q='
    queries = [
        '반도체+OR+SK하이닉스+OR+삼성전자+OR+HBM',
        '이차전지+OR+배터리+OR+에코프로+OR+LG에너지솔루션',
        '방산+OR+한화에어로스페이스+OR+방위산업',
        '코스피+OR+관세+OR+미국증시+OR+환율',
    ]
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=14)  # 전일 저녁 이후

    for q in queries:
        try:
            feed = feedparser.parse(base + q)
            for e in feed.entries[:5]:
                t = getattr(e, 'published_parsed', None) or getattr(e, 'updated_parsed', None)
                pub = datetime.fromtimestamp(time.mktime(t), tz=timezone.utc) if t else None
                if pub and pub < cutoff:
                    continue
                title = e.get('title', '').strip()
                link  = e.get('link', '').strip()
                if title and link:
                    items.append({'title': title, 'link': link, 'pub': pub})
        except Exception as ex:
            print(f'뉴스 오류: {ex}')

    # 제목 앞 20자 기준 중복 제거
    seen, unique = set(), []
    for item in items:
        key = item['title'][:20]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:limit]


# ── PDF 생성 ──────────────────────────────────────────────────────────────────
def build_pdf() -> bytes:
    font, font_bold = register_fonts()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=12*mm, bottomMargin=12*mm,
    )

    # 스타일
    def S(name, **kw):
        base = dict(fontName=font, fontSize=10, leading=14, textColor=colors.black)
        base.update(kw)
        return ParagraphStyle(name, **base)

    s_title   = S('title',   fontName=font_bold, fontSize=22, leading=28,
                  textColor=colors.HexColor('#1a1a2e'), spaceAfter=4)
    s_sub     = S('sub',     fontName=font_bold, fontSize=11, leading=14,
                  textColor=colors.HexColor('#16213e'), spaceBefore=10, spaceAfter=4)
    s_section = S('section', fontName=font_bold, fontSize=13, leading=18,
                  textColor=colors.white, backColor=colors.HexColor('#16213e'),
                  spaceBefore=8, spaceAfter=4, leftIndent=4, rightIndent=4)
    s_body    = S('body',    fontSize=9, leading=13, textColor=colors.HexColor('#333333'))
    s_news    = S('news',    fontSize=8.5, leading=13, textColor=colors.HexColor('#1a1a2e'))
    s_footer  = S('footer',  fontSize=8, leading=11, textColor=colors.grey, alignment=1)

    now_kst = datetime.now(KST)
    story = []

    # ── 헤더 ──
    story.append(Paragraph('📊 Morning Signal Report', s_title))
    story.append(Paragraph(
        f'<font color="#666666">{now_kst.strftime("%Y년 %m월 %d일 (%a) — 오전 7시 KST 기준")}</font>',
        S('date', fontSize=9, leading=13, textColor=colors.HexColor('#666666'))
    ))
    story.append(HRFlowable(width='100%', thickness=2, color=colors.HexColor('#16213e'), spaceAfter=8))

    # ── 미국 시장 ──
    story.append(Paragraph('🇺🇸  미국 시장 (전일 종가)', s_section))
    market = fetch_market_data()

    us_keys  = ['S&P 500', 'NASDAQ', 'DOW', 'VIX']
    fx_keys  = ['KRW/USD']
    com_keys = ['금(Gold)', 'WTI유가', '구리']

    def market_rows(keys, unit=''):
        rows = [['지수/자산', '종가', '전일 대비']]
        for k in keys:
            d = market.get(k)
            if d:
                chg = d['chg']
                arrow = '▲' if chg >= 0 else '▼'
                chg_color = '#2ecc71' if chg >= 0 else '#e74c3c'
                rows.append([
                    k,
                    f"{d['close']:,.2f}{unit}",
                    f'<font color="{chg_color}">{arrow} {abs(chg):.2f}%</font>',
                ])
            else:
                rows.append([k, '-', '-'])
        return rows

    def make_table(rows):
        col_w = [60*mm, 55*mm, 55*mm]
        t = Table([[Paragraph(str(c), S('tc', fontSize=9, leading=13,
                    fontName=(font_bold if r == 0 else font),
                    textColor=(colors.white if r == 0 else colors.black)))
                    for c in row]
                   for r, row in enumerate(rows)],
                  colWidths=col_w)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#16213e')),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8f9fa')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1),
             [colors.HexColor('#f8f9fa'), colors.HexColor('#ffffff')]),
            ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#cccccc')),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ]))
        return t

    story.append(make_table(market_rows(us_keys)))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph('💱  환율 / 원자재', s_section))
    story.append(make_table(market_rows(fx_keys + com_keys)))
    story.append(Spacer(1, 4*mm))

    # ── 시황 해석 ──
    sp = market.get('S&P 500', {})
    nq = market.get('NASDAQ', {})
    vx = market.get('VIX', {})
    kr = market.get('KRW/USD', {})

    def sentiment(chg):
        if chg >= 1.5:  return '강세 마감'
        if chg >= 0.3:  return '소폭 상승'
        if chg >= -0.3: return '보합 마감'
        if chg >= -1.5: return '소폭 하락'
        return '약세 마감'

    interp_parts = []
    if sp:
        interp_parts.append(f"S&P500 {sentiment(sp['chg'])}({sp['chg']:+.2f}%)")
    if nq:
        interp_parts.append(f"NASDAQ {sentiment(nq['chg'])}({nq['chg']:+.2f}%)")
    if vx:
        vix_comment = '공포 지수 상승 — 변동성 확대 주의' if vx['close'] > 25 else '변동성 안정권'
        interp_parts.append(f"VIX {vx['close']:.1f} ({vix_comment})")
    if kr:
        interp_parts.append(f"원/달러 {kr['close']:,.1f}원")

    if interp_parts:
        story.append(Paragraph('📝  시황 요약', s_sub))
        story.append(Paragraph(' · '.join(interp_parts), s_body))
        story.append(Spacer(1, 3*mm))

    # ── 뉴스 ──
    story.append(Paragraph('📰  오늘의 주요 뉴스', s_section))
    news_items = fetch_news(12)
    if news_items:
        for i, item in enumerate(news_items, 1):
            pub_str = ''
            if item['pub']:
                pub_str = f" <font color='#888888'>[{item['pub'].astimezone(KST).strftime('%H:%M')}]</font>"
            story.append(Paragraph(
                f'<b>{i}.</b> <a href="{item["link"]}" color="#1a5276">{item["title"]}</a>{pub_str}',
                s_news
            ))
            story.append(Spacer(1, 1.5*mm))
    else:
        story.append(Paragraph('수집된 뉴스가 없습니다.', s_body))

    # ── 푸터 ──
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#cccccc')))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f'자동 생성 · {now_kst.strftime("%Y-%m-%d %H:%M")} KST · Morning Signal Report',
        s_footer
    ))

    doc.build(story)
    return buf.getvalue()


# ── 텔레그램 전송 ─────────────────────────────────────────────────────────────
def send_pdf(pdf_bytes: bytes, filename: str, caption: str):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendDocument'
    resp = requests.post(url, data={
        'chat_id': CHAT_ID,
        'caption': caption,
        'parse_mode': 'HTML',
    }, files={
        'document': (filename, pdf_bytes, 'application/pdf'),
    }, timeout=30)
    if resp.status_code == 200:
        print('텔레그램 전송 완료')
    else:
        print(f'전송 실패: {resp.status_code} {resp.text}')


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    now_kst = datetime.now(KST)
    print(f'Morning Report 생성 중 — {now_kst.strftime("%Y-%m-%d %H:%M KST")}')

    pdf_bytes = build_pdf()
    filename  = f'morning_report_{now_kst.strftime("%Y%m%d")}.pdf'
    caption   = (
        f'📊 <b>Morning Signal Report</b>\n'
        f'{now_kst.strftime("%Y년 %m월 %d일")} — 오전 7시 KST\n'
        f'미국시장 · 환율 · 원자재 · 주요뉴스'
    )
    send_pdf(pdf_bytes, filename, caption)


if __name__ == '__main__':
    main()
