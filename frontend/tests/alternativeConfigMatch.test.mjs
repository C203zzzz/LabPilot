/**
 * 候选卡片上的「读到的配置」。
 *
 * 口径：候选卡片**只罗列资料原文**，不替用户判断是否满足需求
 * （``回归基线`` 的「业务口径」第二条；规范项 的
 * 「结果里只摆配置原文，不再输出满足 / 差异 / 未知」）。所以这一层只回答两件事：
 * 这条候选的页面上写到了哪些配置项、写的是什么（连出处）；没写到的有几项。
 *
 * 下面这些 fixture 逐字照抄真跑产物 ``output/alt-extract/live-extract5.json``
 * （H1650K 那一条：转速 16500 r/min、最大相对离心力 26054 xg、类型 observed=null），
 * 只把长正文截短——字段名一个都没改。fixture 里仍然带着后端的 ``status`` / ``kind``
 * 字段，而这一层**故意不读它们**：谁把它们读回来做判定，下面第 2 条测试就会红。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  BRAND_UNRESOLVED,
  CANDIDATE_LISTING_BOUNDARY,
  candidateBrandLabel,
  candidateIdentity,
  candidateTitle,
  configListing,
  configSourceRows,
} from '../src/services/alternativePresentation.js'

const DETAIL = 'https://www.yiqi.com/product/detail_1156692.html'

const differentItem = {
  name: '最高转速',
  required: { name: '最高转速', value: 20000, unit: 'r/min', direction: 'min' },
  observed: {
    value: 16500.0, unit: 'r/min', conditions: '',
    evidence: { url: DETAIL, excerpt: '[表格按列展开 表 2 列] H1650K 上限转速 16500r/min' },
  },
  status: 'violated',
  kind: 'different',
  shortfall_text: '差额：实读比要求低 17.5%（以要求值为基准）。',
}

const matchItem = {
  name: '最大相对离心力',
  required: { name: '最大相对离心力', value: 25000, unit: '×g', direction: 'min' },
  observed: {
    value: 26054.0, unit: 'xg', conditions: '',
    evidence: { url: DETAIL, excerpt: '[表格按列展开 表 2 列] H1650 上限相对离心力 26054xg' },
  },
  status: 'satisfied',
  kind: 'match',
}

const unknownItem = {
  name: '类型',
  required: { name: '类型', value: '高速冷冻离心机', direction: 'equal' },
  observed: null,
  status: 'unknown',
  kind: 'unknown',
}

const candidate = (extra = {}) => ({
  candidate_id: '9a8b7c6d5e4f3021',
  brand: '',
  model: 'H1650K',
  url: DETAIL,
  excerpt: '高速离心机-湘仪H1650K台式微量高速离心机',
  config_match: {
    score: 0.5,
    items: [differentItem, matchItem, unknownItem],
    diff_points: ['最高转速：要求 20000 r/min，正文查得 16500 r/min，不满足。'],
    hard_failures: ['最高转速'],
    soft_score: 0.959545559223152,
    confidence: 0.6666666666666666,
    basis: 'compared',
  },
  price_lookup: { status: 'not_requested', reason_code: 'not_requested' },
  ...extra,
})

test('只列页面上写到的配置项，没写到的只报个数', () => {
  const rows = configSourceRows(candidate())
  assert.deepEqual(rows.map(row => row.name), ['最高转速', '最大相对离心力'])
  assert.equal(rows[0].value, '16500 r/min')
  assert.equal(rows[1].value, '26054 xg')
  const listing = configListing(candidate())
  assert.equal(listing.present, true)
  assert.equal(listing.readCount, 2)
  assert.equal(listing.missingCount, 1, '「类型」那条页面上没写到，要如实报个数')
  assert.equal(listing.emptyNote, '')
})

test('不再输出「满足／差异／未知」：判定标签、差额、分数一个都不进配置栏', () => {
  const rows = configSourceRows(candidate())
  const rendered = JSON.stringify(rows)
  for (const word of ['满足', '不满足', '差异', '未知', '一致', '不同', '缺少证据', '17.5']) {
    assert.ok(!rendered.includes(word), `配置栏里还在输出「${word}」：${rendered}`)
  }
  const listing = configListing(candidate())
  assert.deepEqual(Object.keys(listing).sort(), ['boundary', 'emptyNote', 'missingCount', 'present', 'readCount', 'rows', 'watchedCount'])
  // 差额那句话（``shortfall_text``）是判定的一部分，不许出现在行结构里。
  assert.ok(!('shortfallText' in rows[0]) && !('state' in rows[0]) && !('stateLabel' in rows[0]))
})

test('出处与原文逐项给出（不印网址本身，链接文字由页面给）', () => {
  const rows = configSourceRows(candidate())
  assert.equal(rows[0].url, DETAIL)
  assert.match(rows[0].excerpt, /16500r\/min/)
  // 原文那一栏是独立的一列：值是从结构化字段来的，原文照样留住，读者能回原文核对。
  assert.match(rows[1].excerpt, /26054xg/)
  assert.equal(rows[1].url, DETAIL)
})

test('只有原文、没有结构化值时印原文，不印空值', () => {
  const rows = configSourceRows({ config_match: { items: [
    { name: '腔体尺寸', observed: { value: null, evidence: { url: DETAIL, excerpt: '灭菌腔 30 直径*71深 cm' } }, status: 'unknown', kind: 'unknown' },
  ] } })
  assert.equal(rows.length, 1)
  assert.equal(rows[0].value, '灭菌腔 30 直径*71深 cm')
  assert.equal(rows[0].excerpt, '')
})

test('一项都没读到时给一句人话，不留空白、也不铺一排"未知"', () => {
  const listing = configListing(candidate({ config_match: { items: [unknownItem] } }))
  assert.equal(listing.readCount, 0)
  assert.deepEqual(listing.rows, [])
  assert.equal(listing.missingCount, 1)
  assert.match(listing.emptyNote, /未读到可引用的配置原文/)
  assert.ok(!listing.emptyNote.includes('未知'))
})

test('边界句说清"这不是满足结论"，且是模块里的唯一出口', () => {
  assert.match(CANDIDATE_LISTING_BOUNDARY, /不代表其满足本轮实验要求/)
  assert.equal(configListing(candidate()).boundary, CANDIDATE_LISTING_BOUNDARY)
})

test('没有 config_match 或字段形状不对时给空结构，不抛异常、也不假装读到了', () => {
  const missing = configListing({ model: 'X' })
  assert.equal(missing.present, false)
  assert.deepEqual(missing.rows, [])
  assert.deepEqual(configSourceRows({ config_match: { items: 'not-a-list' } }), [])
  assert.deepEqual(configSourceRows(null), [])
  assert.equal(configListing({ config_match: { items: [] } }).present, true)
})

test('配置栏也能用 matrix 里读到的原文（天平那一轮只有它）', () => {
  // 实测 ``b3-opt-balance-mt-me204e``：``config_match.items`` 四项全空，值只落在
  // ``matrix`` 的 ``raw_evidence`` 里。只读第一处，配置栏会显示"什么都没读到"。
  const rows = configSourceRows({
    model: 'PTY-124/223',
    matrix: [
      { required: { name: '量程' }, observed: null, raw_evidence: [{ url: 'http://www.fzhz.com.cn/sys-pd/40.html', excerpt: 'PTY-124/223 | 120/220 | 0.1/0.5 | ±0.1/±0.5' }] },
      { required: { name: '分度值' }, observed: { value: null }, raw_evidence: [] },
    ],
  })
  assert.deepEqual(rows.map(row => row.name), ['量程'])
  assert.equal(rows[0].value, 'PTY-124/223 | 120/220 | 0.1/0.5 | ±0.1/±0.5')
  assert.equal(rows[0].url, 'http://www.fzhz.com.cn/sys-pd/40.html')
  const listing = configListing({
    matrix: [
      { required: { name: '量程' }, raw_evidence: [{ url: 'https://example.com/a', excerpt: '量程 120/220' }] },
      { required: { name: '分度值' }, raw_evidence: [] },
    ],
  })
  assert.equal(listing.readCount, 1)
  assert.equal(listing.missingCount, 1)
  assert.equal(listing.present, true)
})

test('候选自己的资料正文整段摆出来，标成「配置原文」', () => {
  // 实测洁净台那一轮（bekomat30）：``config_match`` 与 ``matrix`` 都是空的，唯一的
  // 配置原文在 ``configuration`` 上——M4 的"有配置"判据认的就是它。
  const listing = configListing({
    model: 'bekomat30',
    configuration: '型号 技术参数 | BBS-V600\n外形尺寸 | 600×580×1060mm\n',
    configuration_url: 'https://www.olaibo.com/ProductDetail/630.html',
  })
  assert.equal(listing.readCount, 1)
  assert.equal(listing.rows[0].name, '配置原文')
  assert.equal(listing.rows[0].block, true)
  assert.match(listing.rows[0].value, /BBS-V600/)
  assert.equal(listing.rows[0].url, 'https://www.olaibo.com/ProductDetail/630.html')
  assert.equal(listing.missingCount, 0)
  assert.equal(listing.present, true)
  // 占位值不算配置原文。
  assert.deepEqual(configSourceRows({ configuration: 'unknown' }), [])
  assert.deepEqual(configSourceRows({ configuration: '   ' }), [])
})

test('品牌解析不出来时印「品牌未解析」，不留空', () => {
  assert.equal(BRAND_UNRESOLVED, '品牌未解析')
  assert.equal(candidateBrandLabel({ model: 'H1650K' }), '品牌未解析')
  assert.equal(candidateBrandLabel({ brand: '  ' }), '品牌未解析')
  assert.equal(candidateTitle({ model: 'H1650K' }), '品牌未解析 H1650K')
  assert.equal(candidateTitle({ brand: '湘仪', model: 'H1650K' }), '湘仪 H1650K')
})

test('品牌缺失时用供应商作为展示身份，但不把供应商当品牌', () => {
  const item = { supplier: '苏州捷美电子有限公司', model: 'SYG-1210' }
  assert.deepEqual(candidateIdentity(item), {
    value: '苏州捷美电子有限公司', kind: 'supplier', label: '供应商',
  })
  assert.equal(candidateTitle(item), '供应商：苏州捷美电子有限公司 SYG-1210')
  assert.equal(candidateBrandLabel(item), '品牌未解析')
})
