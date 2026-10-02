"""回答插图及 PDF：只展示原回答和引用资料，不生成市场数字。"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from html import escape
from functools import lru_cache
from io import BytesIO
import math
import os
from pathlib import Path
import re
from threading import Lock
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from frontend.financial_view import period_info, fact_unit, fact_numeric_value

FONT_PATHS = [os.getenv('WENCE_REPORT_FONT', ''), 'C:/Windows/Fonts/simsun.ttc',
              '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc']
METRICS = {'change': ('涨跌幅', '%'), 'nav_change': ('净值涨跌幅', '%'), 'fund_nav': ('单位净值', ''), 'close_price': ('收盘价', '元'),
           'pe_ttm': ('市盈率 TTM', '倍'), 'pb': ('市净率', '倍'), 'roe': ('净资产收益率', '%'),
           'fee_rate': ('管理费率', '%'), 'conversion_premium_rate': ('转股溢价率', '%'),
           'tracking_error': ('跟踪误差', '%'), 'yield_to_maturity': ('到期收益率', '%'),
           'revenue_growth': ('营收同比增长', '%'), 'net_profit_growth': ('净利润同比增长', '%')}
_FONT_LOCK = Lock()


def font_path() -> str | None:
    return next((path for path in FONT_PATHS if path and Path(path).is_file()), None)


@lru_cache(maxsize=16)
def font(size: int):
    path = font_path()
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)


def cited_facts(advice: dict) -> list[dict]:
    used = {str(value) for value in advice.get('evidence', [])}
    return [fact for fact in advice.get('facts', []) if str(fact.get('fact_id', '')) in used]


def chart_groups(advice: dict) -> list[dict]:
    """同指标同日期比较；缺日期、未来值或同日冲突不画。"""
    values, conflicts = {}, set()
    for fact in cited_facts(advice):
        field = fact.get('field')
        if field not in METRICS or isinstance(fact.get('value'), bool):
            continue
        info = period_info(fact.get('period'))
        value = fact_numeric_value(fact)
        if value is None:
            continue
        if not info or info[1] > date.today() or not math.isfinite(value):
            continue
        period, observed, period_kind = info
        identity = str(fact.get('entity_code') or fact.get('entity') or '')
        if not identity:
            continue
        # close_price can mean index points or currency; never combine incompatible units.
        unit = fact_unit(fact)
        key = (field, unit, identity, period)
        if key in values and not math.isclose(values[key]['value'], value, rel_tol=1e-8, abs_tol=1e-5):
            conflicts.add(key)
        values[key] = {'name': str(fact.get('entity') or identity), 'value': value, 'date': period}
    bars, lines = defaultdict(list), defaultdict(list)
    for (field, unit, identity, period), row in values.items():
        if (field, unit, identity, period) in conflicts:
            continue
        bars[(field, unit, period)].append(row)
        if field in {'close_price', 'fund_nav'} and period_info(period)[2] == 'date':
            lines[(field, unit, identity)].append(row)
    groups = []
    for (field, unit, period), rows in bars.items():
        if len(rows) >= 2:
            groups.append({'kind': 'bar', 'title': METRICS[field][0] + '对比', 'unit': unit,
                           'period': period, 'rows': rows[:8]})
    for (field, unit, identity), rows in lines.items():
        if len(rows) >= 2:
            ordered = sorted(rows, key=lambda row: row['date'])[-30:]
            groups.append({'kind': 'line', 'title': ordered[0]['name'] + ' · ' + METRICS[field][0] + '走势',
                           'unit': unit, 'period': ordered[0]['date'] + ' 至 ' + ordered[-1]['date'], 'rows': ordered})
    return groups[:3]


def _png(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def summary_image(advice: dict) -> bytes:
    image = Image.new('RGB', (1080, 240), '#f8fafc')
    draw = ImageDraw.Draw(image)
    status = advice.get('compliance', {}).get('status', 'REVIEW')
    label, color = {'PASS': ('可供参考 · 仍有投资风险', '#18794e'),
                    'BLOCK': ('暂不提供投资建议', '#ad384e')}.get(status, ('仍需核实 · 请先补充资料', '#a36a12'))
    draw.text((28, 20), '回答速览', font=font(28), fill='#172d3e')
    cards = [('引用资料', f'{len(cited_facts(advice))} 条', '#246b89'),
             ('分析维度', f'{len(advice.get("agent_results", []))} 项', '#246b89'),
             ('风险检查', label, color)]
    for index, (title, value, tint) in enumerate(cards):
        x = 28 + index * 347
        draw.rounded_rectangle((x, 72, x + 328, 184), radius=15, fill='white', outline='#dce6ed', width=2)
        draw.text((x + 18, 88), title, font=font(21), fill='#526b7c')
        draw.text((x + 18, 125), value, font=font(22), fill=tint)
    draw.text((28, 204), '图示用于梳理资料与风险，不代表收益预测或买卖指令。', font=font(18), fill='#526b7c')
    return _png(image)


def fit_label(draw: ImageDraw.ImageDraw, text: str, size: int, width: int) -> str:
    if draw.textlength(text, font=font(size)) <= width:
        return text
    while text and draw.textlength(text + '…', font=font(size)) > width:
        text = text[:-1]
    return text + '…'


def chart_image(group: dict) -> bytes:
    rows = group['rows']
    height = max(370, 155 + len(rows)*43) if group['kind'] == 'bar' else 420
    image = Image.new('RGB', (1080, height), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((30, 20), fit_label(draw, group['title'], 27, 1010), font=font(27), fill='#172d3e')
    draw.text((30, 58), '数据日期：' + group['period'] + (' · 单位：' + group['unit'] if group['unit'] else ''), font=font(19), fill='#526b7c')
    if group['kind'] == 'bar':
        low, high = min(0, min(row['value'] for row in rows)), max(0, max(row['value'] for row in rows))
        span = high-low or 1
        def x(value):
            return 290 + 625*(value-low)/span
        zero = x(0)
        draw.line((zero, 100, zero, 110+len(rows)*43), fill='#c7d6df', width=2)
        for index, row in enumerate(rows):
            y = 112+index*43
            draw.text((30, y), fit_label(draw, row['name'], 21, 240), font=font(21), fill='#172d3e')
            end = x(row['value'])
            draw.rectangle((min(zero,end), y+3, max(zero,end)+1, y+26), fill=('#ad384e' if row['value'] >= 0 else '#18794e') if '涨跌' in group['title'] else '#246b89')
            draw.text((935, y), f'{row["value"]:.4f}' if '净值' in group['title'] else f'{row["value"]:.2f}', font=font(20), fill='#172d3e')
    else:
        low, high = min(row['value'] for row in rows), max(row['value'] for row in rows)
        margin = (high-low)*.1 or max(abs(high)*.05, .1)
        low, high = low-margin, high+margin
        for tick in range(4):
            y = 112+tick*70
            value = high-(high-low)*tick/3
            draw.line((120,y,1025,y), fill='#e3ebf0')
            draw.text((25,y-10), f'{value:.2f}', font=font(18), fill='#526b7c')
        dates = [date.fromisoformat(row['date']).toordinal() for row in rows]
        points = [(120+(day-dates[0])/(dates[-1]-dates[0])*905,
                   112+(high-row['value'])/(high-low)*210) for day,row in zip(dates, rows)]
        draw.line(points, fill='#246b89', width=4)
        for x,y in points:
            draw.ellipse((x-4,y-4,x+4,y+4), fill='#246b89')
        draw.text((120,340), rows[0]['date'], font=font(18), fill='#526b7c')
        draw.text((895,340), rows[-1]['date'], font=font(18), fill='#526b7c')
    draw.text((30,height-32), '仅展示本回答已引用的数据；不同日期和口径分别呈现。', font=font(18), fill='#526b7c')
    return _png(image)


def answer_images(advice: dict) -> list[tuple[str, bytes]]:
    return [('资料与风险速览', summary_image(advice)),
            *[(group['title'], chart_image(group)) for group in chart_groups(advice)]]


def build_answer_pdf(report: dict[str, Any]) -> BytesIO:
    """独立于 Streamlit 的延迟导出；正文、图片、风险及来源完整保留。"""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Image as PDFImage, Paragraph, SimpleDocTemplate, Spacer

    with _FONT_LOCK:
        name = 'WenceReportChinese'
        if name not in pdfmetrics.getRegisteredFontNames():
            path = font_path()
            try:
                if not path:
                    raise ValueError('No TTF font')
                pdfmetrics.registerFont(TTFont(name, path, subfontIndex=0))
            except Exception:
                name = 'STSong-Light'
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(UnicodeCIDFont(name))
    body = ParagraphStyle('CNBody', fontName=name, fontSize=10, leading=17, wordWrap='CJK', spaceAfter=8, textColor=colors.HexColor('#172d3e'))
    heading = ParagraphStyle('CNHeading', parent=body, fontSize=14, leading=21, spaceBefore=14, spaceAfter=9)
    title = ParagraphStyle('CNTitle', parent=heading, fontSize=22, leading=28, alignment=TA_CENTER)
    small = ParagraphStyle('CNSmall', parent=body, fontSize=8, leading=12, textColor=colors.HexColor('#526b7c'))
    def paragraph(text, style=body):
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', str(text or ''))
        return Paragraph(escape(text).replace('\n', '<br/>'), style)
    story = [paragraph('问策智投 · 投资研究回答', title), paragraph('导出时间：' + datetime.now().strftime('%Y-%m-%d %H:%M'), small)]
    if report.get('question'):
        story += [paragraph('研究问题', heading), paragraph(report['question'])]
    for label, image in report.get('images', []):
        source = Image.open(BytesIO(image))
        story += [PDFImage(BytesIO(image), width=487, height=487*source.height/source.width), Spacer(1,8)]
    for label, texts in report['sections']:
        if texts:
            story.append(paragraph(label, heading))
            for text in texts:
                story.append(paragraph(text))
    if report.get('sources'):
        story.append(paragraph('引用资料与数据来源', heading))
        for index, row in enumerate(report['sources'], 1):
            text = f'{index}. {row["对象"]} / {row["指标"]}：{row["内容 / 数值"]}\n数据日期：{row.get("报告期", "未提供")}；获取时间：{row["数据时间（北京时间）"]}；来源：{row["来源"]}'
            story.append(paragraph(text, small))
            if row.get('原文'):
                story.append(paragraph('原文：' + row['原文'], small))
    story.append(paragraph('分析仅供参考，不保证收益。投资前，请结合自己的情况判断。', small))
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=54, leftMargin=54, topMargin=42, bottomMargin=46,
                                 title='问策智投投资研究回答', author='问策智投')
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(name, 8)
        canvas.setFillColor(colors.HexColor('#526b7c'))
        canvas.drawString(54, 24, '问策智投 · 投资研究 · 仅供参考')
        canvas.drawRightString(A4[0]-54, 24, f'第 {doc.page} 页')
        canvas.restoreState()
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    buffer.seek(0)
    return buffer
