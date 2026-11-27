/**
 * 拒收记录归位与文案（规范项 归位 + 迭代规范 文案收尾，设计规范）。
 *
 * 真跑背景（``output/e2e-final/runs/merge-audit-hve50/evidence/16-final-run.json``）：
 * 一轮 5 条拒收记录，1 条 ``model`` 是采购目标 ``HVE-50``，4 条是候选 ``LS-50LD``——
 * 而那 4 条的 ``detail`` 写的是「…与**本次目标型号 LS-50LD** 不一致」。
 *
 * 迭代规范 之后这里钉三件事：
 * 1. **归位**判据不变（等于基线 → 成交价；等于候选 → 候选块；都不是 → 单列）；
 * 2. **共同解释**只说一次（``rejectionGroupNote``），逐条 ``detail`` 里不许再出现
 *    "同品类的不同型号不能互相认定成交价。"——实测天平/洁净台那两节各重复 24 次；
 * 3. **型号称呼**只有两种：「本次目标型号」与「候选型号」。原来同一条记录被叫成
 *    "该候选品所在来源的候选型号""该条核价记录的核价型号"（已于 迭代规范 统一）。
 *
 * fixture 逐字照抄后端模板（``backend/services/price_rejections.py`` 的 MODEL_MISMATCH
 * 分支与 ``procurement_research.aggregate_rejections_by_source`` 的聚合句，都是**历史
 * 长句**形状——归一要在展示层做，历史台账改不掉）。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CANDIDATE_OWNER_LABEL,
  REJECTION_REASON_NOTES,
  TARGET_OWNER_LABEL,
  normalizeModel,
  ownerLabelFor,
  partitionRejections,
  rejectionDetail,
  rejectionGroupNote,
  rejectionOwnerLabel,
  rejectionRole,
  rejectionSectionCopy,
} from '../src/services/rejectionPlacement.js'

const BASELINE = 'HVE-50'
const CANDIDATE_MODEL = 'LS-50LD'
const MISMATCH_NOTE = '同品类的不同型号不能互相认定成交价。'

const baselineRecord = {
  url: 'http://www.ccgp.gov.cn/cggg/zygg/zbgg/202510/t20251015_25511028.htm',
  model: BASELINE,
  reason_code: 'brand_mismatch',
  detail: '引文中的品牌是 上海泰，与目标品牌 HIRAYAMA 不一致。',
}

const baselineModelRecord = {
  url: 'http://www.ccgp.gov.cn/cggg/zygg/zbgg/202510/t20251015_25511028.htm',
  model: BASELINE,
  reason_code: 'model_mismatch',
  other_models: ['BSC-1004', 'MF-52M'],
  detail: `引文中的型号是 BSC-1004、MF-52M，与本次目标型号 ${BASELINE} 不一致：${MISMATCH_NOTE}`,
}

const candidateRecord = {
  url: 'https://zfcg.czt.zj.gov.cn/portal/detail?articleId=i7TM7G23K7WoSG7VhkImcA%3D%3D',
  model: CANDIDATE_MODEL,
  reason_code: 'model_mismatch',
  other_models: ['HVE-50', 'ECH2O', 'PTC-300'],
  detail: `引文中的型号是 HVE-50、ECH2O、PTC-300，与本次目标型号 ${CANDIDATE_MODEL} 不一致：${MISMATCH_NOTE}`,
}

const aggregatedCandidateRecord = {
  url: 'https://purchase.lsnu.edu.cn/info/1072/6898.htm',
  model: CANDIDATE_MODEL,
  reason_code: 'model_mismatch',
  aggregated_count: 9,
  detail: `这条来源上共有 9 条金额，引文里的型号（RV3210、UF-200-4、LS-A100P）都与本次目标型号 ${CANDIDATE_MODEL} 不一致：该公告讲的是别的仪器，已按来源聚合为这一条，不再逐条列出。`,
}

const options = { baselineModel: BASELINE, candidateModels: [CANDIDATE_MODEL, 'LS-100LD'] }

test('归位只用记录自带的 model：基线 → 成交价，候选 → 候选块，其余单列', () => {
  const buckets = partitionRejections([
    baselineRecord,
    candidateRecord,
    aggregatedCandidateRecord,
    { url: 'https://example.com/x', model: 'ZZ-999', reason_code: 'model_mismatch', detail: '引文中的型号是 AA-1，与本次目标型号 ZZ-999 不一致。' },
    { url: 'https://example.com/y', reason_code: 'not_award_evidence', detail: '不是成交证据。' },
  ], options)
  assert.equal(buckets.price.length, 1)
  assert.equal(buckets.price[0].model, BASELINE)
  assert.equal(buckets.byModel[normalizeModel(CANDIDATE_MODEL)].length, 2)
  assert.equal(buckets.other.length, 1)
  assert.equal(buckets.unattributed.length, 1)
  const total = buckets.price.length + buckets.other.length + buckets.unattributed.length
    + Object.values(buckets.byModel).reduce((sum, rows) => sum + rows.length, 0)
  assert.equal(total, 5)
})

test('列表里每条只留短信息：引文型号（称呼由行首那一句承载，不在这里重复）', () => {
  assert.equal(rejectionDetail(candidateRecord, { role: 'candidate' }),
    '引文型号：HVE-50、ECH2O、PTC-300。')
  assert.equal(rejectionDetail(baselineModelRecord, { role: 'price' }),
    '引文型号：BSC-1004、MF-52M。')
  // 行首那一句才是型号称呼的出口：每条一次，不重复。
  assert.equal(rejectionOwnerLabel(candidateRecord, { role: 'candidate' }), `候选型号 ${CANDIDATE_MODEL}`)
  for (const word of [TARGET_OWNER_LABEL, CANDIDATE_OWNER_LABEL]) {
    assert.ok(!rejectionDetail(candidateRecord, { role: 'candidate' }).includes(word),
      `detail 里又写了一遍型号称呼：${word}`)
  }
})

test('共同解释不再逐条重复：detail 里一次都不出现，只由分组说明说一次', () => {
  for (const [record, role] of [[candidateRecord, 'candidate'], [baselineModelRecord, 'price']]) {
    const shown = rejectionDetail(record, { role })
    assert.ok(!shown.includes(MISMATCH_NOTE), `共同解释还留在逐条 detail 里：${shown}`)
    assert.ok(!shown.includes('不一致：'), `长句没有归一：${shown}`)
  }
  // 分组说明才是它的出口，且与后端常量逐字一致。
  assert.equal(REJECTION_REASON_NOTES.model_mismatch, MISMATCH_NOTE)
  assert.equal(rejectionGroupNote('model_mismatch', { role: 'candidate', model: CANDIDATE_MODEL }),
    `引文写的是别的型号，与候选型号 ${CANDIDATE_MODEL} 不一致；${MISMATCH_NOTE}`)
  assert.equal(rejectionGroupNote('model_mismatch', { role: 'price', model: BASELINE }),
    `引文写的是别的型号，与本次目标型号 ${BASELINE} 不一致；${MISMATCH_NOTE}`)
  // 没有登记共同解释的原因不印说明行。
  assert.equal(rejectionGroupNote('brand_mismatch', { role: 'price', model: BASELINE }), '')
})

test('按来源聚合的那一条也归一，且不再说"不再逐条列出"这种口语尾句', () => {
  const shown = rejectionDetail(aggregatedCandidateRecord, { role: 'candidate' })
  assert.equal(shown,
    '这条来源上共有 9 条金额，引文型号：RV3210、UF-200-4、LS-A100P；该公告讲的是别的仪器，已按来源聚合为一条。')
  assert.ok(!shown.includes(MISMATCH_NOTE))
})

test('新台账写下的短句原样透传（归一不再加工）', () => {
  const fresh = { model: CANDIDATE_MODEL, reason_code: 'model_mismatch', detail: '引文型号：FTIR-850。' }
  assert.equal(rejectionDetail(fresh, { role: 'candidate' }), fresh.detail)
  const freshBaseline = { model: BASELINE, reason_code: 'model_mismatch', detail: '引文型号：FTIR-850。' }
  assert.equal(rejectionDetail(freshBaseline, { role: 'price' }), freshBaseline.detail)
  // 其它原因的句子一字不动（这一单只动型号不一致那一类）。
  assert.equal(rejectionDetail(baselineRecord, { role: 'price' }), baselineRecord.detail)
})

test('型号称呼只剩两种：本次目标型号 / 候选型号', () => {
  assert.equal(TARGET_OWNER_LABEL, '本次目标型号')
  assert.equal(CANDIDATE_OWNER_LABEL, '候选型号')
  assert.equal(ownerLabelFor('price'), TARGET_OWNER_LABEL)
  for (const role of ['candidate', 'other', 'unattributed']) {
    assert.equal(ownerLabelFor(role), CANDIDATE_OWNER_LABEL)
  }
  assert.equal(rejectionOwnerLabel(baselineRecord, { role: 'price' }), `本次目标型号 ${BASELINE}`)
  assert.equal(rejectionOwnerLabel(candidateRecord, { role: 'candidate' }), `候选型号 ${CANDIDATE_MODEL}`)
  assert.equal(rejectionOwnerLabel({ model: 'ZZ-999' }, { role: 'other' }), '候选型号 ZZ-999')
  assert.equal(rejectionOwnerLabel({}, { role: 'other' }), '型号未标注')
})

test('模块里不再有那两种旧称呼', async () => {
  const source = await import('node:fs').then(fs => fs.readFileSync(
    new URL('../src/services/rejectionPlacement.js', import.meta.url), 'utf8'))
  assert.ok(!source.includes('该候选品所在来源的候选型号'), '旧称呼还在模块里')
  assert.ok(!source.includes('该条核价记录的核价型号'), '旧称呼还在模块里')
  assert.ok(!source.includes('自己那份'), '口语小标题还在模块里')
})

test('型号写法归一：大小写、空白与连字符不影响归位', () => {
  assert.equal(normalizeModel('hve - 50'), normalizeModel('HVE-50'))
  const buckets = partitionRejections([{ ...candidateRecord, model: 'ls - 50ld' }], options)
  assert.equal(buckets.byModel[normalizeModel(CANDIDATE_MODEL)].length, 1)
  assert.equal(buckets.other.length, 0)
  assert.equal(rejectionRole({ model: 'ls - 50ld' }, options), 'candidate')
})

test('分节标题与小标题去口语，三节各有各的标题', () => {
  const candidateCopy = rejectionSectionCopy('candidate', { model: CANDIDATE_MODEL })
  assert.equal(candidateCopy.title, `该候选品核价时被拒的金额（${CANDIDATE_MODEL}）`)
  assert.ok(!candidateCopy.title.includes('自己那份'))
  assert.match(candidateCopy.lead, /不是本次采购目标的成交价/)
  const priceCopy = rejectionSectionCopy('price')
  assert.match(priceCopy.title, /没有被采用/)
  const otherCopy = rejectionSectionCopy('other')
  assert.equal(otherCopy.title, '其它型号核价时被拒的金额')
  assert.notEqual(otherCopy.title, priceCopy.title)
  for (const copy of [candidateCopy, priceCopy, otherCopy]) {
    for (const word of ['自己那份', '这个候选品自己']) {
      assert.ok(!copy.title.includes(word) && !copy.lead.includes(word), `${word} 还在：${copy.title}`)
    }
  }
})
