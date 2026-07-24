<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import {
  DECISION_VIEW_KEYS,
  type DecisionSummary,
  type DecisionView,
} from '@/api/results'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'

interface Props {
  modelValue: DecisionView
  summary: DecisionSummary | null
  currentViewCount: number
  isLoading: boolean
}

const props = defineProps<Props>()
const emit = defineEmits<{
  (event: 'update:modelValue', value: DecisionView): void
}>()
const { t } = useI18n()

const summaryItems: Array<{ key: keyof DecisionSummary; label: string }> = [
  { key: 'all_count', label: 'all' },
  { key: 'target_only_count', label: 'targetOnly' },
  { key: 'target_bundle_count', label: 'bundles' },
  { key: 'not_target_count', label: 'notTarget' },
  { key: 'uncertain_count', label: 'uncertain' },
  { key: 'comparable_count', label: 'comparable' },
  { key: 'excluded_count', label: 'excluded' },
  { key: 'ai_recommended_count', label: 'aiRecommended' },
  { key: 'ai_not_recommended_count', label: 'aiNotRecommended' },
  { key: 'ai_issue_count', label: 'aiIssues' },
]

function selectView(value: string | number) {
  if (DECISION_VIEW_KEYS.includes(value as DecisionView)) {
    emit('update:modelValue', value as DecisionView)
  }
}
</script>

<template>
  <section class="mb-6 min-w-0 border-y border-slate-200 bg-white py-4" aria-labelledby="decision-queue-title">
    <div class="flex flex-col gap-1 px-1 sm:flex-row sm:items-end sm:justify-between">
      <div class="min-w-0">
        <h2 id="decision-queue-title" class="text-base font-semibold text-slate-800">
          {{ t('results.decisionQueue.title') }}
        </h2>
        <p class="mt-1 text-sm leading-5 text-slate-500">
          {{ t('results.decisionQueue.description') }}
        </p>
      </div>
      <p class="shrink-0 text-sm font-semibold text-slate-700" aria-live="polite">
        {{ t('results.decisionQueue.currentViewCount', { count: props.currentViewCount }) }}
      </p>
    </div>

    <Tabs
      :model-value="props.modelValue"
      class="mt-4 min-w-0"
      @update:model-value="selectView"
    >
      <div class="overflow-x-auto pb-1">
        <TabsList class="h-auto w-max min-w-full justify-start gap-1 bg-slate-100">
          <TabsTrigger
            v-for="view in DECISION_VIEW_KEYS"
            :key="view"
            :value="view"
            class="shrink-0 px-3 py-2"
          >
            {{ t(`results.decisionQueue.tabs.${view}`) }}
          </TabsTrigger>
        </TabsList>
      </div>
    </Tabs>

    <dl
      v-if="props.summary"
      class="mt-4 grid grid-cols-2 border-t border-slate-200 sm:grid-cols-5"
      :aria-label="t('results.decisionQueue.summaryLabel')"
    >
      <div
        v-for="item in summaryItems"
        :key="item.key"
        class="min-w-0 border-b border-slate-100 px-2 py-2.5 sm:px-3"
      >
        <dt class="truncate text-xs text-slate-500">
          {{ t(`results.decisionQueue.summary.${item.label}`) }}
        </dt>
        <dd class="mt-0.5 text-base font-semibold tabular-nums text-slate-800">
          {{ props.summary[item.key] }}
        </dd>
      </div>
    </dl>

    <p v-else-if="props.isLoading" class="mt-4 text-sm text-slate-500" aria-live="polite">
      {{ t('results.decisionQueue.loadingSummary') }}
    </p>
  </section>
</template>
