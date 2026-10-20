// 连接状态的字面口径。
//
// 后端 `backend/api/routes.py:459-464` 对视觉适配器返回四种状态：
// `configured`（本轮自检通过）、`ready_to_test`（适配器存在，本轮还没跑过视觉自检）、
// `payment_required`、`unavailable`。`ready_to_test` 是「可用但未验证」，
// 不是「不能连」——旧界面把它当成了未连接，于是按钮被禁用、还挂了「视觉模型未连接」。

export function providerState(providerStatus, id) {
  const item = providerStatus?.items?.find(entry => entry?.id === id)
  const status = item?.status || 'missing'
  return {
    id,
    status,
    label: item?.label || '',
    configured: status === 'configured',
    readyToTest: status === 'ready_to_test',
    available: status === 'configured' || status === 'ready_to_test',
    verified: status === 'configured',
  }
}

/** 视觉识别按钮的可用性、标签与原因，三处口径一致。 */
export function visionState(providerStatus) {
  const { status } = providerState(providerStatus, 'vision')
  // 连"读状态"这一步都失败时（后端配置项写错会返回带 code 的 502），原因就在
  // `providerStatus.error` 的原话里。这时说"未连接"是把"配置写错了"讲成了
  // "连不上"；宁可说配置有误，并把后端原话放在 hint 里。
  if (providerStatus?.error) {
    return { status, available: false, verified: false, label: '视觉模型配置有误', tone: 'danger', hint: providerStatus.error }
  }
  if (status === 'configured') {
    return { status, available: true, verified: true, label: '视觉模型已连接', tone: 'success', hint: '从图片或扫描页提取文字和参数' }
  }
  if (status === 'ready_to_test') {
    return {
      status,
      available: true,
      verified: false,
      label: '视觉模型可用 · 尚未验证',
      tone: 'neutral',
      hint: '识别服务已配置，本轮还没有跑过视觉自检；可以直接识别图片内容，结果仍按未核实处理',
    }
  }
  if (status === 'payment_required') {
    return { status, available: false, verified: false, label: '视觉模型余额不足', tone: 'danger', hint: '识别服务余额不足，请先在连接设置处理后再识别图片内容' }
  }
  return { status, available: false, verified: false, label: '视觉模型未连接', tone: 'warning', hint: '请先连接图片识别服务' }
}

/**
 * 顶栏（侧栏底部）连接状态的字面口径，与 ``visionState`` 同一套判断。
 *
 * 后端 `/providers` 在配置项写错时返回带 code 与中文原话的 502（例如
 * ``LAB_VISION_JSON_RETRIES=9`` 越界），``services/api.js`` 的
 * ``fetchProviderStatus`` 已经把 404 与网络错误当成"服务未连接"正常返回，
 * 所以能走到 ``error`` 非空的只有"服务在、配置有问题"。这时两行各说一句
 * "未连接"就是把"装错了"讲成了"连不上"——配置错误要让用户看见是配置错了。
 */
export function connectionSummary(providerStatus) {
  const error = providerStatus?.error || ''
  const byId = Object.fromEntries((providerStatus?.items || []).map(item => [item.id, item.status]))
  return {
    error,
    problem: error ? '配置有误' : '',
    model: byId.reasoning === 'configured',
    search: byId.search === 'configured',
  }
}
