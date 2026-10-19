/**
 * Q13b：来源清单按「这条来源能不能用来判断报价」分组。
 *
 * 判据只用后端给的 `source_type`（`backend/services/procurement_research.py` 写 run.sources
 * 时逐条落库），不按域名猜、不按标题猜。用户质疑「后端不是只读官方权威域名吗，
 * 为什么最前面是普通仪器商页面」——这里是展示层，不替后端下结论：
 * 把每一条的身份摆到明面上，普通商家页面不再混在公告前面看起来像权威来源，
 * 采购公告线索也不会被藏起来。
 *
 * 三条规则必须覆盖全部来源：`groupSources` 的第二项返回值就是被分类的条数，
 * 调用方可以对它断言（tests/sourceGrouping.test.mjs 钉住「一个都不丢」）。
 * 万一后端将来多出一种 source_type，它会落进第三组并写明非官方，而不是消失。
 */
export const SOURCE_GROUPS = [
  {
    key: 'tender',
    title: '采购公告与合同',
    note: '政府或采购单位发出的公告与合同文件。成交价的依据只可能来自这一类来源。',
    match: source => source.source_type === 'tender_document',
  },
  {
    key: 'manufacturer',
    title: '厂家资料',
    note: '厂家官网或厂家资料，用于核对型号与标称参数；它不是成交记录。',
    match: source => ['manufacturer_website', 'manufacturer_download'].includes(source.source_type),
  },
  {
    key: 'trade',
    title: '产品页与门户线索（非官方来源）',
    note: '仪器门户、商家页面或转载。只能当线索：既不是厂家确认，也不能作为成交价。',
    match: source => !['tender_document', 'manufacturer_website', 'manufacturer_download'].includes(source.source_type),
  },
]

/**
 * 后端给了权威身份（``authority``）之后的分组：官方公告、官方渠道与发布机构、
 * 厂家资料，其余落最后一组。三组规则同样覆盖全部来源，一个都不丢。
 *
 * 第一、二组都以 ``official_channel`` 为准，区别只在"这份文件本身是不是公告"：
 * 成交价的依据只可能来自第一类。机构官网（高校实验室、政府采购平台首页）是
 * 发布方自己的站点，与厂家官网同级——它们是"谁在说话"的证据，不是成交记录。
 */
export const AUTHORITY_GROUPS = [
  {
    key: 'tender',
    title: '采购公告与合同',
    note: '政府或采购单位发出的公告与合同文件。成交价的依据只可能来自这一类来源。',
    match: source => source.authority === 'official_channel' && source.source_type === 'tender_document',
  },
  {
    key: 'official',
    title: '官方渠道与发布机构',
    note: '政府采购平台与高校、医院、研究机构的自有站点。页面由发布机构自己发出。',
    match: source => source.authority === 'official_channel' && source.source_type !== 'tender_document',
  },
  {
    key: 'manufacturer',
    title: '厂家资料',
    note: '厂家官网或厂家资料，用于核对型号与标称参数；它不是成交记录。',
    match: source => source.authority === 'manufacturer',
  },
  {
    key: 'trade',
    title: '产品页与门户线索（非官方来源）',
    note: '仪器门户、商家页面或转载。只能当线索：既不是厂家确认，也不能作为成交价。',
    match: source => !['official_channel', 'manufacturer'].includes(source.authority),
  },
]

export function groupSources(sources) {
  const list = sources || []
  // 后端给了权威身份时按**权威身份**分组，不再按 source_type 猜。
  //
  // 为什么必须换判据：真实 run 里 ``source_type`` 只有两个值
  // （``task-<run-6>``：``tender_document`` 17 条、``unknown`` 49 条），
  // 厂家官网、高校实验室、政府采购平台全是 ``unknown``。按 source_type 分的结果会是
  // 「厂家资料 0 条」把 27 个高校/厂家站点塞进「产品页与门户线索（非官方来源）」——
  // 组的标题就与内容相反了（用户口径是"前端只留权威来源"，这一组却把权威来源
  // 说成非官方）。所以有 ``authority`` 时用它分组，没有时保留原来的 source_type
  // 分组（其他调用方与既有契约测试都按老口径读）。
  const byAuthority = list.length > 0 && list.every(source => source.authority)
  if (byAuthority) {
    const groups = AUTHORITY_GROUPS.map(group => ({...group, rows: list.filter(group.match)}))
    return {groups, classified: groups.reduce((total, group) => total + group.rows.length, 0), total: list.length}
  }
  const groups = SOURCE_GROUPS.map(group => ({ ...group, rows: list.filter(group.match) }))
  return { groups, classified: groups.reduce((total, group) => total + group.rows.length, 0), total: list.length }
}
