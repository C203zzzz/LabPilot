// 产品资料识别出的型号优先当比较基准；销售口述的型号和它的报价分开显示。
// 这里只做「怎么把两个型号说清楚」，不判断资料是否可用——那是后端的口径。

const named = (brand, model) => [brand, model].map(value => String(value ?? '').trim()).filter(Boolean).join(' ')

export const modelKey = value => String(value ?? '').replace(/[\s\-_/]+/g, '').toUpperCase()

/** 报价挂在口述型号上、比较基准却是另一个型号时，返回那个口述型号；否则返回空串。 */
export const quotedModelOf = baseline => {
  const quoted = String(baseline?.quote_model ?? '').trim()
  const basis = String(baseline?.model ?? '').trim()
  return quoted && modelKey(quoted) !== modelKey(basis) ? quoted : ''
}

export const quotedNameOf = baseline => named(baseline?.quote_brand, quotedModelOf(baseline))

/** 一句话说清这笔报价属于哪台机器；没有跨型号时返回空串。 */
export const quoteAttribution = baseline => {
  const quoted = quotedModelOf(baseline)
  if (!quoted) return ''
  return `这笔报价对应销售推荐的 ${quotedNameOf(baseline) || quoted}，不是本次比较基准 ${String(baseline?.model ?? '').trim()} 的报价。`
}

/** 采购对话上方那条「资料识别到的型号」说明。 */
export const documentIdentityLead = identity => {
  if (!identity) return ''
  const documentName = named(identity.brand, identity.model) || String(identity.model ?? '').trim()
  if (identity.overrides_spoken) {
    const spoken = named(identity.spoken_brand, identity.spoken_model) || String(identity.spoken_model ?? '').trim()
    return `比较基准已按产品资料改为 ${documentName}；销售提到的 ${spoken} 和它的报价保留为待核实的销售推荐。`
  }
  return `资料已识别：${documentName}。已预填到下方摘要，确认后作为比较对象；资料参数不等于你的实验要求。`
}

/** 已确认的比较基准和产品资料型号不一致时的提示；一致时返回空串。 */
export const documentBasisConflict = (document, baseline) => {
  const model = String(document?.model ?? '').trim()
  if (!model || !baseline?.model) return ''
  if (modelKey(model) === modelKey(baseline.model)) return ''
  return `产品资料里的型号是 ${named(document.brand, model)}，与上次确认的比较基准 `
    + `${named(baseline.brand, baseline.model)} 不同。已按产品资料为准把下方基准预填为 ${model}，`
    + '请核对后重新确认；上一次的结果和报告仍对应旧型号，不会自动改写。'
}
