"""Evidence-backed publisher identities; unknown domains stay unverified."""
import re
from urllib.parse import urlsplit
from backend.services.procurement_rules import brand_identity, identity, supported
from backend.services.research_sources import parse_web_url
from backend.services.source_authority import source_publisher_tier


def verify_publisher(candidate, sources, brand):
    """Accept explicit manufacturer-domain links from independent institutions.

    Self-asserted 'official' is deliberately insufficient. The linking text
    must contain the brand, hostname and an explicit official-site relation.
    """
    # 候选地址来自检索结果：解析不了的直接"无法核验"，而不是把 ``ValueError``
    # 抛到品牌核验那一步（那一步在价格车道上，失败半径会变成整条车道）。
    candidate_url = parse_web_url(candidate.get('url'))
    if candidate_url is None:
        return None
    host = (urlsplit(candidate_url).hostname or '').lower().removeprefix('www.')
    if not host or not brand:
        return None
    for source in sources:
        relation = source.get('publisher_evidence') or {}
        # 两侧都是**品牌写法**，所以走 ``brand_identity``（查别名表）而不是 ``identity``：
        # 这一处的用途是"这条来源的发布者证据记的品牌，与当前要验证的品牌是不是同一个"。
        # 用 ``identity`` 时 ``天津泰斯特`` 与 ``泰斯特``、``徕卡`` 与 ``Leica`` 会判成两家，
        # 于是已登记在 ``_BRAND_ALIASES`` 里的别名在这一处全部失效（实测 2026-09-27）。
        # 注意：下面那三处**子串包含**判断刻意保持 ``identity``，理由见那里的说明。
        if source.get('body_status') == 'fetched' and source.get('source_type') in {'manufacturer_website', 'manufacturer_download'} and brand_identity(relation.get('brand')) == brand_identity(brand):
            # A verified publisher can link its own downloads, including a CDN.
            for match in re.finditer(r'https?://[^\s<>\[\]"\']+', source.get('body_text', '')):
                source_url = parse_web_url(source.get('url'))
                same_host = bool(source_url) and (urlsplit(source_url).hostname or '').lower().removeprefix('www.') == host
                download = urlsplit(candidate_url).path.lower().endswith(('.pdf', '.docx', '.xlsx', '.doc', '.xls'))
                if match.group() == candidate.get('url') and (same_host or download):
                    return {'brand': brand, 'domain': host, 'url': source['url'],
                            'excerpt': match.group(), 'parent_evidence': relation, 'scope': 'exact_url', 'target_url': candidate['url']}
        # The vouching page must be an institutional publication; its TLD is
        # not the fact. ``source_authority`` owns that decision.
        if source_publisher_tier(source) is None:
            continue
        body = source.get('body_text', '')
        # Match a complete hostname, never example.com.evil.invalid or a
        # hostname appearing inside a tracking URL's query string.
        for match in re.finditer(r'https?://[^\s<>\[\]"\'，。；）)]+|(?<![\w/@.])www\.[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.])', body, re.I):
            # 正文里抄出来的地址同样可能带着全角冒号一类的尾巴：解析不了就跳过这一条
            # 匹配，而不是把 ``ValueError`` 抛到品牌核验那一步去（见 ``parse_web_url``）。
            linked_url = match.group() if '://' in match.group() else 'https://' + match.group()
            linked_url = parse_web_url(linked_url)
            if linked_url is None:
                continue
            linked_host = (urlsplit(linked_url).hostname or '').lower().removeprefix('www.')
            if linked_host != host:
                continue
            # Do not join brands from neighboring paragraphs or table rows.
            start = max(body.rfind('\n', 0, match.start()) + 1, match.start() - 180)
            end = body.find('\n', match.end())
            excerpt = body[start:min(end if end >= 0 else len(body), match.end() + 180)]
            # 子串包含判断**刻意留在 `identity`**（2026-09-27 量测结论）：这里的用途是
            # "这段引文里写没写这个品牌"，换成 `brand_identity` 会让品牌名在归一后**变短
            # 或换语言**，方向不定——`湖南湘仪`→`湘仪` 更松（会把别家字号也算进来），
            # `申安`→`上海申安`、`徕卡`→`leica` 更紧（中文引文里反而认不出）。
            # 两个权威库上的实测：判据层增量 0（分母 5832 个三元组，改前改后都没有一条
            # 认得出来），而同类子串判断（`product_configuration._own_brand_page`）
            # 放行过「长沙高新开发区湘仪天平仪器设备有限公司」——那是另一家做天平的。
            if identity(brand) in identity(excerpt) and not re.search(r'代理商|经销商', excerpt) and re.search(r'官网|官方网站|官方网址|official website|(?:制造商|生产厂家|厂商)\s*(?:网站|网址|链接)', excerpt, re.I):
                return {'brand': brand, 'domain': host, 'url': source['url'], 'excerpt': excerpt}
            named_link = re.search(r'([\u4e00-\u9fffA-Za-z]{2,}(?:有限公司|研究所))\s*\[$', body[start:match.start()])
            if named_link and not re.search(r'代理|经销|供应商', excerpt):
                company = named_link[1]
                # 子串包含，同上：刻意保持 ``identity``。
                if identity(brand) in identity(company) and supported(company, candidate.get('body_text')):
                    return {'brand': brand, 'domain': host, 'url': source['url'], 'excerpt': excerpt,
                            'method': 'institution_named_company_link', 'company': company}
            # Structured institutional directories often put field labels and
            # values on separate lines. Require one named legal entity and an
            # explicit company-website field, plus matching publisher text.
            lines = body[:match.start()].splitlines()
            context = '\n'.join(lines[-5:]) + match.group()
            if any(word in context for word in ('代理商', '经销商')):
                continue
            companies = set(re.findall(r'[\u4e00-\u9fffA-Za-z]{2,}(?:有限公司|研究所)', context))
            website = re.search(r'(?:企业|公司|厂商|制造商|官方)\s*(?:网站|网址)|官\s*网', context)
            if website and len(companies) == 1:
                company = next(iter(companies)).removeprefix('企业名称').removeprefix('公司名称')
                # 子串包含，同上：刻意保持 ``identity``。
                if identity(brand) in identity(company) and supported(company, candidate.get('body_text')):
                    return {'brand': brand, 'domain': host, 'url': source['url'], 'excerpt': context,
                            'method': 'institution_company_website_fields', 'company': company}
    return None
