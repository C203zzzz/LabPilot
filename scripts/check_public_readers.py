"""Live URL diagnostics only; never inserts results into procurement tasks."""
import json
import time
from pathlib import Path
from backend.infrastructure.page_reader import PublicPageReader
from backend.services.research_sources import attachments

OUT = Path('output/four-module-implementation-20260920/web-comparison')
URLS = [
    'https://ecaigou.swu.edu.cn:30910/bid/detail?id=10559',
    'http://www.taisite.cn/index.php/prod/70.html',
    'https://www.gdipu.edu.cn/info/1078/28523.htm',
    'http://cg.qdu.edu.cn/jjjg/9315.chtml',
    'https://ggzyfw.beijing.gov.cn/jyxxzbjggg/20231206/4273128.html',
    'https://m.nbchao.com/cp/A8D0D32643FD8B94.html',
    'https://www.labx.com/item/binder-kb-23-benchtop-compact-20-liter-microbiological/DIS-59250-70359',
    'https://www.memmert.com/products/incubators/incubator',
]

if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    reader = PublicPageReader(browser=True)
    rows = []
    for index, url in enumerate(URLS, 1):
        for method in ('http', 'browser'):
            start = time.monotonic()
            row = {'url': url, 'method': method, 'cached': False, 'scope': 'fixed_url_diagnostic'}
            try:
                result = getattr(reader, 'read_' + method)(url, 20)
                row.update(status='read', metadata=result.metadata, chars=len(result.text),
                           table_projection='[表格按列展开' in result.text,
                           attachments=attachments({'url': url, 'body_text': result.text}),
                           facts={word: word in result.text for word in ['SPX-150B', '6,800', '6800', '150', '6930']})
                (OUT/f'{index:02}-{method}.txt').write_text(result.text, encoding='utf-8')
                (OUT/f'{index:02}-{method}.bin').write_bytes(result.payload)
            except Exception as exc:
                row.update(status='failed', failure=getattr(exc, 'kind', type(exc).__name__),
                           status_code=getattr(exc, 'status', getattr(exc, 'code', None)), error=str(exc)[:350])
            row['seconds'] = round(time.monotonic()-start, 2)
            rows.append(row)
            (OUT/'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
            print(index, method, row['status'], row.get('failure', row.get('chars')), flush=True)
