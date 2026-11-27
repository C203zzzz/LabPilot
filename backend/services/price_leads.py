"""Keep institution asset values visible without promoting them to trade prices."""
import re

from backend.services.procurement_rules import contains_model
from backend.services.source_authority import source_publisher_tier


_MONEY_RE = re.compile(
    r"(?:[￥¥]\s*(?P<symbol>[\d,]+(?:\.\d+)?)|(?P<number>[\d,]+(?:\.\d+)?)\s*(?P<unit>万元|千元|元))",
    re.IGNORECASE,
)


def _unparsed_amount_leads(sources, target, existing):
    """Keep literal model+amount text visible when no table schema is usable."""
    results = []
    model = target.get('model')
    for source in sources:
        if source.get('body_status') != 'fetched' or not isinstance(source.get('body_text'), str):
            continue
        lines = [line.strip() for line in source['body_text'].splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if not contains_model(line, model):
                continue
            context = line
            match = _MONEY_RE.search(context)
            if not match and index + 1 < len(lines):
                candidate = f"{line}\n{lines[index + 1]}"
                match = _MONEY_RE.search(candidate)
                if match:
                    context = candidate
            if not match:
                continue
            amount_text = match.group('symbol') or match.group('number')
            amount = float(amount_text.replace(',', ''))
            if amount <= 0:
                continue
            unit = match.group('unit') or '元'
            key = (source.get('url'), model, amount, unit)
            if key in existing:
                continue
            results.append({
                'model': model,
                'kind': 'unparsed_amount',
                'amount': amount,
                'amount_unit': unit,
                'currency': 'CNY',
                'url': source['url'],
                'excerpt': context[:1600],
                'note': '正文包含型号和金额，但数量、交易状态或金额口径未完成结构化；仅供人工核对，不能作为成交价或当前报价',
                'evidence_location': {
                    'char_start': source['body_text'].find(line),
                    'snapshot': source.get('snapshot'),
                },
            })
            existing.add(key)
    return results


def asset_price_leads(sources, target):
    results = []
    for source in sources:
        # Institutional asset registers live on hospital and bidding-agency
        # hosts too, so ask the shared publisher test instead of a TLD suffix.
        if source_publisher_tier(source) is None:
            continue
        body = source.get('body_text') or ''
        # An asset number defines the record boundary. Never search a sliding
        # window that could borrow the preceding instrument's amount.
        starts = list(re.finditer(r'资产编号\s*[:：]\s*([^\s]+)', body))
        for index, start in enumerate(starts):
            end = starts[index+1].start() if index+1 < len(starts) else len(body)
            block = body[start.start():end]
            model_fields = list(re.finditer(r'型号\s*[:：]\s*([^\n]+)', block))
            if len(model_fields) != 1 or not contains_model(model_fields[0][1], target['model']):
                continue
            prices = list(re.finditer(r'(?<!总)单价\s*[:：]\s*([\d,]+(?:\.\d+)?)\s*(万元|千元|元)?', block))
            if len(prices) != 1:
                continue
            price = prices[0]
            value = float(price[1].replace(',', ''))
            if value <= 0:
                continue
            unit = price[2]
            excerpt_end = max(model_fields[0].end(), price.end())
            excerpt = block[:excerpt_end].strip()
            results.append({'model': target['model'], 'kind': 'asset_register',
                            'amount': value, 'amount_unit': unit, 'currency': 'CNY' if unit else None,
                            'url': source['url'], 'excerpt': excerpt,
                            'note': '资产登记单价；币种未标明；不能作为成交价或当前报价' if not unit else '资产登记单价；不能作为成交价或当前报价',
                            'evidence_location': {'char_start': start.start(), 'snapshot': source.get('snapshot')}})
    return list({(r['url'], r['excerpt']): r for r in results}.values())


def price_leads(sources, target):
    """Expose literal amounts as leads; these never enter validated prices."""
    from backend.services.procurement_tables import looks_like_table_header

    results = asset_price_leads(sources, target)
    existing = {(r.get('url'), r.get('model'), r.get('amount'), r.get('amount_unit')) for r in results}
    for source in sources:
        if source.get('body_status') != 'fetched':
            continue
        header = None
        body_text = source.get('body_text') or ''
        has_currency_context = bool(re.search(r'(?:金额单位\s*[:：]?\s*元|元\s*[（(]人民币[）)])', body_text))
        for line in body_text.splitlines():
            if '|' not in line:
                if line.strip():
                    header = None
                continue
            cells = [c.strip() for c in line.split('|')]
            if looks_like_table_header(cells, has_currency_context):
                # Reset fallback-lead parsing for every new table, including
                # tables whose amount column is ``总价``/``合计``. Otherwise a
                # data row from that table can inherit the prior table's
                # 单价 column and leak a false amount for the target model.
                header = cells if '型号' in cells and any(re.search(r'金额|单价|价格', c) for c in cells) else None
                continue
            if '型号' in cells:
                header = cells if any(re.search(r'金额|单价|价格', c) for c in cells) else None
                continue
            if not contains_model(line, target['model']):
                continue
            kind, note, value, unit = 'listing', '网页挂牌金额；发布方、配置及报价有效期待核实，不能作为成交价', None, '元'
            amount_column = next((i for i, c in enumerate(header or []) if re.fullmatch(r'(?:金额|单价|价格)[（(]元[）)]', c)), None)
            if header and len(header) == len(cells) and amount_column is not None:
                if not contains_model(cells[header.index('型号')], target['model']):
                    continue
                raw = cells[amount_column].replace(',', '')
                if re.fullmatch(r'\d+(?:\.\d+)?', raw):
                    value = float(raw)
                    kind, note = 'procurement_amount', '采购公示金额；数量及金额口径待核实，不能作为设备成交单价'
                    if '排名' in header:
                        rank = cells[header.index('排名')]
                        note = f'投标公示金额，排名{rank}；非已确认成交价，数量及发布方身份待核实'
                        kind = 'bid_amount'
            else:
                # A currency-prefixed cell in a model-specific catalogue row.
                money = [re.fullmatch(r'[￥¥]\s*([\d,]+(?:\.\d+)?)', c) for c in cells]
                money = [m for m in money if m]
                if len(money) == 1:
                    value = float(money[0][1].replace(',', ''))
            if value and value > 0:
                excerpt = '\n'.join([' | '.join(header), line]) if header and kind != 'listing' else line
                results.append({'model':target['model'], 'kind':kind, 'amount':value, 'amount_unit':unit,
                                'currency':'CNY', 'url':source['url'], 'excerpt':excerpt, 'note':note,
                                'evidence_location':{'char_start':(source.get('body_text') or '').find(line), 'snapshot':source.get('snapshot')}})
                existing.add((source.get('url'), target.get('model'), value, unit))
    results.extend(_unparsed_amount_leads(sources, target, existing))
    return list({(r['url'], r['model'], r['amount'], r['kind']):r for r in results}.values())
