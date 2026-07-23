import { ref, reactive, watch, onMounted, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import type { ResultInsights, ResultItem } from '@/types/result.d.ts'
import * as resultsApi from '@/api/results'
import type {
  DecisionSummary,
  DecisionView,
  GetResultContentParams,
} from '@/api/results'
import { useWebSocket } from '@/composables/useWebSocket'
import * as tasksApi from '@/api/tasks'

const LEGACY_RESULT_ESCAPE_PREFIX = '__legacy__'

export function useResults() {
  const { t } = useI18n()
  const route = useRoute()
  // State
  const files = ref<string[]>([])
  const selectedFile = ref<string | null>(null)
  const results = ref<ResultItem[]>([])
  const insights = ref<ResultInsights | null>(null)
  const totalItems = ref(0)
  const currentViewCount = ref(0)
  const decisionSummary = ref<DecisionSummary | null>(null)
  const decisionView = ref<DecisionView>('worth_viewing')
  const page = ref(1)
  const limit = ref(100)
  const blacklistKeywords = ref<string[]>([])
  const taskNameByKeyword = ref<Record<string, string>>({})
  const taskNameById = ref<Record<number, string>>({})
  const isFileOptionsReady = ref(false)
  const hasFetchedFiles = ref(false)
  const hasFetchedTasks = ref(false)
  const isSavingBlacklist = ref(false)
  const readyDelayMs = 200
  let readyTimer: ReturnType<typeof setTimeout> | null = null
  let resultsRequestSequence = 0
  
  const STORAGE_KEY_FILTERS = 'resultFilters'

  function loadPersistedFilters(): Required<Omit<GetResultContentParams, 'page' | 'limit'>> {
    const defaults: Required<Omit<GetResultContentParams, 'page' | 'limit'>> = {
      recommended_only: false,
      ai_recommended_only: false,
      keyword_recommended_only: false,
      include_hidden: false,
      sort_by: 'crawl_time',
      sort_order: 'desc',
    }
    try {
      const saved = localStorage.getItem(STORAGE_KEY_FILTERS)
      if (saved) return { ...defaults, ...JSON.parse(saved) }
    } catch { /* ignore */ }
    return defaults
  }

  const filters = reactive<Required<Omit<GetResultContentParams, 'page' | 'limit'>>>(loadPersistedFilters())

  const isLoading = ref(false)
  const error = ref<Error | null>(null)
  const { on } = useWebSocket()

  function normalizeKeyword(value: string) {
    return value.trim().toLowerCase().replace(/\s+/g, '_')
  }

  function decodeEscapedLegacyKeyword(filename: string) {
    const suffix = '_full_data.jsonl'
    const stem = filename.endsWith(suffix) ? filename.slice(0, -suffix.length) : filename
    if (!stem.startsWith(LEGACY_RESULT_ESCAPE_PREFIX)) return null

    const encoded = stem.slice(LEGACY_RESULT_ESCAPE_PREFIX.length)
    if (encoded.length === 0 || encoded.length % 2 !== 0 || !/^[0-9a-f]+$/.test(encoded)) {
      return null
    }
    try {
      const bytes = new Uint8Array(
        encoded.match(/.{2}/g)?.map((value) => Number.parseInt(value, 16)) || []
      )
      return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
    } catch {
      return null
    }
  }

  function getKeywordFromFilename(filename: string) {
    const decodedKeyword = decodeEscapedLegacyKeyword(filename)
    const fallback = filename.replace(/_full_data\.jsonl$/i, '')
    return normalizeKeyword(decodedKeyword ?? fallback)
  }

  function getTaskIdFromFilename(filename: string) {
    const match = /^task_(0|[1-9]\d*)_full_data\.jsonl$/i.exec(filename)
    return match ? Number(match[1]) : null
  }

  const selectedTaskId = computed(() => (
    selectedFile.value ? getTaskIdFromFilename(selectedFile.value) : null
  ))
  const isTaskOwnedResult = computed(() => selectedTaskId.value !== null)

  // Methods
  async function fetchFiles() {
    try {
      const fileList = await resultsApi.getResultFiles()
      files.value = fileList
      // If a file is selected that no longer exists, reset it.
      // Otherwise, if nothing is selected, select the first file by default.
      if (selectedFile.value && fileList.includes(selectedFile.value)) {
        return
      }

      const lastSelected = localStorage.getItem('lastSelectedResultFile')
      if (lastSelected && fileList.includes(lastSelected)) {
        selectedFile.value = lastSelected
        return
      }

      selectedFile.value = fileList[0] || null
    } catch (e) {
      if (e instanceof Error) error.value = e
    } finally {
      hasFetchedFiles.value = true
      scheduleFileOptionsReady()
    }
  }

  async function fetchResults() {
    const requestSequence = ++resultsRequestSequence
    if (!selectedFile.value) {
      results.value = []
      totalItems.value = 0
      currentViewCount.value = 0
      decisionSummary.value = null
      isLoading.value = false
      return
    }

    isLoading.value = true
    error.value = null
    try {
      const taskId = selectedTaskId.value
      if (taskId !== null) {
        const data = await resultsApi.getTaskResultContent(taskId, decisionView.value, {
          include_hidden: filters.include_hidden,
          page: page.value,
          limit: limit.value,
        })
        if (requestSequence !== resultsRequestSequence) return
        results.value = data.items
        totalItems.value = data.total_items
        currentViewCount.value = data.current_view_count
        decisionSummary.value = data.decision_summary
      } else {
        const data = await resultsApi.getResultContent(selectedFile.value, {
          ...filters,
          page: page.value,
          limit: limit.value,
        })
        if (requestSequence !== resultsRequestSequence) return
        results.value = data.items
        totalItems.value = data.total_items
        currentViewCount.value = data.total_items
        decisionSummary.value = null
      }
    } catch (e) {
      if (requestSequence !== resultsRequestSequence) return
      if (e instanceof Error) error.value = e
      results.value = []
      totalItems.value = 0
      currentViewCount.value = 0
      decisionSummary.value = null
    } finally {
      if (requestSequence === resultsRequestSequence) {
        isLoading.value = false
      }
    }
  }

  async function fetchInsights() {
    if (!selectedFile.value) {
      insights.value = null
      return
    }

    try {
      insights.value = await resultsApi.getResultInsights(selectedFile.value)
    } catch (e) {
      if (e instanceof Error) error.value = e
      insights.value = null
    }
  }

  async function fetchBlacklistRules() {
    if (!selectedFile.value) {
      blacklistKeywords.value = []
      return
    }

    try {
      const data = await resultsApi.getResultBlacklistRules(selectedFile.value)
      blacklistKeywords.value = data.keywords || []
    } catch (e) {
      if (e instanceof Error) error.value = e
      blacklistKeywords.value = []
    }
  }

  async function fetchTaskNameMap() {
    try {
      const tasks = await tasksApi.getAllTasks()
      const mapping: Record<string, string> = {}
      const idMapping: Record<number, string> = {}
      tasks.forEach((task) => {
        if (task.keyword) {
          mapping[normalizeKeyword(task.keyword)] = task.task_name
        }
        if (task.id !== null && task.id !== undefined) {
          idMapping[task.id] = task.task_name
        }
      })
      taskNameByKeyword.value = mapping
      taskNameById.value = idMapping
    } catch (e) {
      if (e instanceof Error) error.value = e
    } finally {
      hasFetchedTasks.value = true
      scheduleFileOptionsReady()
    }
  }

  function scheduleFileOptionsReady() {
    if (isFileOptionsReady.value || !hasFetchedFiles.value || !hasFetchedTasks.value) return
    if (readyTimer) return
    readyTimer = setTimeout(() => {
      isFileOptionsReady.value = true
      readyTimer = null
    }, readyDelayMs)
  }

  // Real-time updates
  on('results_updated', async () => {
    const oldFile = selectedFile.value
    await fetchFiles()
    // If the selected file remains the same, refresh its content (in case of append)
    // If it changed (e.g. from null to new file), the watcher will handle it.
    if (selectedFile.value && selectedFile.value === oldFile) {
      fetchResults()
      fetchInsights()
    }
  })

  on('tasks_updated', () => {
    fetchTaskNameMap()
  })

  async function refreshResults() {
    const current = selectedFile.value
    await fetchFiles()
    if (selectedFile.value && selectedFile.value === current) {
      await fetchResults()
      await fetchInsights()
      await fetchBlacklistRules()
    }
  }

  function exportSelectedResults() {
    if (!selectedFile.value) return
    resultsApi.downloadResultExport(selectedFile.value, { ...filters })
  }

  async function deleteSelectedFile(filename?: string) {
    const target = filename || selectedFile.value
    if (!target) return
    isLoading.value = true
    error.value = null
    try {
      await resultsApi.deleteResultFile(target)
      if (selectedFile.value === target) {
        const lastSelected = localStorage.getItem('lastSelectedResultFile')
        if (lastSelected === target) {
          localStorage.removeItem('lastSelectedResultFile')
        }
      }
      await fetchFiles()
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function toggleItemBlock(item: ResultItem) {
    if (!selectedFile.value) return
    const itemId = item.商品信息?.商品ID
    if (!itemId) return
    const newStatus = item._status === 'hidden' ? 'active' : 'hidden'
    try {
      await resultsApi.updateItemStatus(selectedFile.value, itemId, newStatus)
      await fetchResults()
    } catch (e) {
      if (e instanceof Error) error.value = e
    }
  }

  async function saveBlacklistRules(keywords: string[]) {
    if (!selectedFile.value) return
    isSavingBlacklist.value = true
    error.value = null
    try {
      const data = await resultsApi.updateResultBlacklistRules(selectedFile.value, keywords)
      blacklistKeywords.value = data.keywords || []
      await fetchResults()
      await fetchInsights()
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isSavingBlacklist.value = false
    }
  }

  // Watchers
  watch(filters, (val) => {
    localStorage.setItem(STORAGE_KEY_FILTERS, JSON.stringify(val))
  }, { deep: true })
  watch([selectedFile, filters, decisionView], fetchResults, { deep: true })
  watch(selectedFile, () => {
    decisionView.value = 'worth_viewing'
    fetchInsights()
    fetchBlacklistRules()
  })
  watch(selectedFile, (value) => {
    if (value) localStorage.setItem('lastSelectedResultFile', value)
  })
  watch(
    [() => route.query.file, files],
    ([routeFile, currentFiles]) => {
      if (typeof routeFile !== 'string') return
      if (currentFiles.includes(routeFile)) {
        selectedFile.value = routeFile
      }
    },
    { immediate: true }
  )

  const fileOptions = computed(() =>
    files.value.map((file) => {
      const keyword = getKeywordFromFilename(file)
      const escapedLegacyKeyword = decodeEscapedLegacyKeyword(file)
      const taskId = getTaskIdFromFilename(file)
      const taskName = taskId !== null
        ? taskNameById.value[taskId]
        : escapedLegacyKeyword ?? taskNameByKeyword.value[keyword]
      const label = escapedLegacyKeyword !== null
        ? t('results.filters.legacyTaskNameLabel', { keyword: escapedLegacyKeyword })
        : t('results.filters.taskNameLabel', {
            task: taskName || t('common.unnamed'),
          })
      return {
        value: file,
        taskName: taskName || t('common.unnamed'),
        label,
      }
    })
  )

  // Lifecycle
  onMounted(() => {
    fetchFiles()
    fetchTaskNameMap()
  })

  return {
    files,
    selectedFile,
    results,
    insights,
    totalItems,
    currentViewCount,
    decisionSummary,
    decisionView,
    isTaskOwnedResult,
    filters,
    isLoading,
    error,
    fetchFiles, // Expose to allow manual refresh
    refreshResults,
    exportSelectedResults,
    deleteSelectedFile,
    toggleItemBlock,
    blacklistKeywords,
    isSavingBlacklist,
    saveBlacklistRules,
    fileOptions,
    isFileOptionsReady,
  }
}
