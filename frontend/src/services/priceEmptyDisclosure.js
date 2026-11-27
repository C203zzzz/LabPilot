/**
 * 「价格栏空着」时到底发生了什么。
 *
 * 实测背景：58 轮里 33 轮 ``prices`` 为 0，其中 32 轮连一条聚合站转载价都没有——
 * 价格栏彻底空着。用户 2026-10-06 的口径是**不留白**：把"查了什么、为什么没有"
 * 写清楚。数据全在 run 台账里，这一层只做汇总，不抓任何新东西：
 *
 *   - ``run.usage``：检索／抓取／模型调用各多少次；
 *   - ``run.provider_failure_counts``：每个检索通道失败了几次（>0 的才点名）；
 *   - ``run.unavailable_search_providers``：本轮已被隔离、不再调用的通道；
 *   - ``run.provider_result_totals``：通道"有返回"的调用次数（不是条数）；
 *   - ``run.republished_prices``：聚合站转载价——**二手来源、非一手公告**；
 *   - ``run.price_source_skips``：平台挂牌价／未取到正文等被跳过的来源——**非成交价、
 *     不能用于判断**。
 *
 * 明确不做（用户 2026-10-06 否决）：不新增"同品牌同品类成交价"，不做"第三方报价表"。
 * 这一块只解释"为什么这一栏是空的"，绝不拿别的东西填空。
 */

/** 检索通道的内部名 → 人话。名字取自 ``backend/infrastructure/search_providers.py``
 *  各 Provider 类的 ``name`` 常量（exa_mcp / deepseek_web / public_web / tavily /
 *  baidu / ccgp）。认不出的通道原样印出来，不隐藏。 */
export const SEARCH_CHANNEL_LABELS = {
  reasoning: '思考模型',
  exa_mcp: 'Exa 检索',
  deepseek_web: 'DeepSeek 联网检索',
  public_web: '公开网页直读',
  tavily: 'Tavily 检索',
  ccgp: '中国政府采购网直查',
  baidu: '百度检索',
}

export function searchChannelLabel(name) {
  const key = String(name || '').trim()
  return SEARCH_CHANNEL_LABELS[key] || key || '未命名通道'
}

/** 来源被跳过的原因 → 人话。代号来自后端 ``price_source_skips`` 的 ``skip_reason``。 */
export const PRICE_SKIP_REASON_LABELS = {
  no_amount_evidence: '正文里没有金额证据',
  price_source_limit: '本轮核价来源名额已用完，没轮到它',
  body_not_fetched: '正文没抓到',
  source_type_not_price_source: '来源类型不是成交价来源',
  target_model_not_in_body: '正文里没有这个型号',
}

export function priceSkipReasonLabel(reason) {
  const key = String(reason || '').trim()
  return PRICE_SKIP_REASON_LABELS[key] || key || '未注明原因'
}

const count = value => {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? Math.round(number) : 0
}

/**
 * 空价格栏的交代：一段可直接读的文字 + 逐条事实，页面只排版。
 *
 * ``hasPrices`` 为真时不产出内容（有成交价就不需要交代"为什么没有"）；调用方用
 * ``disclosure.rows.length`` 判断要不要渲染。
 */
export function priceEmptyDisclosure(run = {}) {
  const data = run || {}
  const usage = data.usage || {}
  const search = count(usage.search)
  const fetch = count(usage.fetch)
  const model = count(usage.model)
  const failureCounts = data.provider_failure_counts || {}
  const channels = Object.keys(failureCounts)
  const failed = Object.entries(failureCounts)
    .filter(([, times]) => count(times) > 0)
    .map(([name, times]) => ({ name, label: searchChannelLabel(name), times: count(times) }))
  const quarantined = [...new Set((data.unavailable_search_providers || []).map(searchChannelLabel).filter(Boolean))]
  const productive = Object.entries(data.provider_result_totals || {})
    .filter(([, times]) => count(times) > 0)
    .map(([name, times]) => ({ name, label: searchChannelLabel(name), times: count(times) }))
  const republished = (data.republished_prices || []).length
  const skips = data.price_source_skips || []
  const skipReasons = [...skips.reduce((acc, item) => {
    const key = String(item?.skip_reason || '').trim() || 'unstated'
    acc.set(key, (acc.get(key) || 0) + 1)
    return acc
  }, new Map())].map(([reason, times]) => ({ reason, label: priceSkipReasonLabel(reason), times }))

  const rows = []
  if (search || fetch) {
    const parts = [`本轮发起检索 ${search} 次、抓取正文 ${fetch} 次`]
    if (model) parts.push(`模型调用 ${model} 次`)
    rows.push({ key: 'usage', label: '查了多少', value: `${parts.join('，')}；检索通道共 ${channels.length} 个。` })
  }
  if (productive.length) {
    rows.push({
      key: 'channels',
      label: '通道产出',
      value: productive.map(item => `${item.label} ${item.times} 次有返回`).join('、') + '（计数是"有返回的调用次数"，不是金额条数）。',
    })
  }
  if (failed.length || quarantined.length) {
    const failedText = failed.map(item => `${item.label}失败 ${item.times} 次`).join('、')
    const quarantineText = quarantined.length ? `；${quarantined.join('、')}本轮已被隔离，不再调用` : ''
    rows.push({ key: 'failures', label: '失败或被限流的通道', value: `${failedText || '没有记录到通道失败'}${quarantineText}。` })
  } else if (channels.length) {
    rows.push({ key: 'failures', label: '失败或被限流的通道', value: '本轮没有通道失败或限流的记录。' })
  }
  rows.push({
    key: 'republished',
    label: '聚合站转载价',
    value: republished
      ? `${republished} 条。这些是聚合站、门户站转载的第二手金额，不是一手公告原文，不能当作成交价。`
      : '0 条；本轮连聚合站转载的金额也没有读到。',
  })
  rows.push({
    key: 'skips',
    label: '平台挂牌价等被跳过的来源',
    value: skips.length
      ? `${skips.length} 条来源没有进入核价：${skipReasons.map(item => `${item.label} ${item.times} 条`).join('、')}。这些金额是挂牌价、预算或与型号不符的内容，不是成交价，不能用于判断。`
      : '0 条；本轮没有留下被跳过的价格来源记录。',
  })
  rows.push({
    key: 'conclusion',
    label: '结论',
    value: '本轮没有取得可采信的同型号成交价，所以无法判断这份销售报价是否合理；这也不代表这台机器在别处没有成交记录。',
  })
  return { rows, failed, productive, quarantined, republished, skipReasons }
}
