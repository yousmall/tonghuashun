"""Bounded public evidence recovery, with no SkillHub credentials or orders."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import httpx

from backend.app.models import FactRecord
from backend.app.services.provider_errors import ProviderCallError

# Verified annual exchange publications, not hard-coded holiday dates. A new
# year without a registered publication fails closed instead of using weekdays.
ANNUAL_NOTICES = {
    2025: (
        'https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml',
        'https://www.szse.cn/disclosure/notice/t20241223_611283.html',
        'https://www.bse.cn/important_news/200024437.html',
    ),
    2026: (
        'https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml',
        'https://investor.szse.cn/disclosure/notice/general/t20251222_618087.html',
        'https://www.bse.cn/important_news/200027428.html',
    ),
}


def _failure(code, status=None):
    return ProviderCallError('公开数据补取未取得完整且明确的来源证据。', code=code, status_code=status)


def _fact(entity, field, value, *, now, url, source, period, unit=None, code=None,
          parents=(), rule=None, source_field=None):
    identity = json.dumps([source, url, entity, code, field, period, value], ensure_ascii=False, sort_keys=True)
    return FactRecord(fact_id='PUBLIC-' + hashlib.sha256(identity.encode()).hexdigest()[:24],
        entity=entity, entity_code=code, field=field, value=value, unit=unit,
        period=period, snapshot_time=now, source_url=url, source_id=source,
        source_field=source_field, quality=.9, derived_from=[f.fact_id for f in parents], derivation_rule=rule)


def parse_annual_notice(content: str, year: int) -> tuple[str, set[date]]:
    text = html.unescape(re.sub(r'<[^>]+>', ' ', re.sub(r'<(script|style)\b.*?</\1>', '', content, flags=re.S | re.I)))
    text = re.sub(r'\s+', '', text)
    if f'{year}年部分节假日休市安排' not in text:
        raise _failure('CALENDAR_NOTICE_INVALID')
    start = text.find('一、休市安排')
    end = text.find('二、', start)
    if start < 0 or end <= start:
        raise _failure('CALENDAR_NOTICE_INVALID')
    body = text[start:end]
    holidays = {'元旦', '春节', '清明节', '劳动节', '端午节', '中秋节', '国庆节'}
    segments = re.split(r'[（(][一二三四五六七八九十]+[）)]', body)[1:]
    covered, closed = set(), set()
    for segment in segments:
        label = segment.split('：', 1)[0].split(':', 1)[0]
        names = {name for name in holidays if name in label}
        if not names or covered & names:
            raise _failure('CALENDAR_NOTICE_INVALID')
        covered |= names
        match = re.search(r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日[（(]星期[^）)]+[）)]'
                          r'(?:至(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日[（(]星期[^）)]+[）)])?休市', segment)
        if not match:
            raise _failure('CALENDAR_NOTICE_INVALID')
        y1, m1, d1, y2, m2, d2 = match.groups()
        try:
            first = date(int(y1 or year), int(m1), int(d1))
            last = date(int(y2 or year), int(m2 or m1), int(d2 or d1))
        except ValueError:
            raise _failure('CALENDAR_NOTICE_INVALID') from None
        if first.year != year or last.year != year or not 0 <= (last-first).days <= 16:
            raise _failure('CALENDAR_NOTICE_INVALID')
        closed.update(first + timedelta(days=i) for i in range((last-first).days + 1))
    if covered != holidays or not 15 <= len(closed) <= 45:
        raise _failure('CALENDAR_NOTICE_INVALID')
    return body, closed


def calendar_from_notices(notices, *, start, end, now):
    start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
    if not 350 <= (end_day-start_day).days <= 366 or end_day >= now.astimezone(timezone(timedelta(hours=8))).date():
        raise ValueError('日历补取仅支持已经结束的完整一年窗口')
    years = set(range(start_day.year, end_day.year + 1))
    if set(notices) != years or any(len(notices[year]) != 3 for year in years):
        raise _failure('CALENDAR_COVERAGE_INCOMPLETE')
    if any({url for url, content in notices[year]} != set(ANNUAL_NOTICES.get(year, ())) for year in years):
        raise _failure('CALENDAR_COVERAGE_INCOMPLETE')
    roots, all_closed = [], set()
    for year in sorted(years):
        parsed = [parse_annual_notice(content, year) for url, content in notices[year]]
        # SSE, SZSE and BSE must independently declare the same closures.
        if any(closed != parsed[0][1] for body, closed in parsed[1:]):
            raise _failure('CALENDAR_EXCHANGE_CONFLICT')
        all_closed |= parsed[0][1]
        roots.extend(_fact('中国A股交易日历', 'exchange_calendar_notice', body, now=now,
            url=url, source='EXCHANGE_ANNUAL_NOTICE', period=f'{year}:{urlsplit(url).hostname}', source_field='年度休市安排')
            for (url, content), (body, closed) in zip(notices[year], parsed))
    sessions = [start_day + timedelta(days=i) for i in range((end_day-start_day).days + 1)
                if (start_day + timedelta(days=i)).weekday() < 5 and start_day + timedelta(days=i) not in all_closed]
    rule = 'EXCHANGE_NOTICE_CALENDAR_V1: Mon-Fri minus identical SSE/SZSE/BSE annual closures; planned sessions'
    counts = _fact('中国A股交易日历', 'market_session_count', len(sessions), now=now,
        url=roots[0].source_url, source='DERIVED_EXCHANGE_CALENDAR_V1', period=f'{start}/{end}', parents=roots, rule=rule)
    days = [_fact('中国A股交易日历', 'market_session', 1, now=now, url=roots[0].source_url,
        source='DERIVED_EXCHANGE_CALENDAR_V1', period=day.isoformat(), parents=roots, rule=rule) for day in sessions]
    return [*roots, counts, *days]


def parse_adjusted_history(payload, *, symbol, start, end, now, url):
    # Only an explicit qfqday array can prove adjustment. Plain day is rejected,
    # even if the request contained qfq or the security has no recent dividends.
    code = symbol[2:]
    if not isinstance(payload, dict) or isinstance(payload.get('code'), bool) or payload.get('code') != 0:
        return []
    records = payload.get('data')
    if not isinstance(records, dict) or not isinstance(records.get(symbol), dict):
        return []
    data = records[symbol]
    rows = data.get('qfqday')
    quote_data = data.get('qt')
    quote = quote_data.get(symbol, []) if isinstance(quote_data, dict) else []
    if not isinstance(rows, list) or not isinstance(quote, list) or len(quote) < 3 or str(quote[2]) != code:
        return []
    values, conflicts = {}, set()
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            return []
        try:
            day = date.fromisoformat(str(row[0]))
            if day.weekday() >= 5 or any(isinstance(item, bool) for item in row[1:5]):
                return []
            prices = [float(item) for item in row[1:5]]
        except (ValueError, TypeError):
            return []
        if not all(math.isfinite(value) and value > 0 for value in prices):
            return []
        opening, close, high, low = prices
        if not low <= min(opening, close) <= max(opening, close) <= high:
            return []
        if not start <= day.isoformat() <= end or day >= now.astimezone(timezone(timedelta(hours=8))).date():
            continue
        if day in values and values[day] != close:
            conflicts.add(day)
        values[day] = close
    return [_fact(code, 'adjusted_close_history', close, now=now, url=url,
        source='TENCENT_PUBLIC_QFQ', period=day.isoformat(), unit='CNY:qfq', code=code,
        source_field=f'{symbol}.qfqday[date,open,close,high,low,volume].close')
        for day, close in sorted(values.items()) if day not in conflicts]


def parse_risk_inventory(pages, *, now, url):
    """A complete classified board can prove membership; names cannot."""
    symbols, totals, updated = set(), set(), []
    for payload in pages:
        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, dict) or isinstance(payload.get('rc'), bool) or payload.get('rc') != 0:
            raise _failure('RISK_INVENTORY_INVALID')
        total, rows = data.get('total'), data.get('diff')
        if (isinstance(total, bool) or not isinstance(total, int) or not 1 <= total <= 1000
                or not isinstance(rows, list) or not rows):
            raise _failure('RISK_INVENTORY_INCOMPLETE')
        totals.add(total)
        for row in rows:
            if not isinstance(row, dict):
                raise _failure('RISK_INVENTORY_INVALID')
            code, market = row.get('f12'), row.get('f13')
            if (not isinstance(code, str) or not re.fullmatch(r'(?:60|68|00|30)\d{4}', code)
                    or isinstance(market, bool) or market != (1 if code.startswith(('60', '68')) else 0)
                    or code in symbols):
                raise _failure('RISK_INVENTORY_INCOMPLETE')
            symbols.add(code)
            try:
                stamp = float(row['f124'])
                if isinstance(row['f124'], bool) or not math.isfinite(stamp) or not stamp.is_integer() or stamp < 0:
                    raise ValueError
                if stamp:
                    observed = datetime.fromtimestamp(stamp, timezone.utc)
                    if observed > now + timedelta(minutes=5):
                        raise ValueError
                    updated.append(observed)
            except (KeyError, ValueError, TypeError, OverflowError, OSError):
                raise _failure('RISK_INVENTORY_INVALID') from None
    if totals != {len(symbols)} or not updated or now-max(updated) > timedelta(days=16):
        raise _failure('RISK_INVENTORY_INCOMPLETE')
    return _fact('沪深风险警示板', 'risk_warning_inventory',
        {'scope': ['SH', 'SZ'], 'filter': 'm:0 f:4,m:1 f:4', 'declared_count': len(symbols),
         'symbols': sorted(symbols), 'latest_quote_time': max(updated).isoformat()}, now=now,
        url=url, source='EASTMONEY_PUBLIC_RISK_BOARD', period=now.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
        source_field='classified risk-warning board; all pages match declared total')


def parse_bse_adjusted_history(payload, *, code, start, end, now, url):
    """Fixed Eastmoney daily/qfq contract, plus returned BSE instrument identity.

    Unlike a natural-language query, fqt is an API enum documented in the
    upstream SDK. Its contract is retained as a parent, never inferred from
    price similarity. An old NEEQ code is never guessed for a new BSE code.
    """
    if not re.fullmatch(r'(?:92\d{4}|[48]\d{5})', code):
        return []
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return []
    query = dict(parse_qsl(parsed.query))
    if (parsed.scheme != 'https' or parsed.username or parsed.password or port not in {None,443}
            or parsed.hostname != 'push2his.eastmoney.com'
            or parsed.path != '/api/qt/stock/kline/get'
            or query.get('secid') != '0.' + code or query.get('fqt') != '1' or query.get('klt') != '101'
            or query.get('beg') != start.replace('-', '') or query.get('end') != end.replace('-', '')):
        return []
    data = payload.get('data') if isinstance(payload, dict) else None
    if (not isinstance(data, dict) or isinstance(payload.get('rc'), bool) or payload.get('rc') != 0
            or data.get('code') != code or data.get('market') != 0 or isinstance(data.get('market'), bool)
            or not isinstance(data.get('name'), str) or not data['name'].strip()
            or not isinstance(data.get('klines'), list) or not data['klines']):
        return []
    contract = _fact(code, 'price_adjustment_contract', {'provider':'Eastmoney', 'fqt':'1', 'klt':'101',
        'security': '0.' + code, 'adjustment':'qfq', 'currency':'CNY', 'amount_unit':'CNY',
        'reference':'https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_hist_em.py'},
        now=now, url=url, source='EASTMONEY_QFQ_CONTRACT_V1', period=f'{start}/{end}', code=code)
    result, seen = [], set()
    for row in data['klines']:
        if not isinstance(row, str):
            return []
        values = row.split(',')
        if len(values) != 11:
            return []
        try:
            day = date.fromisoformat(values[0])
            opening, closing, high, low = map(float, values[1:5])
            amount = float(values[6])
        except (ValueError, TypeError):
            return []
        if (day in seen or day.weekday() >= 5 or not start <= day.isoformat() <= end
                or day >= now.astimezone(timezone(timedelta(hours=8))).date()
                or not all(math.isfinite(x) and x > 0 for x in (opening,closing,high,low))
                or not low <= min(opening,closing) <= max(opening,closing) <= high
                or not math.isfinite(amount) or amount < 0):
            return []
        seen.add(day)
        for field, value, unit in [('adjusted_close_history',closing,'CNY:qfq'), ('daily_turnover_history',amount,'CNY')]:
            result.append(_fact(code, field, value, now=now, url=url, source='EASTMONEY_PUBLIC_BSE_QFQ_V1',
                period=day.isoformat(), unit=unit, code=code, parents=[contract],
                source_field='klines[date,open,close,high,low,volume,amount,...]',
                rule='EASTMONEY_DAILY_QFQ_V1: fixed klt=101/fqt=1 contract; exact returned security'))
    return [contract, *result]


class PublicMarketRecovery:
    def __init__(self, *, transport=None, now=None):
        self.now = now or (lambda: datetime.now(timezone.utc))
        # A separate client never inherits the SkillHub bearer token.
        self.client = httpx.AsyncClient(timeout=12, transport=transport,
            headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json,text/html'}, follow_redirects=False)
        self.calendar_lock = asyncio.Lock()
        self.calendar_cache = None
        self.risk_lock = asyncio.Lock()
        self.risk_cache = None
        self.risk_failure = None

    async def aclose(self):
        await self.client.aclose()

    async def _get(self, url):
        try:
            current = url
            for attempt in range(3):
                async with self.client.stream('GET', current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = urljoin(current, response.headers.get('location', ''))
                        try:
                            parsed = urlsplit(target)
                            port = parsed.port
                        except ValueError:
                            raise _failure('PUBLIC_SOURCE_REDIRECT_REJECTED') from None
                        if (parsed.scheme != 'https' or parsed.hostname != urlsplit(url).hostname
                                or parsed.username or parsed.password or port not in {None, 443}):
                            raise _failure('PUBLIC_SOURCE_REDIRECT_REJECTED')
                        if attempt == 2:
                            raise _failure('PUBLIC_SOURCE_REDIRECT_LIMIT')
                        current = target
                        continue
                    if response.status_code != 200:
                        raise _failure('PUBLIC_SOURCE_HTTP_ERROR', response.status_code)
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 2_000_000:
                            raise _failure('PUBLIC_SOURCE_TOO_LARGE')
                        chunks.append(chunk)
                    return b''.join(chunks).decode('utf-8')
        except (httpx.RequestError, UnicodeError):
            raise _failure('PUBLIC_SOURCE_UNAVAILABLE') from None

    async def get_adjusted_stock_history(self, code, start, end):
        if not re.fullmatch(r'(?:60|68|00|30)\d{4}|(?:92\d{4}|[48]\d{5})', code):
            return []
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if not 350 <= (last-first).days <= 366 or last >= self.now().astimezone(timezone(timedelta(hours=8))).date():
            raise ValueError('历史补取仅支持已经结束的完整一年窗口')
        if code.startswith(('92','4','8')):
            url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get?' + urlencode({
                'secid':'0.'+code, 'klt':'101', 'fqt':'1', 'beg':start.replace('-',''), 'end':end.replace('-',''),
                'fields1':'f1,f2,f3,f4,f5,f6', 'fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
                'ut':'7eea3edcaed734bea9cbfc24409ed989'})
            try:
                payload = json.loads(await self._get(url))
            except ValueError:
                raise _failure('PUBLIC_SOURCE_INVALID') from None
            return parse_bse_adjusted_history(payload, code=code, start=start, end=end, now=self.now(), url=url)
        symbol = ('sh' if code.startswith(('60', '68')) else 'sz') + code
        url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,{start},{end},640,qfq'
        try:
            payload = json.loads(await self._get(url))
        except ValueError:
            raise _failure('PUBLIC_SOURCE_INVALID') from None
        return parse_adjusted_history(payload, symbol=symbol, start=start, end=end, now=self.now(), url=url)

    async def get_exchange_calendar(self, start, end):
        async with self.calendar_lock:
            now = self.now()
            if self.calendar_cache:
                key, fetched, facts = self.calendar_cache
                if key == (start, end) and timedelta(0) <= now-fetched < timedelta(seconds=30):
                    return facts
            years = range(date.fromisoformat(start).year, date.fromisoformat(end).year + 1)
            if any(year not in ANNUAL_NOTICES for year in years):
                raise _failure('CALENDAR_YEAR_UNSUPPORTED')
            tasks = [(year, url) for year in years for url in ANNUAL_NOTICES[year]]
            texts = await asyncio.gather(*(self._get(url) for year, url in tasks), return_exceptions=True)
            failure = next((result for result in texts if isinstance(result, BaseException)), None)
            if failure:
                raise failure
            notices = {year: [(url, content) for (y, url), content in zip(tasks, texts) if y == year] for year in years}
            facts = calendar_from_notices(notices, start=start, end=end, now=now)
            self.calendar_cache = ((start, end), now, facts)
            return facts

    async def get_stock_risk_state(self, code):
        if not re.fullmatch(r'(?:60|68|00|30)\d{4}', code):
            return []  # This board explicitly covers SH/SZ; it cannot clear BSE.
        async with self.risk_lock:
            now = self.now()
            if self.risk_failure and timedelta(0) <= now-self.risk_failure[0] < timedelta(seconds=30):
                raise self.risk_failure[1]
            cached = self.risk_cache
            if cached and timedelta(0) <= now-cached.snapshot_time < timedelta(seconds=30):
                inventory = cached
            else:
                def page_url(page):
                    return 'https://push2.eastmoney.com/api/qt/clist/get?' + urlencode({
                        'pn': page, 'pz': 100, 'po': 1, 'np': 1, 'fltt': 2, 'invt': 2, 'fid': 'f12',
                        'fs': 'm:0 f:4,m:1 f:4', 'fields': 'f12,f13,f124',
                        # Published website application identifier, never a user credential.
                        'ut': 'bd1d9ddb04089700cf9c27f6f7426281'})
                try:
                    first = json.loads(await self._get(page_url(1)))
                    data = first.get('data') if isinstance(first, dict) else None
                    total = data.get('total') if isinstance(data, dict) else None
                    if isinstance(total, bool) or not isinstance(total, int) or not 1 <= total <= 1000:
                        raise _failure('RISK_INVENTORY_INCOMPLETE')
                    pages = [first]
                    for page in range(2, math.ceil(total/100) + 1):
                        pages.append(json.loads(await self._get(page_url(page))))
                    inventory = parse_risk_inventory(pages, now=self.now(), url=page_url(1))
                    self.risk_cache, self.risk_failure = inventory, None
                except ValueError:
                    error = _failure('RISK_INVENTORY_INVALID')
                    self.risk_failure = (now, error)
                    raise error from None
                except ProviderCallError as error:
                    self.risk_failure = (now, error)
                    raise
            state = _fact(code, 'is_st', code in inventory.value['symbols'], now=inventory.snapshot_time,
                url=inventory.source_url, source='DERIVED_PUBLIC_RISK_STATE_V1', period=inventory.period,
                code=code, parents=[inventory], rule='COMPLETE_RISK_BOARD_MEMBERSHIP_V1: symbol in complete SH/SZ classified board')
            return [inventory, state]
