"""
저녁 시황 보고서 (오후 5시 KST)
- 당일 KOSPI / KOSDAQ 지수 및 등락
- 상위 상승/하락 종목
- 외국인·기관 순매수/매도 상위
- 섹터별 흐름 요약
- 당일 주요 뉴스
- PDF 생성 후 텔레그램 전송
"""
import os
import io
import time
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from pykrx import stock
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

# 관심 섹터 대표 종목
SECTOR_STOCKS = {
    '반도체/AI':   ['005930', '000660', '042700'],   # 삼성전자, SK하이닉스, 한미반도체
    '이차전지/EV': ['373220', '006400', '086520'],   # LG에너지솔루션, 삼성SDI, 에코프로
    '방산/우주':   ['012450', '079550', '064350'],   # 한화에어로스페이스, LIG넥스원, 현대로템
    '바이오/제약': ['207940', '068270', '196170'],   # 삼성바이오, 셀트리온, 알테오젠
}


# ── 폰트 등록 ──────────────────────────────────────────────────────────────────
def register_fonts():
    regular = FONT_PATHS[0]
    bold    = FONT_PATHS[1]
    if Path(regular).exists():
        pdfmetrics.registerFont(TTFont('Korean', regular))
        pdfmetrics.registerFont(TTFont('KoreanBold', bold))
        return 'Korean', 'KoreanBold'
    return 'Helvetica', 'Helvetica-Bold'


# ── 시장 데이터 (pykrx) ────────────────────────────────────────────────────────
def fetch_krx_data(date_str: str):
    """KOSPI/KOSDAQ 지수, 상위 종목, 외국인/기관 순매수 수집"""
    result = {}

    # 지수
    for market in ('KOSPI', 'KOSDAQ'):
        try:
            df = stock.get_index_ohlcv_by_date(date_str, date_str, market)
            if not df.empty:
                row = df.iloc[-1]
                prev_close = row['시가'] / (1 + row['등락률'] / 100) if row['등락률'] != -100 else row['시가']
                result[market] = {
                    'close':  row['종가'],
                    'chg':    row['등락률'],
                    'vol':    row['거래량'],
                }
        except Exception as e:
            print(f'[{market}] 지수 오류: {e}')

    # 상위 상승/하락 종목 (KOSPI)
    try:
        df_top = stock.get_market_ohlcv_by_ticker(date_str, market='KOSPI')
        df_top = df_top[df_top['거래량'] > 0].copy()
        df_top['등락률'] = (df_top['종가'] - df_top['시가']) / df_top['시가'] * 100
        # 종목명 조회
        names = {}
        for ticker in df_top.index[:]:
            try:
                names[ticker] = stock.get_market_ticker_name(ticker)
            except:
                names[ticker] = ticker
        df_top['종목명'] = df_top.index.map(lambda x: names.get(x, x))
        result['top_rise']  = df_top.nlargest(5, '등락률')[['종목명', '종가', '등락률']].values.tolist()
        result['top_fall']  = df_top.nsmallest(5, '등락률')[['종목명', '종가', '등락률']].values.tolist()
    except Exception as e:
        print(f'상위 종목 오류: {e}')
        result['top_rise'] = []
        result['top_fall'] = []

    # 외국인 순매수 상위
    try:
        df_inst = stock.get_market_net_purchases_of_equities_by_ticker(
            date_str, date_str, 'KOSPI', '외국인합계'
        )
        if not df_inst.empty:
            names_inst = {}
            for ticker in df_inst.index:
                try:
                    names_inst[ticker] = stock.get_market_ticker_name(ticker)
                except:
                    names_inst[ticker] = ticker
            df_inst['종목명'] = df_inst.index.map(lambda x: names_inst.get(x, x))
            result['foreign_buy']  = df_inst.nlargest(5, '순매수')[['종목명', '순매수']].values.tolist()
            result['foreign_sell'] = df_inst.nsmallest(5, '순매수')[['종목명', '순매수']].values.tolist()
        else:
            result['foreign_buy']  = []
            result['foreign_sell'] = []
    except Exception as e:
        print(f'외국인 순매수 오류: {e}')
        result['foreign_buy']  = []
        result['foreign_sell'] = []

    # 관심 섹터 종목 등락
    sector_result = {}
    for sector, tickers in SECTOR_STOCKS.items():
        sector_rows = []
        for ticker in tickers:
            try:
                df = stock.get_market_ohlcv_by_date(date_str, date_str, ticker)
                name = stock.get_market_ticker_name(ticker)
                if not df.empty:
                    row = df.iloc[-1]
                    chg = (row['종가'] - row['시가']) / row['시가'] * 100
                    sector_rows.append({'name': name, 'close': row['종가'], 'chg': chg})
            except Exception:
                pass
        sector_result[sector] = sector_rows
    result['sectors'] = sector_result

    return result


# ── 뉴스 수집 ──────────────────────────────────────────────────────────────────
def fetch_news(limit: int = 10):
    base = 'https://news.google.com/rss/search?hl=ko&gl=KR&ceid=KR:ko&q='
    queries = [
        '반도체+OR+SK하이닉스+OR+삼성전자',
        '이차전지+OR+배터리+OR+에코프로',
        '방산+OR+한화에어로스페이스',
        '코스피+OR+코스닥+OR+외국인순매수',
    ]
    items, cutoff = [], datetime.now(timezone.utc) - timedelta(hours=10)

    for q in queries:
        try:
            feed = feedparser.parse(base + q)
            for e in feed.entries[:4]:
                t   = getattr(e, 'published_parsed', None) or getattr(e, 'updated_parsed', None)
                pub = datetime.fromtimestamp(time.mktime(t), tz=timezone.utc) if t else None
                if pub and pub < cutoff:
                    continue
                title = e.get('title', '').strip()
                link  = e.get('link', '').strip()
                if title and link:
                    items.append({'title': title, 'link': link, 'pub': pub})
        except Exception as ex:
            print(f'뉴스 오류: {ex}')

    seen, unique = set(), []
    for item in items:
        key = item['title'][:20]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:limit]


# ── PDF 생성 ──────────────────────────────────────────────────────────────────
def build_pdf(krx: dict) -> bytes:
    font, font_bold = register_fonts()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=12*mm, bottomMargin=12*mm,
    )

    def S(name, **kw):
        base = dict(fontName=font, fontSize=10, leading=14, textColor=colors.black)
        base.update(kw)
        return ParagraphStyle(name, **base)

    s_title   = S('title',   fontName=font_bold, fontSize=22, leading=28,
                  textColor=colors.HexColor('#1a1a2e'), spaceAfter=4)
    s_section = S('section', fontName=font_bold, fontSize=13, leading=18,
                  textColor=colors.white, backColor=colors.HexColor('#0f3460'),
                  spaceBefore=8, spaceAfter=4, leftIndent=4, rightIndent=4)
    s_body    = S('body',    fontSize=9, leading=13, textColor=colors.HexColor('#333333'))
    s_news    = S('news',    fontSize=8.5, leading=13, textColor=colors.HexColor('#1a1a2e'))
    s_footer  = S('footer',  fontSize=8, leading=11, textColor=colors.grey, alignment=1)

    now_kst = datetime.now(KST)
    story   = []

    # ── 헤더 ──
    story.append(Paragraph('📈 Evening Signal Report', s_title))
    story.append(Paragraph(
        f'<font color="#666666">{now_kst.strftime("%Y년 %m월 %d일 (%a) — 오후 5시 KST 기준")}</font>',
        S('date', fontSize=9, leading=13, textColor=colors.HexColor('#666666'))
    ))
    story.append(HRFlowable(width='100%', thickness=2, color=colors.HexColor('#0f3460'), spaceAfter=8))

    # ── 지수 요약 ──
    story.append(Paragraph('🇰🇷  오늘의 국내 시장', s_section))
    idx_rows = [['시장', '종가', '등락률', '거래량']]
    for mkt in ('KOSPI', 'KOSDAQ'):
        d = krx.get(mkt)
        if d:
            chg_color = '#2ecc71' if d['chg'] >= 0 else '#e74c3c'
            arrow     = '▲' if d['chg'] >= 0 else '▼'
            idx_rows.append([
                mkt,
                f"{d['close']:,.2f}",
                f'<font color="{chg_color}">{arrow} {abs(d["chg"]):.2f}%</font>',
                f"{d['vol']:,}",
            ])
        else:
            idx_rows.append([mkt, '-', '-', '-'])

    col_w = [40*mm, 45*mm, 50*mm, 35*mm]
    t = Table([[Paragraph(str(c), S('tc', fontSize=9, leading=13,
                fontName=(font_bold if r == 0 else font),
                textColor=(colors.white if r == 0 else colors.black)))
                for c in row]
               for r, row in enumerate(idx_rows)],
              colWidths=col_w)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0f3460')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1),
         [colors.HexColor('#f0f4ff'), colors.HexColor('#ffffff')]),
        ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#cccccc')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1, 4*mm))

    # ── 상위 상승/하락 ──
    def movers_table(rows_data, header, color_header):
        rows = [[header, '종가', '등락률']]
        for row in rows_data:
            name, close, chg = row[0], row[1], row[2]
            chg_color = '#2ecc71' if chg >= 0 else '#e74c3c'
            arrow = '▲' if chg >= 0 else '▼'
            rows.append([
                name,
                f'{close:,}',
                f'<font color="{chg_color}">{arrow} {abs(chg):.2f}%</font>',
            ])
        col_w2 = [65*mm, 50*mm, 55*mm]
        t2 = Table([[Paragraph(str(c), S('tc2', fontSize=8.5, leading=13,
                    fontName=(font_bold if r == 0 else font),
                    textColor=(colors.white if r == 0 else colors.black)))
                     for c in row]
                    for r, row in enumerate(rows)],
                   colWidths=col_w2)
        t2.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), color_header),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1),
             [colors.HexColor('#f8f9fa'), colors.HexColor('#ffffff')]),
            ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#cccccc')),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ]))
        return t2

    if krx.get('top_rise'):
        story.append(Paragraph('🚀  상승 상위 5종목 (KOSPI)', s_section))
        story.append(movers_table(krx['top_rise'], '종목명', colors.HexColor('#1a6b3a')))
        story.append(Spacer(1, 3*mm))

    if krx.get('top_fall'):
        story.append(Paragraph('📉  하락 상위 5종목 (KOSPI)', s_section))
        story.append(movers_table(krx['top_fall'], '종목명', colors.HexColor('#8b1a1a')))
        story.append(Spacer(1, 3*mm))

    # ── 외국인 순매수 ──
    if krx.get('foreign_buy'):
        story.append(Paragraph('🌐  외국인 순매수/매도 상위', s_section))
        fb_rows = [['순매수 종목', '순매수(주)'], ['순매도 종목', '순매도(주)']]
        buy_data  = krx.get('foreign_buy',  [])
        sell_data = krx.get('foreign_sell', [])

        combined_rows = [['순매수 TOP5', '', '순매도 TOP5', '']]
        combined_rows.append(['종목', '순매수(주)', '종목', '순매도(주)'])
        for i in range(max(len(buy_data), len(sell_data))):
            b = buy_data[i]  if i < len(buy_data)  else ['-', 0]
            s = sell_data[i] if i < len(sell_data) else ['-', 0]
            b_color = '#2ecc71'
            s_color = '#e74c3c'
            combined_rows.append([
                b[0],
                f'<font color="{b_color}">+{b[1]:,}</font>',
                s[0],
                f'<font color="{s_color}">{s[1]:,}</font>',
            ])

        col_w3 = [52*mm, 30*mm, 52*mm, 30*mm]
        t3 = Table([[Paragraph(str(c), S('tc3', fontSize=8.5, leading=13,
                    fontName=(font_bold if r <= 1 else font),
                    textColor=(colors.white if r == 0 else colors.black)))
                     for c in row]
                    for r, row in enumerate(combined_rows)],
                   colWidths=col_w3)
        t3.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0f3460')),
            ('BACKGROUND', (0, 1), (-1, 1), colors.HexColor('#344e7c')),
            ('ROWBACKGROUNDS', (0, 2), (-1, -1),
             [colors.HexColor('#f8f9fa'), colors.HexColor('#ffffff')]),
            ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#cccccc')),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('SPAN', (0, 0), (1, 0)),
            ('SPAN', (2, 0), (3, 0)),
        ]))
        story.append(t3)
        story.append(Spacer(1, 3*mm))

    # ── 관심 섹터 ──
    story.append(Paragraph('🔍  관심 섹터 종목 현황', s_section))
    for sector, rows in krx.get('sectors', {}).items():
        if not rows:
            continue
        story.append(Paragraph(f'● {sector}', S('sec_title', fontName=font_bold,
                                                  fontSize=9.5, leading=13,
                                                  textColor=colors.HexColor('#0f3460'),
                                                  spaceBefore=4)))
        for r in rows:
            chg_color = '#2ecc71' if r['chg'] >= 0 else '#e74c3c'
            arrow = '▲' if r['chg'] >= 0 else '▼'
            story.append(Paragraph(
                f"  {r['name']}  {r['close']:,}원  "
                f'<font color="{chg_color}">{arrow} {abs(r["chg"]):.2f}%</font>',
                s_body
            ))
    story.append(Spacer(1, 3*mm))

    # ── 뉴스 ──
    story.append(Paragraph('📰  오늘의 주요 뉴스', s_section))
    news_items = fetch_news(10)
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
        f'자동 생성 · {now_kst.strftime("%Y-%m-%d %H:%M")} KST · Evening Signal Report',
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
    now_kst  = datetime.now(KST)
    date_str = now_kst.strftime('%Y%m%d')
    print(f'Evening Report 생성 중 — {now_kst.strftime("%Y-%m-%d %H:%M KST")}')

    krx = fetch_krx_data(date_str)

    pdf_bytes = build_pdf(krx)
    filename  = f'evening_report_{date_str}.pdf'
    caption   = (
        f'📈 <b>Evening Signal Report</b>\n'
        f'{now_kst.strftime("%Y년 %m월 %d일")} — 오후 5시 KST\n'
        f'KOSPI·KOSDAQ · 상위종목 · 외국인동향 · 관심섹터'
    )
    send_pdf(pdf_bytes, filename, caption)


if __name__ == '__main__':
    main()
