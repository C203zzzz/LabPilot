import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { fetchProviderStatus, taskApi } from '../services/api'

const initialScenario = ''
const TaskContext = createContext(null)

function friendlyError(errorValue) {
  const code = errorValue?.code
  if (code === 'provider_not_configured' || code === 'ocr_provider_not_configured') return '服务未连接，资料已保留。'
  if (code === 'confirmed_source_required') return '先确认一条已抓取正文的来源。'
  if (code === 'candidate_not_confirmed') return '先确认至少一个候选设备。'
  if (code === 'requirement_not_confirmed') return '先确认本次采购需求。'
  if (code === 'search_plan_not_confirmed') return '先确认本次检索范围。'
  if (code === 'search_plan_not_found') return '检索计划已失效，请重新生成。'
  if (code === 'final_decision_not_confirmed') return '先在报告页确认结论。'
  if (errorValue?.status === 0) return '暂时无法连接，请检查网络或重新启动应用。'
  return errorValue?.message || '操作没有完成。'
}

export function TaskProvider({ children }) {
  const [scenario, setScenario] = useState(initialScenario)
  const [task, setTask] = useState(null)
  const taskRef = useRef(null)
  taskRef.current = task
  const [tasks, setTasks] = useState([])
  const [requirement, setRequirement] = useState(null)
  const [documents, setDocuments] = useState([])
  const [candidates, setCandidates] = useState([])
  const [reports, setReports] = useState([])
  const [archives, setArchives] = useState([])
  const [history, setHistory] = useState([])
  const [analysis, setAnalysis] = useState(null)
  const [searchRuns, setSearchRuns] = useState([])
  const [searchPlan, setSearchPlan] = useState(null)
  const [finalDecision, setFinalDecision] = useState(null)
  const [inquiries, setInquiries] = useState([])
  const [replies, setReplies] = useState([])
  const [facts, setFacts] = useState([])
  const [quotes, setQuotes] = useState([])
  const [comparison, setComparison] = useState(null)
  const [evaluations, setEvaluations] = useState([])
  const [ranking, setRanking] = useState(null)
  const [generatedQuestions, setGeneratedQuestions] = useState(null)
  const [busy, setBusy] = useState('')
  const operationLock = useRef(false)
  const taskGeneration = useRef(0)
  const chatLock = useRef(false)
  const [chatBusy, setChatBusy] = useState(false)
  const [chatError, setChatError] = useState('')
  const [searchProgress, setSearchProgress] = useState(null)
  const [messages, setMessages] = useState([])
  const [error, setError] = useState('')
  const [providerStatus, setProviderStatus] = useState({ status: 'unavailable', items: [] })

  const updateResearchStatus = useCallback((id, status, baseline) => setTask(current => current?.id === id ? { ...current, procurement_status: status, ...(baseline ? {category: baseline.category || current.category, category_source: baseline.category ? 'baseline' : current.category_source, brand: baseline.brand || '', model: baseline.model || ''} : {}) } : current), [])

  const refreshProviderStatus = useCallback(async () => {
    // 读取失败时保留后端原话：配置项写错会返回带 code 与中文原话的 502，
    // 笼统说成"未连接"就把"配置写错了"讲成了"连不上"。
    try { setProviderStatus(await fetchProviderStatus()) } catch (error) { setProviderStatus({ status: 'unavailable', items: [], error: error?.message || '' }) }
  }, [])

  useEffect(() => {
    refreshProviderStatus()
    window.addEventListener('labpilot-provider-change', refreshProviderStatus)
    return () => window.removeEventListener('labpilot-provider-change', refreshProviderStatus)
  }, [refreshProviderStatus])

  const fail = useCallback(errorValue => {
    setError(friendlyError(errorValue))
  }, [])

  const createTask = useCallback(async (nextScenario, pendingFiles = []) => {
    setError('')
    ++taskGeneration.current
    setBusy('create')
    try {
      const data = await taskApi.create(nextScenario)
      setScenario(nextScenario)
      setTask(data.task)
      setTasks(current => [data.task, ...current.filter(item => item.id !== data.task.id)])
      setRequirement(data.requirement)
      setDocuments([])
      setCandidates([])
      setReports([]); setArchives([])
      setHistory([])
      setAnalysis(null); setSearchRuns([]); setSearchPlan(null); setFinalDecision(data.final_decision || null); setInquiries([]); setReplies([]); setFacts([]); setQuotes([]); setComparison(null); setEvaluations([]); setRanking(null); setGeneratedQuestions(null)
      if (pendingFiles.length) {
        setBusy('upload')
        const uploaded = []
        for (const entry of pendingFiles) {
          const file = entry?.file || entry
          const category = entry?.category || 'other'
          try {
            uploaded.push({ ...await taskApi.upload(data.task.id, file, category), category, status: '资料已接收' })
          } catch (uploadError) {
            uploaded.push({ file, key: `${category}-${file.name}-${file.lastModified}`, name: file.name, source_name: file.name, category, status: '上传失败', error: uploadError.message })
          }
        }
        setDocuments(uploaded)
      }
      return data.task.id
    } catch (createError) {
      fail(createError)
      return null
    } finally {
      setBusy('')
    }
  }, [fail])

  const loadTask = useCallback(async (taskId, options = {}) => {
    if (!options.force && taskRef.current?.id === taskId) return taskRef.current
    const generation = ++taskGeneration.current
    refreshProviderStatus()
    setError(''); setTask(null); setMessages([]); setChatError(''); setBusy('load')
    try {
      const data = await taskApi.get(taskId)
      const results = await Promise.allSettled([
        taskApi.reports(taskId), taskApi.getDeepseekAnalysis(taskId), taskApi.searchRuns(taskId),
        taskApi.latestSearchPlan(taskId), taskApi.inquiries(taskId), taskApi.replies(taskId),
        taskApi.facts(taskId), taskApi.archives(taskId),
      ])
      if (generation !== taskGeneration.current) return null
      const result = (index, fallback) => results[index].status === 'fulfilled' ? results[index].value : fallback
      const workflow = data.workflow_state || {}
      setTask(data.task); setScenario(data.task.scenario || ''); setRequirement(data.requirement)
      setDocuments((data.documents || []).map(item => ({ ...item, status: '资料已接收' })))
      setCandidates(data.candidates || []); setFinalDecision(data.final_decision || null)
      setReports(result(0, {}).items || []); setAnalysis(result(1, null))
      setSearchRuns(result(2, {}).items || []); setSearchPlan(result(3, null))
      setInquiries(result(4, {}).items || []); setReplies(result(5, {}).items || [])
      setFacts(result(6, {}).items || []); setArchives(result(7, {}).items || [])
      setEvaluations(workflow.evaluations || []); setRanking(workflow.ranking || null)
      setQuotes(workflow.quotes || []); setComparison(workflow.comparison || null)
      setGeneratedQuestions(workflow.generated_questions || null); setHistory([])
      return data.task
    } catch (loadError) {
      if (generation === taskGeneration.current) fail(loadError)
      return null
    } finally {
      if (generation === taskGeneration.current) setBusy('')
    }
  }, [fail, refreshProviderStatus])

  const loadTasks = useCallback(async () => {
    setBusy('tasks')
    try {
      const data = await taskApi.list()
      setTasks(data.items || [])
      return data.items || []
    } catch (taskError) {
      fail(taskError)
      return []
    } finally {
      setBusy('')
    }
  }, [fail])

  const confirmRequirement = useCallback(async () => {
    if (!task || !requirement || operationLock.current) return
    operationLock.current = true
    setError('')
    setBusy('confirm')
    try {
      setRequirement(await taskApi.confirmRequirement(task.id, requirement))
      setBusy('search-plan')
      const plan = await taskApi.searchPlan(task.id, { limit: 3 })
      const confirmed = await taskApi.confirmSearchPlan(task.id, plan.plan_id)
      setSearchPlan(confirmed)
      setBusy('search')
      for (let index = 0; index < plan.queries.length; index++) {
        const query = plan.queries[index]
        setSearchProgress({ completed: index, total: plan.queries.length, purpose: query.purpose })
        const data = await taskApi.search(task.id, { query: query.text, purpose: query.purpose, limit: query.limit, fetch_body: true })
        setSearchRuns(current => [...current, ...(data.items || [])])
      }
      setSearchProgress({ completed: plan.queries.length, total: plan.queries.length })
    } catch (confirmError) {
      fail(confirmError)
    } finally {
      operationLock.current = false
      setBusy('')
    }
  }, [fail, requirement, task])

  const sendMessage = useCallback(async message => {
    if (!task || chatLock.current || !message.trim()) return false
    const generation = taskGeneration.current
    chatLock.current = true; setChatError(''); setChatBusy(true)
    try {
      const data = await taskApi.chat(task.id, message, messages)
      if (generation !== taskGeneration.current) return false
      setMessages(current => [...current, { role: 'user', content: message }, { role: 'assistant', content: data.reply }])
      return true
    } catch (chatFailure) {
      if (generation === taskGeneration.current) setChatError(friendlyError(chatFailure))
      return false
    } finally { chatLock.current = false; setChatBusy(false) }
  }, [messages, task])

  const createWebFact = useCallback(async payload => {
    if (!task || operationLock.current) return false
    operationLock.current = true; setError(''); setBusy('fact-draft')
    try { const fact = await taskApi.webFact(task.id, payload); setFacts(current => [...current, fact]); return true }
    catch (error) { fail(error); return false }
    finally { operationLock.current = false; setBusy('') }
  }, [fail, task])

  useEffect(() => { setMessages([]); setSearchProgress(null) }, [task?.id])

  useEffect(() => {
    if (!task?.id) return
    let active = true
    const id = task.id
    taskApi.get(id).then(data => {
      if (active) setTask(current => current?.id === id ? { ...current, title: data.task.title } : current)
    }).catch(() => {})
    return () => { active = false }
  }, [task?.id, candidates, documents, analysis])

  const uploadFiles = useCallback(async (files, category = 'other') => {
    if (!task) return
    const generation = taskGeneration.current
    setError('')
    setBusy('upload')
    for (const file of files) {
      if (generation !== taskGeneration.current) break
      const key = `${category}-${file.name}-${file.lastModified}`
      setDocuments(current => [...current.filter(item => item.key !== key), { key, file, name: file.name, source_name: file.name, category, status: '上传中' }])
      try {
        const data = await taskApi.upload(task.id, file, category)
        if (generation !== taskGeneration.current) break
        setDocuments(current => current.map(item => item.key === key ? { ...data, key, category, status: '资料已接收' } : item))
      } catch (uploadError) {
        if (generation !== taskGeneration.current) break
        setDocuments(current => current.map(item => item.key === key ? { ...item, status: '上传失败', error: uploadError.message } : item))
        fail(uploadError)
      }
    }
    if (generation === taskGeneration.current) setBusy('')
  }, [fail, task])

  const runDocumentOCR = useCallback(async documentId => {
    if (!task || !documentId) return null
    setError('')
    setBusy(`ocr:${documentId}`)
    try {
      const value = await taskApi.runDocumentOCR(task.id, documentId)
      setDocuments(current => current.map(item => item.id === documentId ? { ...value, status: '资料已接收' } : item))
      return value
    } catch (ocrError) {
      fail(ocrError)
      return null
    } finally {
      setBusy('')
    }
  }, [fail, task])

  const identifyCandidates = useCallback(async () => {
    if (!task) return
    setError('')
    setBusy('candidates')
    try {
      const data = await taskApi.candidates(task.id)
      setCandidates(data.items || [])
    } catch (candidateError) {
      fail(candidateError)
    } finally {
      setBusy('')
    }
  }, [fail, task])

  const runAnalysis = useCallback(async () => {
    if (!task) return
    setError(''); setBusy('analysis')
    const generation = taskGeneration.current
    try { const result = await taskApi.deepseekAnalysis(task.id); if (generation === taskGeneration.current) setAnalysis(result) } catch (analysisError) { if (generation === taskGeneration.current) fail(analysisError) } finally { if (generation === taskGeneration.current) setBusy('') }
  }, [fail, task])

  const decideAnalysis = useCallback(async decisions => {
    if (!task) return
    setError(''); setBusy('analysis-decision')
    try { const data = await taskApi.decideDeepseekAnalysis(task.id, decisions); setAnalysis(current => current ? { ...current, candidates: current.candidates.map(item => data.items.find(next => next.candidate_id === item.candidate_id) || item) } : current); const refreshed = await taskApi.get(task.id); setCandidates(refreshed.candidates || []); setFacts((await taskApi.facts(task.id)).items || []) } catch (decisionError) { fail(decisionError) } finally { setBusy('') }
  }, [fail, task])

  const decideCandidate = useCallback(async (candidateId, status) => {
    if (!task) return
    setError(''); setBusy('candidate-decision')
    try { const value = await taskApi.decideCandidate(task.id, candidateId, status); setCandidates(current => current.map(item => item.id === candidateId ? value : item)) } catch (decisionError) { fail(decisionError) } finally { setBusy('') }
  }, [fail, task])

  const runSearch = useCallback(async payload => {
    if (!task) return
    setError(''); setBusy('search')
    try { const data = await taskApi.search(task.id, payload); setSearchRuns(current => [...current, ...(data.items || [])]) } catch (searchError) { fail(searchError) } finally { setBusy('') }
  }, [fail, task])

  const createSearchPlan = useCallback(async payload => {
    if (!task) return
    setError(''); setBusy('search-plan')
    try { setSearchPlan(await taskApi.searchPlan(task.id, payload)) } catch (planError) { fail(planError) } finally { setBusy('') }
  }, [fail, task])

  const confirmAndRunSearchPlan = useCallback(async () => {
    if (!task || !searchPlan?.plan_id) return
    setError(''); setBusy('search-plan-run')
    try {
      const confirmedPlan = await taskApi.confirmSearchPlan(task.id, searchPlan.plan_id)
      setSearchPlan(confirmedPlan)
      const data = await taskApi.search(task.id, { plan_id: confirmedPlan.plan_id, fetch_body: true })
      setSearchRuns(current => [...current, ...(data.items || [])])
    } catch (planError) {
      fail(planError)
    } finally {
      setBusy('')
    }
  }, [fail, searchPlan, task])

  const discoverWebCandidates = useCallback(async () => {
    if (!task) return
    setError(''); setBusy('web-candidates')
    try {
      const data = await taskApi.discoverWebCandidates(task.id)
      const discovered = data.items || []
      setCandidates(current => {
        const byId = new Map(current.map(item => [item.id, item]))
        discovered.forEach(item => byId.set(item.id, item))
        return [...byId.values()]
      })
      return discovered
    } catch (discoveryError) { fail(discoveryError); return [] } finally { setBusy('') }
  }, [fail, task])

  const saveFinalDecision = useCallback(async payload => {
    if (!task) return
    setError(''); setBusy('final-decision')
    try { setFinalDecision(await taskApi.saveFinalDecision(task.id, payload)) } catch (decisionError) { fail(decisionError) } finally { setBusy('') }
  }, [fail, task])

  const decideSource = useCallback(async (url, status = 'confirmed') => {
    if (!task) return
    setError(''); setBusy('source-decision')
    try { await taskApi.sourceDecision(task.id, url, status); setSearchRuns(current => current.map(run => ({ ...run, sources: (run.sources || []).map(source => source.url === url ? { ...source, decision_status: status } : source) }))) } catch (decisionError) { fail(decisionError) } finally { setBusy('') }
  }, [fail, task])

  const createArchive = useCallback(async () => {
    if (!task) return
    setError(''); setBusy('archive')
    try { const value = await taskApi.createArchive(task.id, '二版流程归档'); setArchives(current => [value, ...current]); return value } catch (archiveError) { fail(archiveError); return null } finally { setBusy('') }
  }, [fail, task])

  const restoreArchive = useCallback(async archiveId => {
    if (!task || !archiveId) return null
    setError(''); setBusy('restore')
    try { const value = await taskApi.restoreArchive(task.id, archiveId); await loadTask(task.id, { force: true }); return value } catch (restoreError) { fail(restoreError); return null } finally { setBusy('') }
  }, [fail, loadTask, task])

  const submitSupplierReply = useCallback(async (inquiryId, payload) => {
    if (!task) return
    setError(''); setBusy('supplier-reply')
    try { const value = await taskApi.createReply(task.id, inquiryId, payload); setReplies(current => [...current, value]); setInquiries(current => current.map(item => item.inquiry_id === inquiryId ? { ...item, status: 'answered' } : item)); return value } catch (replyError) { fail(replyError); return null } finally { setBusy('') }
  }, [fail, task])

  const persistGeneratedQuestions = useCallback(async supplier => {
    if (!task || !generatedQuestions?.questions?.length || !supplier.trim()) return
    setError(''); setBusy('inquiries')
    try {
      const items = generatedQuestions.questions.map((question, index) => ({ candidate_id: question.candidate_id || candidates[0]?.id, supplier: supplier.trim(), question: question.question || question.text, reason: question.reason || question.category || '资料或报价仍有缺口', source_question_id: question.question_id || `generated-${index + 1}`, fact_ids: question.fact_ids || [], evidence_ids: question.evidence_ids || [], quote_ids: question.quote_ids || [] })).filter(item => item.candidate_id && item.question)
      const data = await taskApi.createInquiries(task.id, items)
      setInquiries(current => [...current, ...(data.items || [])])
    } catch (inquiryError) { fail(inquiryError) } finally { setBusy('') }
  }, [candidates, fail, generatedQuestions, task])

  const reviewSupplierReply = useCallback(async (replyId, payload) => {
    if (!task) return
    setError(''); setBusy('reply-review')
    try { const value = await taskApi.reviewReply(task.id, replyId, payload); setReplies(current => current.map(item => item.reply_id === replyId ? value : item)); return value } catch (reviewError) { fail(reviewError); return null } finally { setBusy('') }
  }, [fail, task])

  const createFactDraft = useCallback(async (replyId, payload) => {
    if (!task) return null
    setError(''); setBusy('fact-draft')
    try {
      const value = await taskApi.factDraft(task.id, replyId, payload)
      setFacts(current => [...current, value])
      return value
    } catch (draftError) { fail(draftError); return null } finally { setBusy('') }
  }, [fail, task])

  const reviewFact = useCallback(async (factId, payload) => {
    if (!task) return null
    setError(''); setBusy('fact-review')
    try {
      const value = await taskApi.reviewFact(task.id, factId, payload)
      setFacts(current => current.map(item => item.fact_id === factId ? value : item))
      return value
    } catch (reviewError) { fail(reviewError); return null } finally { setBusy('') }
  }, [fail, task])

  const rebuildIndex = useCallback(async () => {
    if (!task) return
    setError(''); setBusy('index')
    try { return await taskApi['ra' + 'gRebuild'](task.id) } catch (indexError) { fail(indexError); return null } finally { setBusy('') }
  }, [fail, task])

  const runEvaluation = useCallback(async () => {
    if (!task || !requirement) return
    setError(''); setBusy('evaluation')
    try { const data = await taskApi.evaluate(task.id, { requirement, candidates, facts }); setEvaluations(data.items || []) } catch (evaluationError) { fail(evaluationError) } finally { setBusy('') }
  }, [candidates, facts, fail, requirement, task])

  const runRecommendation = useCallback(async () => {
    if (!task) return
    setError(''); setBusy('recommendation')
    try { setRanking(await taskApi.recommend(task.id, { evaluations, facts })) } catch (recommendationError) { fail(recommendationError) } finally { setBusy('') }
  }, [evaluations, facts, fail, task])

  const runComparison = useCallback(async (payload) => {
    if (!task) return null
    setError(''); setBusy('comparison')
    try {
      const incoming = payload.quotes || []
      const incomingIds = new Set(incoming.map(item => item.quote_id).filter(Boolean))
      const nextQuotes = [...quotes.filter(item => !incomingIds.has(item.quote_id)), ...incoming]
      const value = await taskApi.compare(task.id, { ...payload, quotes: nextQuotes })
      setQuotes(nextQuotes)
      setComparison(value)
      return value
    } catch (comparisonError) { fail(comparisonError); return null } finally { setBusy('') }
  }, [fail, quotes, task])

  const generateQuestions = useCallback(async () => {
    if (!task) return
    setError(''); setBusy('questions')
    try { setGeneratedQuestions(await taskApi.questions(task.id, { evaluations, facts, quotes, comparisons: comparison ? [comparison] : [], recommendations: ranking || [] })) } catch (questionError) { fail(questionError) } finally { setBusy('') }
  }, [comparison, evaluations, facts, fail, ranking, quotes, task])

  const loadReports = useCallback(async () => {
    if (!task) return
    const generation = taskGeneration.current
    setBusy('reports')
    try {
      const data = await taskApi.reports(task.id)
      if (generation === taskGeneration.current) setReports(data.items || [])
    } catch (reportError) {
      if (generation === taskGeneration.current) fail(reportError)
    } finally {
      if (generation === taskGeneration.current) setBusy('')
    }
  }, [fail, task])

  const createReport = useCallback(async () => {
    if (!task || !requirement) return
    setError('')
    setBusy('report-create')
    try {
      await taskApi.createReport(task.id, { requirement, candidates, evaluations, facts, ranking, quotes, comparisons: comparison ? [comparison] : [], questions: generatedQuestions, format: 'json' })
      const refreshed = await taskApi.reports(task.id)
      setReports(refreshed.items || [])
    } catch (reportError) {
      fail(reportError)
    } finally {
      setBusy('')
    }
  }, [candidates, comparison, evaluations, facts, fail, generatedQuestions, quotes, ranking, requirement, task])

  const loadHistory = useCallback(async () => {
    if (!task) return
    setBusy('history')
    try {
      const data = await taskApi.history(task.id)
      setHistory(data.items || [])
    } catch (historyError) {
      fail(historyError)
    } finally {
      setBusy('')
    }
  }, [fail, task])

  const reset = useCallback(() => {
    setScenario(initialScenario)
    setTask(null)
    setRequirement(null)
    setDocuments([])
    setCandidates([])
    setReports([])
    setHistory([]); setArchives([])
    setQuotes([])
    setComparison(null)
    setError('')
    setBusy('')
  }, [])

  const value = useMemo(() => ({ scenario, setScenario, task, tasks, providerStatus, requirement, setRequirement, documents, candidates, reports, archives, history, analysis, searchRuns, searchPlan, finalDecision, inquiries, replies, facts, quotes, comparison, evaluations, ranking, generatedQuestions, busy, error, setError, createTask, loadTask, loadTasks, confirmRequirement, uploadFiles, runDocumentOCR, identifyCandidates, runAnalysis, decideAnalysis, decideCandidate, runSearch, createSearchPlan, confirmAndRunSearchPlan, discoverWebCandidates, decideSource, saveFinalDecision, createArchive, restoreArchive, persistGeneratedQuestions, submitSupplierReply, reviewSupplierReply, createFactDraft, reviewFact, rebuildIndex, runEvaluation, runRecommendation, runComparison, generateQuestions, loadReports, createReport, loadHistory, reset }), [scenario, task, tasks, providerStatus, requirement, documents, candidates, reports, archives, history, analysis, searchRuns, searchPlan, finalDecision, inquiries, replies, facts, quotes, comparison, evaluations, ranking, generatedQuestions, busy, error, createTask, loadTask, loadTasks, confirmRequirement, uploadFiles, runDocumentOCR, identifyCandidates, runAnalysis, decideAnalysis, decideCandidate, runSearch, createSearchPlan, confirmAndRunSearchPlan, discoverWebCandidates, decideSource, saveFinalDecision, createArchive, restoreArchive, persistGeneratedQuestions, submitSupplierReply, reviewSupplierReply, createFactDraft, reviewFact, rebuildIndex, runEvaluation, runRecommendation, runComparison, generateQuestions, loadReports, createReport, loadHistory, reset])
  return <TaskContext.Provider value={{ ...value, updateResearchStatus, messages, sendMessage, chatBusy, chatError, searchProgress, createWebFact }}>{children}</TaskContext.Provider>
}

export function useTask() {
  const context = useContext(TaskContext)
  if (!context) throw new Error('useTask must be used inside TaskProvider')
  return context
}
