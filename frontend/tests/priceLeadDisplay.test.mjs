/**
 * 商业线索的展示契约：**条件渲染，判据只有一个**。
 *
 * 用户的定位（原文）：「这个产品是面向历史招标公告的比价工具」。规则因此按任务分
 * 两种，判据只有一个——这一轮有没有可采信的官方成交记录（`transactionPrices` 非空，
 * 也就是页面主区那张成交价卡片）：
 *
 * * 有官方成交记录 → 聚合站与电商页面的金额一个都不出现在前端：没有区块、没有域名、
 *   也没有「聚合站」「转载」「镜像」这类字样或条数说明。
 * * 一条都没有 → 它们**正常列出**（不是折叠隐藏、也不是只给个数量），并在块头写明
 *   它不是原始公告、不能当成交价。那时它是这台机器唯一的交易信号，藏起来用户会
 *   以为市场上没有任何记录。
 *
 * 这一份是**渲染契约测试**，和 tests/researchResults.test.mjs 同一套路：读源文件断言
 * 关键片段在不在。原因是 `PriceLead` / `MarketReferenceSection` 是 JSX，而这个项目
 * 没有 DOM 测试环境（package.json 里没有 jsdom / testing-library）。真正跑在浏览器里的
 * 验证见 docs/待归档-前端只留权威来源.md（真实后端 + 真实浏览器，DOM 计数，两个任务
 * 各测一遍：有官方记录的 task-<run-6> 与没有官方记录的 task-<run-5>）。
 *
 * 数据仍然全在后端：`run.price_leads`、`run.sources`、`run.republished_prices` 一个都
 * 没动，这里是**前端渲染**的契约。
 */
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

import {
  formatPriceAmount,
  MARKET_REFERENCE_EXPLANATION,
  MARKET_REFERENCE_FIELD_LEGEND,
  MARKET_REFERENCE_TIERS,
  MARKET_REFERENCE_WARNING,
  marketReferenceBadge,
  marketReferenceBlock,
  marketReferenceLeads,
} from '../src/services/pricePresentation.js'

const page = readFileSync(new URL('../src/pages/ResearchResults.jsx', import.meta.url), 'utf8')
const service = readFileSync(new URL('../src/services/pricePresentation.js', import.meta.url), 'utf8')
const styles = readFileSync(new URL('../src/styles/index.css', import.meta.url), 'utf8')

// 一个型号被聚合站命中六条，另有一条电商挂牌价——就是用户在「成交项目核价」页上
// 实际看到的那批线索（某型号，task-<run-6> 与 task-<run-5> 同一批）。
const aggregatorLead = amount => ({
  model: 'XKA-2200',
  amount,
  amount_unit: '万元',
  currency: 'CNY',
  kind: 'unparsed_amount',
  url: 'https://www.120shop.cn/i1000001',
  excerpt: `血液专用离心机 | 湘仪 | XKA - 2200 | 某县人民医院 | 某医疗科技公司 | 2024-03-12 | ${amount}万元 | 查看`,
})
const listingLead = {
  model: 'XKA-2200', amount: 4500, amount_unit: '元', currency: 'CNY',
  kind: 'unparsed_amount', url: 'https://www.yiqi.com/product/detail_1000003.html',
  excerpt: '湖南湘仪XKA-2200台式离心机\n参考价格 ¥4500',
}
const leads = [aggregatorLead(1.75), aggregatorLead(1.07), listingLead]
const official = [{kind: 'award', unit_price: 17480, url: 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260301_10000001.htm'}]

test('有官方成交记录时，这一块整个不存在——不是折叠、不是给个数量', () => {
  assert.equal(marketReferenceBlock(leads, official), null)
  // 只有经过核验闸门的 award / contract 才算官方记录；listing 与 bid_amount 混在
  // prices 里都不算。
  assert.notEqual(marketReferenceBlock(leads, [{kind: 'listing', unit_price: 4500}]), null, 'listing 不是官方记录')
  assert.notEqual(marketReferenceBlock(leads, [{kind: 'bid_amount', unit_price: 6400}]), null, 'bid_amount 不是官方记录')
  assert.notEqual(marketReferenceBlock(leads, []), null, '一条官方记录都没有时必须给出这一块')
  assert.equal(marketReferenceBlock(leads, [{kind: 'contract', unit_price: 17480}]), null, 'contract 也是官方成交记录')
})

test('没有官方成交记录时正常展开，并写明它不是原始公告', () => {
  const block = marketReferenceBlock(leads, [])
  assert.equal(block.count, 3)
  assert.equal(block.title, '市场参考价（非成交价）')
  assert.equal(block.badge, '非成交价')
  assert.match(block.warning, /网页上写着的金额不是成交价/)
  assert.match(block.warning, /不能用来判断销售报价是否合理/)
  // 原来和警示句挤在同一段里的两条事实挪到 explanation，一条都不丢。
  assert.match(block.explanation, /没有可核实的同型号成交单价/)
  assert.match(block.explanation, /离成交价有多近/)
  assert.match(block.explanation, /跨度可达数十倍/)
  // 判据是"有没有官方成交记录"，不是"线索自己说不说自己是聚合站"：
  // 只有挂牌价一条时同样按这个判据走。
  assert.equal(marketReferenceBlock([listingLead], []).count, 1)
  // 没有线索就没有这个区块。
  assert.equal(marketReferenceBlock([], []), null)
})

test('警示句只在区块顶部出现一次：逐条线索不重复它', () => {
  // 渲染出来的 JSX 里，警示常量只被引用一次——两条线索也好、七条也好，都是这一处。
  const warningRefs = page.split('{warning}').length - 1
  assert.equal(warningRefs, 1, `警示句渲染点应当只有 1 处，实际 ${warningRefs} 处：多出来的那一处会逐条重复`)
  // 那一处必须在区块最顶部：在标题之后、层级说明/图例/任何一条线索之前。
  const section = page.slice(page.indexOf('function MarketReferenceSection'))
  const positions = {
    warning: section.indexOf('{warning}'),
    tierNote: section.indexOf('market-reference-tier-note'),
    legend: section.indexOf('market-reference-legend'),
    rows: section.indexOf('market-reference-rows'),
    card: section.indexOf('MarketReferenceCard key='),
  }
  for (const [name, at] of Object.entries(positions)) assert.ok(at > -1, `页面里找不到 ${name} 这个锚点`)
  assert.ok(positions.warning < positions.tierNote, '警示句必须在层级说明之前')
  assert.ok(positions.warning < positions.legend, '警示句必须在表头图例之前')
  assert.ok(positions.warning < positions.rows, '警示句必须在线索行之前')
  assert.ok(positions.warning < positions.card, '警示句必须在线索卡片之前')
  // 逐条线索上留下的短标记是"这条自己属于哪一层"，不是这句警示的重复。
  assert.equal(marketReferenceBadge({tier: 1}), '转发记录')
  assert.equal(marketReferenceBadge({tier: 2}), '网页报价')
  for (const badge of ['转发记录', '网页报价', '未标金额', '来源未核实']) {
    assert.ok(!MARKET_REFERENCE_WARNING.includes(badge), `逐行标记「${badge}」不该出现在警示句里`)
  }
})

test('按渲染顺序拼一遍：那 7 条线索上一条都没有这句警示', () => {
  // 浏览器里真正数过的是 DOM（见 docs/待归档-聚合站警示样式.md：整页
  // innerText / textContent 各 1 次）。这里把组件按渲染顺序拼成同样的字符串再数一遍，
  // 不用 DOM 环境就能拦住"警示被塞进每张卡"的回归。
  const {title, warning, explanation, badge, leads: items, count} = marketReferenceBlock(leads, [])
  const banner = `${title}\n${badge}\n${warning}\n${explanation}`
  const tierGroups = [1, 2, 3].map(tier => items.filter(lead => lead.tier === tier)).filter(group => group.length)
  const body = [
    ...tierGroups.map(group => `${MARKET_REFERENCE_TIERS[group[0].tier].title}（${group.length} 条 · 标记「${marketReferenceBadge(group[0])}」）`),
    MARKET_REFERENCE_FIELD_LEGEND,
    // 每条线索卡片：型号 + 金额 + 自己的短标记 + 来源域名（MarketReferenceCard 的全部文本）。
    ...items.map(lead => `${lead.model} · ${formatPriceAmount(lead.amount, lead.currency, lead.amount_unit)} ${marketReferenceBadge(lead)} ${lead.host}`),
    `这 ${count} 条线索没有进入正式成交价比较，也不参与销售报价是否合理的判断。`,
  ].join('\n')
  const pageText = `${banner}\n${body}`
  assert.equal(pageText.split('不能用来判断').length - 1, 1, `整块里警示句应当只有 1 次，实际 ${pageText.split('不能用来判断').length - 1} 次`)
  assert.equal(pageText.split(MARKET_REFERENCE_WARNING).length - 1, 1)
  for (const [index, lead] of items.entries()) {
    const card = `${lead.model} · ${formatPriceAmount(lead.amount, lead.currency, lead.amount_unit)} ${marketReferenceBadge(lead)} ${lead.host}`
    assert.ok(!card.includes('不能用来判断'), `第 ${index + 1} 条线索上重复了警示句`)
  }
  // 三条一次性说明各自只出现一遍。
  assert.equal(pageText.split(MARKET_REFERENCE_EXPLANATION).length - 1, 1)
  assert.equal(pageText.split(MARKET_REFERENCE_FIELD_LEGEND).length - 1, 1)
})

test('警示句在 CSS 里是加粗的横幅，字号不低于正文', () => {
  const rule = styles.slice(styles.indexOf('.market-reference-warning {'))
  const body = rule.slice(0, rule.indexOf('}'))
  // 原来那句和别的解释挤在同一个 <p> 里、只有 600（半粗），用户说不够醒目。
  const weight = /font-weight:\s*(\d+)/.exec(body)
  assert.ok(weight, '警示句必须有显式 font-weight')
  assert.ok(Number(weight[1]) >= 700, `警示句的 computed font-weight 必须 ≥ 700，规则里写的是 ${weight[1]}`)
  const size = /font-size:\s*([\d.]+)rem/.exec(body)
  assert.ok(size && Number(size[1]) >= 1, '警示句字号不小于正文 1rem，别被当成附注')
  assert.match(body, /background:\s*var\(--warning-soft\)/, '警示句要有自己的底色，才在一屏里先被看见')
  assert.match(body, /border-left:\s*4px solid var\(--warning\)/, '警示句要有左侧警示竖线')
  // 旧的半粗段落样式不许留着盖回去。
  assert.ok(!/\.market-reference-block\s*>\s*summary/.test(styles), 'summary 那套样式应当随 <details> 一起去掉')
})

test('同型号多条线索分行，按"离成交价有多近"排序', () => {
  const items = marketReferenceLeads(leads)
  assert.equal(items[0].tier, 1, '带采购单位与日期的转发记录排最前')
  assert.equal(items[0].buyer, '某县人民医院')
  assert.equal(items[0].date, '2024-03-12')
  assert.equal(items[0].host, 'www.120shop.cn')
  assert.equal(items[items.length - 1].tier, 2, '挂牌价排在后一层')
})

test('逐行只剩一个短标记，整句说明每个区块只说一次', () => {
  assert.equal(marketReferenceBadge({tier: 1}), '转发记录')
  assert.equal(marketReferenceBadge({tier: 2}), '网页报价')
  assert.equal(marketReferenceBadge({tier: 3}), '未标金额')
  assert.equal(marketReferenceBadge({}), '来源未核实')
  // 被去掉的逐格说明必须仍然以「每个区块一次」的形式存在。
  assert.match(MARKET_REFERENCE_FIELD_LEGEND, /页面自述/)
  assert.match(MARKET_REFERENCE_FIELD_LEGEND, /非我方核实的公告日期/)
  assert.match(MARKET_REFERENCE_FIELD_LEGEND, /来源也不是采购公示原文/)
  // 每块一次的三句各自只有一个渲染点。
  for (const [name, marker] of [['警示', '{warning}'], ['说明', '{explanation}'], ['图例', '{MARKET_REFERENCE_FIELD_LEGEND}']]) {
    assert.equal(page.split(marker).length - 1, 1, `${name}的渲染点应当只有 1 处`)
  }
  // 警示句的措辞只有单一出处（常量），页面里不许再手写一遍。
  assert.ok(!page.includes('网页上写着的金额不是成交价'), '警示句原文不该在页面里另写一遍')
  assert.ok(!service.includes('不能用来判断销售报价是否合理。同一型号'), '旧的合并长句应当已经拆开')
})

test('三个渲染点都由同一个条件守着，不是各自判一次', () => {
  // 核价页的市场参考区块。
  assert.match(page, /\{section !== 'alternatives' && <MarketReferenceSection leads=\{leads\} values=\{run\.prices \|\| \[\]\} \/>\}/)
  // 候选卡片的「这个型号的价格线索」——block 由 marketReferenceBlock 给出，为 null 就不渲染。
  // 判据传的是 ``run.prices``（这个任务有没有官方成交记录），不是候选人自己的
  // ``candidate.prices``：候选的价格依据常常只有挂牌价，用它当判据会让同一页上
  // 两处结论相反。
  assert.match(page, /const block = marketReferenceBlock\(candidate\.price_leads \|\| \[\], officialPrices\)/)
  // 这一行原来钉的是「``Alternative`` 的入参里有 ``officialPrices``（默认 ``[]``）」。
  // 按需查价给同一个组件又加了三个入参（``lookupResults`` / ``pendingLookups`` /
  // ``onLookupPrice``，见 services/alternativePriceLookup.js），签名不再是原来那个
  // 以 ``officialPrices`` 结尾的字符串；判据本身一个字没动，所以这条只把签名匹配
  // 收到 ``officialPrices = []`` 为止。
  assert.match(page, /function Alternative\(\{ candidate, baseline, sources, reference = false, officialPrices = \[\]/)
  assert.match(page, /\{block && <div className="alternative-leads">/)
  // 跨品牌一组两处、原品牌一处、品牌不全一处、**参考候选一处**——参考候选现在也走
  // 同一张 ``Alternative`` 卡片（查价按钮与来源块都在那张卡上），所以同一个判据
  // （``run.prices``，不是候选自己的 ``candidate.prices``）必须同样传下去。
  assert.equal(page.split('officialPrices={run.prices || []}').length - 1, 5, '每个候选渲染点都要把同一个判据传下去')
  // 待核实价格线索区块只在没有官方成交记录时才补，否则与市场参考区块重复。
  assert.match(page, /\{section !== 'alternatives' && leads\.length > 0 && !prices\.length && <section className="result-section price-leads-section"/)
  // 判据必须是 transactionPrices，不能用别的口径另判一次。
  const marketBlock = service.slice(service.indexOf('export function marketReferenceBlock'))
  assert.match(marketBlock.slice(0, marketBlock.indexOf('\n}')), /transactionPrices\(values\)\.length > 0/)
})

test('前端不再按域名手工排除来源，改由后端给的 authority 决定', () => {
  assert.ok(!service.includes('AGGREGATOR_HOSTS'), '手工域名表又回来了')
  assert.ok(!service.includes('isAggregatorSource'), 'isAggregatorSource 应当已经删掉')
  // 页面读的是后端判定的字段，且与"有没有官方成交记录"同一个条件。
  assert.match(page, /const listedOnly = prices\.length > 0/)
  assert.match(page, /!listedOnly \|\| \(s\.authority && s\.authority !== 'unlisted'\)/)
})

test('线索字段本身没有被删：后端照旧产出，前端只是按条件渲染', () => {
  // 防的是「顺手把数据来源也砍了」：前端仍然读 run.price_leads，
  // 源码里的说明必须仍然记得 republished_prices 在后端存在。
  assert.match(service, /run\.price_leads/)
  assert.match(service, /run\.republished_prices/)
})
