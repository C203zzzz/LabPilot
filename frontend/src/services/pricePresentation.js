export const hasDisplayValue = value => value != null && value !== '' && value !== 'unknown'

export function displayPriceValue(value, fallback = '未提供') {
  if (!hasDisplayValue(value)) return fallback
  if (Array.isArray(value)) return value.map(item => displayPriceValue(item, fallback)).join(' 至 ')
  if (typeof value === 'object') {
    return Object.entries(value).map(([key, item]) => `${key}：${displayPriceValue(item, fallback)}`).join('；')
  }
  return String(value)
}

/**
 * Why a commercial term of a price record is missing, or '' when it is present.
 *
 * A bare "未提供" reads the same whether the announcement and its attachment
 * never mention the term or the extractor read something it could not tie back
 * to their own words. The backend records which one it was, so the card can
 * say so; records stored before that still fall through to the plain value.
 */
export function termAbsenceReason(price, key) {
  if (hasDisplayValue(price && price[key])) return ''
  return ((price && price.field_notes) || {})[key] || ''
}

export function formatPriceAmount(value, currency = 'CNY', amountUnit = '') {
  if (!hasDisplayValue(value) || !Number.isFinite(Number(value))) return '未提供'
  const amount = Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
  if (amountUnit) return `${amount} ${amountUnit}`
  return currency === 'CNY' ? `${amount} 元` : `${amount} ${currency || '币种未说明'}`
}

export function transactionPrices(values = []) {
  return values.filter(item => ['award', 'contract'].includes(item?.kind))
}

/**
 * 页面要列出的成交记录，按**成交日期从新到旧**排。
 *
 * 后端 ``run['prices']`` 的顺序不是价格顺序：``procurement_research._merged_prices``
 * 是"先见先留"的并集（``_merge_price_rows`` docstring：先读到的那条保住自己的位置），
 * 而 ``prices()`` 扫来源时用的 ``_price_source_rank``（定义在
 * ``procurement_research.py:333-345``，``sorted(sources, key=…)`` 的调用处在 3261 行）
 * 自己那段 docstring 写的是 "Order in which a price **budget spends its calls** on
 * eligible sources"——**它是"预算先花在谁身上"的排序，不是价格排序**。所以
 * 同一个型号有多条记录时，谁排前面取决于这次扫到了哪个来源，跟金额、日期都无关。
 * 页面上既然要给每条编号（「第 n 条 / 共 m 条」），就得先有个说得出口的顺序，否则
 * 「第 1 条」会因为一次无关的检索顺序变化而换人。这里选日期从新到旧：价格比较里
 * 越新的成交越有参考价值。
 *
 * 两条纪律：
 * - **日期读不出来的排在有日期的后面**（不能猜，也不能当成最新），它们之间保持
 *   后端原序——``Array.prototype.sort`` 在现代 V8 里是稳定排序，返回 0 即保序；
 * - 不原地排序，返回新数组——``run.prices`` 是调用方的数据，``transactionPrices``
 *   还是"有没有官方成交记录"的判据，两处不能互相影响。
 */
export function orderedTransactionPrices(values = []) {
  const stamp = price => {
    const raw = String((price && price.date) || '').trim()
    // 只认「四位年 + 任意分隔 + 月 + 任意分隔 + 日」这种写法（后端存的是
    // ``2026-03-25``，历史数据里也有 ``2026年3月25日``），归一成 ``20260325``。
    // **不走 ``Date.parse``**：``2026-03-25`` 按 UTC 解析、``2026-3-25`` 按本地时区
    // 解析，同一个日历日会因为写法不同排出先后，而这里只需要比到"哪一天"。
    const matched = raw.match(/^(\d{4})\D+(\d{1,2})\D+(\d{1,2})/)
    if (!matched) return null
    return Number(`${matched[1]}${matched[2].padStart(2, '0')}${matched[3].padStart(2, '0')}`)
  }
  return transactionPrices(values).map((price, index) => ({price, index, at: stamp(price)}))
    .sort((left, right) => {
      if (left.at === right.at) return left.index - right.index
      if (left.at === null) return 1
      if (right.at === null) return -1
      return right.at - left.at
    })
    .map(entry => entry.price)
}

const PROCUREMENT_UNIT_KEYS = ['procurement_unit', 'purchase_unit', 'purchaser', 'buyer',
  'purchasing_unit', 'institution', 'organization', 'purchaser_name']

/** 这一条成交记录里有没有写采购单位；没写就不该在页面上印"原文未提取"。 */
export function hasProcurementUnit(price = {}) {
  return PROCUREMENT_UNIT_KEYS.some(key => hasDisplayValue(price[key]))
}

export function procurementUnit(price = {}) {
  const value = PROCUREMENT_UNIT_KEYS.map(key => price[key]).find(hasDisplayValue)
  return displayPriceValue(value, '原文未提取')
}

// --- 产品资料上的标称参数 ---------------------------------------------------
//
// 中标公告只写型号、品牌、数量、单价，从不写技术参数，所以配置那一格经常是空的。
// 后端会在同一批来源里找**这个型号自己的产品参数页**，把参数取回来，并用
// ``configuration_source`` 标明它来自产品资料而不是这笔成交记录（取值见
// ``backend/services/product_configuration.py``）。
//
// 这里只做两件事：认出它、把出处准备好给页面印。判据必须是后端给的那个来源标记，
// 不能是「configuration 有值就算」——公告正文或合同附件里写的配置是另一种证据，
// 把它当厂家参数印，与把采购需求当参数印是同一类错误。
export const PRODUCT_SPEC_SOURCES = {
  manufacturer_spec: '厂家资料',
  product_page_spec: '产品资料',
}

/** 这条成交记录的配置是不是产品资料上的标称参数；不是就返回 null。 */
export function productSpec(price = {}) {
  const label = PRODUCT_SPEC_SOURCES[price.configuration_source]
  if (!label || !hasDisplayValue(price.configuration)) return null
  const url = String(price.configuration_url || '')
  return { text: String(price.configuration), url, host: hostOf(url), label }
}

export function hostOf(url) {
  return String(url || '')
    .replace(/^[a-z]+:\/\//i, '')
    .split('/')[0]
    .split('?')[0]
    .toLowerCase()
}

// --- 一条成交记录自己的出处 ------------------------------------------------
//
// 用户要的排版是三层：型号与价格并排、配置、来源；「来源」默认折叠，展开后给出
// **价格来源**和**配置来源**两条链接。价格来源就是这条金额所在的公告／附件页面，
// 后端把它写在记录的 ``url`` 上（``procurement_rules.validate_price`` 里
// ``quote = {**raw, ..., 'url': source['url'], ...}``）。
//
// 为什么是一个字段列表、不是直接取 ``price.url``：用户明确要求"价格来源可能不止
// 一条，按列表渲染，别只取第一条就把其余丢了"。写成列表以后后端多写一个来源
// 字段时，加进这里即可，页面不会只认一条。
//
// 现在列表里只有 ``url`` 一项，这是读代码得到的结论（``validate_price`` 的构造、
// 以及价格抽取模板里的字段）；**没有从真实数据验证过第二个字段**——库里唯一带
// ``prices`` 的 run 是空数组，成交记录一条都没有。
export const PRICE_SOURCE_FIELDS = ['url']

/** 这条成交记录给出的价格来源地址，按字段顺序去重；一条都没有时返回空数组。 */
export function priceSourceLinks(price = {}) {
  const links = []
  for (const field of PRICE_SOURCE_FIELDS) {
    const url = String((price || {})[field] || '').trim()
    if (url && !links.includes(url)) links.push(url)
  }
  return links
}

// --- 商业线索：有官方成交记录时不出现，没有时才作为唯一参考 -------------------
//
// 用户的定位（原话）：「这个产品是面向历史招标公告的比价工具」。规则因此是按任务
// 分两种，判据只有一个：这一轮有没有可采信的官方成交记录（``transactionPrices``
// 非空，也就是主区那张成交价卡片）。有官方记录时，聚合站与电商页面的金额是"历史
// 招标公告"之外的商业线索，前端一律不承载——不出现域名，也不出现「聚合站」「转载」
// 「镜像」这类字样；一条官方记录都没有时它们**正常展示**（不折叠、不只给个数量），
// 因为那时它是这台机器唯一的交易信号，藏起来用户会以为市场上没有任何记录。
//
// 数据没有删：`run.price_leads`、`run.sources`、`run.republished_prices` 仍由后端
// 原样产出并留在库里，后端照样把它们当发现渠道。这里只控制**前端渲染**。

const ROW_SPLIT = /\s*[|｜]\s*/
//: 只在原文里找"这是对外报价"的证据。不能用 note 当证据：每条线索的说明都
//: 写着"不能作为成交价或当前报价"，那样会把转发的成交记录也判成挂牌价。
const LISTING_PATTERN = /市场价|销售价|挂牌价|零售价|京东价|参考价|官网价|促销价|特价|供应价|价格[：:]\s*[¥￥$]/
const ASK_PATTERN = /面议|电议|询价|议价/

/** 从线索原文里读出"谁买的、谁卖的、什么时候"，读不到就留空。 */
export function marketReferenceDetails(lead = {}) {
  const excerpt = String(lead.excerpt || '')
  if (ROW_SPLIT.test(excerpt)) {
    const cells = excerpt.split(ROW_SPLIT).map(cell => cell.trim()).filter(Boolean)
    const date = cells.find(cell => /^\d{4}[-/年]\d{1,2}([-/月]\d{1,2}日?)?$/.test(cell))
    const buyers = cells.filter(cell => /大学|学院|医院|研究所|监督|检测|中心|学校|研究院|疾控|海关/.test(cell))
    const sellers = cells.filter(cell => /有限公司|有限责任|贸易行|仪器厂|科技|生物|进出口/.test(cell))
    return { date: date || '', buyer: buyers[0] || '', seller: sellers[0] || '' }
  }
  return { date: '', buyer: '', seller: '' }
}

function listingKind(lead) {
  const excerpt = String(lead.excerpt || '')
  if (LISTING_PATTERN.test(excerpt)) return 'asking'
  if (ASK_PATTERN.test(excerpt)) return 'no_price'
  return 'unknown'
}

const HOST_LABELS = [
  [/(^|\.)120shop\.cn$/, '中标记录聚合站'],
  [/(^|\.)instrument\.com\.cn$|(^|\.)yiqi\.com$|(^|\.)app17\.com$|(^|\.)bingosci\.com$|(^|\.)sns17\.com$/, '仪器电商 / 门户'],
  [/(^|\.)qianlima\.com$|(^|\.)ccgp\.gov\.cn$/, '招标信息平台'],
]

function hostLabel(url) {
  const host = hostOf(url)
  const match = HOST_LABELS.find(([pattern]) => pattern.test(host))
  return match ? match[1] : ''
}

//: 上面这张表是「域名 → 来源类别」，不是站点名。链接上要写「这条指向哪」时，
//: 站点名在 ``services/siteNames.js``；那边认不出来的域名**回头看这里**取一个
//: 类别名兜底，不另造一套。
export { hostLabel }

/**
 * 把待核实线索按"离成交价有多近"分层，供前端在权威价格缺失时展示。
 *
 * 第一层：转发的历史成交记录（有日期或采购单位，最接近成交价但出处未验真）
 * 第二层：网页挂牌价 / 报价（经销商、电商的对外价格，通常高于成交价）
 * 第三层：明确"面议/询价"的页面（没有价格，只说明有人在卖）
 */
export function marketReferenceLeads(leads = []) {
  const enriched = (leads || [])
    .filter(lead => lead && lead.url)
    .map(lead => {
      const details = marketReferenceDetails(lead)
      const kind = listingKind(lead)
      // 0 元通常是电商模板的占位值，不能算作"有价格"。
      const hasValue = Number.isFinite(Number(lead.amount)) && Number(lead.amount) > 0
      const tier = hasValue && kind !== 'asking' && (details.date || details.buyer || details.seller) ? 1
        : hasValue ? 2
          : 3
      return { ...lead, ...details, listingKind: kind, tier, host: hostOf(lead.url), hostLabel: hostLabel(lead.url) }
    })

  const tierRank = { 1: 0, 2: 1, 3: 2 }
  return enriched.sort((left, right) => {
    if (tierRank[left.tier] !== tierRank[right.tier]) return tierRank[left.tier] - tierRank[right.tier]
    if (left.tier === 1) return String(right.date || '').localeCompare(String(left.date || ''))
    const amount = Number(right.amount) - Number(left.amount)
    return Number.isFinite(amount) && amount !== 0 ? amount : String(left.url).localeCompare(String(right.url))
  })
}

export const MARKET_REFERENCE_TIERS = {
  1: {
    title: '转发的历史成交记录',
    summary: '网页上带采购单位或成交日期的金额。它们最接近成交价，但出处是转发页面，本轮没能核到原始公告，因此没进入正式成交价比较。',
  },
  2: {
    title: '网页报价 / 经销商挂牌价',
    summary: '厂商、经销商或电商页面写明的价格。这是对外报价，通常高于实际成交价，只能说明量级。',
  },
  3: {
    title: '只有"面议 / 询价"的页面',
    summary: '页面上没有可提取的金额，只能说明有人在卖这个型号。',
  },
}

/**
 * 一行线索最必要的短标记，块标题与块说明已经把来源身份说清楚了。
 *
 * 原来每张卡都印一遍「转发记录，原始公告未核实 · 不能作为成交价」这种整句，
 * 7 条线索就是 7 遍，卡尾还各有一行「来源为中标记录聚合站，页面本身不是原始
 * 公告」；块级说一次之后，逐行只留「这是哪一类记录」这一个短标记。
 */
export function marketReferenceBadge(lead = {}) {
  const notes = {1: '转发记录', 2: '网页报价', 3: '未标金额'}
  return notes[lead.tier] || '来源未核实'
}

//: 这张卡上被去掉的逐格说明，合成一句放在区块表头。原来「（页面自述）」在日期、
//: 采购方、供货方后面各挂一遍（一张卡三遍、七张卡二十一遍），「非我方核实的公告
//: 日期」六遍；现在只在这里说一次。写成一个常量是为了让这句话有单一出处——
//: 少印几遍可以，事实本身一句都不能丢。
export const MARKET_REFERENCE_FIELD_LEGEND = '上表写的是页面上的金额与出处：日期与买卖双方均为页面自述，非我方核实的公告日期；来源也不是采购公示原文。'

//: 区块顶部那一句警示，**整页只说一次**（每条线索不再各印一遍）。
//: 用户口径（原话）：「要在展示聚合站内容的顶部加粗标出 但不能重复展示 比如每一条
//: 价格都展示出这个提醒」。所以它写成一个常量、只有 ``MarketReferenceSection`` 顶部
//: 一个渲染点；逐行留下的 ``marketReferenceBadge`` 是"这条自己属于哪一层"的短标记，
//: 不是这句警示的重复。
export const MARKET_REFERENCE_WARNING = '网页上写着的金额不是成交价，不能用来判断销售报价是否合理。'

//: 原来和警示句挤在同一段里的两条事实。分开的是排版，事实一条都不丢：为什么没有可比的
//: 成交价、下面这些数字按什么排序、跨度为什么这么大。
export const MARKET_REFERENCE_EXPLANATION = '本轮没有可核实的同型号成交单价。下面是网页上实际写着的金额，按“离成交价有多近”排序；同一型号的网页报价跨度可达数十倍，差异主要来自配置与商务条件。'

/**
 * 这一轮的商业线索要不要展示，以及展示时标题写什么。
 *
 * 判据只有一个：``values``（``run.prices``）里有没有走过核验闸门的官方成交记录
 * （``transactionPrices`` 非空）。有 → 返回 ``null``，页面上连这一块都不存在；
 * 没有 → 返回展开的区块：``warning`` 是顶部那一句加粗警示（整块只说一次），
 * ``explanation`` 是它下面垫底的说明，``leads`` 逐条只带自己的分层标记。
 *
 * 这个判据与报告「二手成交记录」那一节、来源清单的过滤用的是同一个事实，
 * 三处不各判一次，也不另造判据。
 */
export function marketReferenceBlock(leads = [], values = []) {
  const items = marketReferenceLeads(leads)
  if (!items.length) return null
  if (transactionPrices(values).length > 0) return null
  return {
    leads: items,
    count: items.length,
    badge: '非成交价',
    title: '市场参考价（非成交价）',
    warning: MARKET_REFERENCE_WARNING,
    explanation: MARKET_REFERENCE_EXPLANATION,
  }
}

// --- 同一型号的多条线索合成一行 ---------------------------------------------
//
// 一个型号在网页上常被十几条线索命中（不同配置、不同卖家、转发记录），逐条铺开
// 既占满页面、又让每条重复同一段说明。这里把同型号合成一组，用一个金额区间表示，
// 逐条来源收进折叠里。

const UNIT_SCALE = unit => (/万/.test(String(unit || '')) ? 10000 : 1)

/** 把带单位的金额换算成元，用于排序和取区间。读不到金额时返回 NaN。 */
export function amountInYuan(lead = {}) {
  if (!hasDisplayValue(lead.amount)) return Number.NaN
  const value = Number(lead.amount)
  // 万元换算会留下浮点尾数（1.38 万 → 13799.999…），金额取整到元。
  return Number.isFinite(value) ? Math.round(value * UNIT_SCALE(lead.amount_unit)) : Number.NaN
}

export function priceLeadGroups(leads = []) {
  const groups = new Map()
  for (const lead of leads) {
    const key = String(lead?.model || '').trim() || '型号未标注'
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(lead)
  }
  return [...groups].map(([model, items]) => {
    const sorted = [...items].sort((left, right) => amountInYuan(left) - amountInYuan(right))
    const priced = sorted.filter(item => Number.isFinite(amountInYuan(item)))
    return {model, leads: sorted, low: priced[0] || null, high: priced[priced.length - 1] || null}
  })
}

/** 一组线索的金额区间；只有一条时就是这个金额本身。 */
export function priceRangeText(group) {
  const amount = lead => formatPriceAmount(lead.amount, lead.currency, lead.amount_unit)
  if (!group?.low) return '页面未给出金额'
  if (!group.high || group.low === group.high) return amount(group.low)
  return `${amount(group.low)} ~ ${amount(group.high)}`
}

/** 这一组线索该按核价页还是其他品牌页取；两页取的不是同一批数据。 */
export function priceLeadsForSection(run = {}, alternatives = [], section = 'price') {
  const direct = run.price_leads || []
  const alternative = alternatives.flatMap(item => item.price_leads || [])
  const selected = section === 'price' ? direct : section === 'alternatives' ? alternative : [...direct, ...alternative]
  return [...new Map(selected.map(item => [
    [item.url, item.model, item.amount, item.kind].join('|'),
    item,
  ])).values()]
}


