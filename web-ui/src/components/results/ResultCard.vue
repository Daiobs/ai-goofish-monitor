<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ResultItem } from '@/types/result.d.ts'
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import Badge from '@/components/ui/badge/Badge.vue'
import { ExternalLink, TrendingUp, TrendingDown, Info, User, Clock, CheckCircle2, XCircle, AlertCircle, EyeOff, Eye } from 'lucide-vue-next'
import { formatDateTime } from '@/i18n'

interface Props {
  item: ResultItem
}

const props = defineProps<Props>()
const emit = defineEmits<{
  (e: 'toggle-block', item: ResultItem): void
}>()
const { t } = useI18n()

const info = props.item.商品信息
const seller = props.item.卖家信息
const ai = props.item.ai_analysis
const priceInsight = props.item.price_insight
const usesAiMarketScope = computed(() => priceInsight?.market_scope === 'ai_classified')

const analysisStatus = computed(() => {
  if (ai?.analysis_status) return ai.analysis_status
  if (ai?.error) return 'failed'
  if (typeof ai?.is_recommended === 'boolean') return 'completed'
  return 'pending'
})
const isAiFailure = computed(() => analysisStatus.value === 'failed')
const isRecommended = computed(() => (
  analysisStatus.value === 'completed' && ai?.is_recommended === true
))
const recommendationStatus = computed(() => {
  if (isAiFailure.value) return { label: t('results.card.aiFailed'), color: 'bg-rose-500', icon: AlertCircle, text: 'text-rose-600', bg: 'bg-rose-50' }
  if (analysisStatus.value === 'skipped') return { label: t('results.card.aiSkipped'), color: 'bg-slate-400', icon: AlertCircle, text: 'text-slate-600', bg: 'bg-slate-50' }
  if (analysisStatus.value === 'pending') return { label: t('results.card.aiPending'), color: 'bg-amber-500', icon: AlertCircle, text: 'text-amber-600', bg: 'bg-amber-50' }
  if (ai?.is_recommended === true) return { label: t('results.card.strongRecommend'), color: 'bg-emerald-500', icon: CheckCircle2, text: 'text-emerald-600', bg: 'bg-emerald-50' }
  if (ai?.is_recommended === false) return { label: t('results.card.notRecommended'), color: 'bg-rose-500', icon: XCircle, text: 'text-rose-600', bg: 'bg-rose-50' }
  return { label: t('results.card.pending'), color: 'bg-amber-500', icon: AlertCircle, text: 'text-amber-600', bg: 'bg-amber-50' }
})
const analysisStatusLabel = computed(() => {
  if (analysisStatus.value === 'completed') return t('results.card.aiCompleted')
  if (analysisStatus.value === 'failed') return t('results.card.aiFailed')
  if (analysisStatus.value === 'skipped') return t('results.card.aiSkipped')
  return t('results.card.aiPending')
})
const targetCategoryLabel = computed(() => {
  if (ai?.target_category === 'target_only') return t('results.card.targetOnly')
  if (ai?.target_category === 'target_bundle') return t('results.card.targetBundle')
  if (ai?.target_category === 'not_target') return t('results.card.notTarget')
  if (ai?.target_category === 'uncertain') return t('results.card.targetUncertain')
  return t('results.card.targetUnclassified')
})
const exclusionReason = computed(() => {
  if (analysisStatus.value !== 'completed') return t('results.card.aiIssueExclusion')
  if (ai?.target_category === 'target_bundle') return t('results.card.bundleExclusion')
  if (ai?.target_category === 'not_target') return t('results.card.nonTargetExclusion')
  if (ai?.target_category === 'uncertain') return t('results.card.uncertainExclusion')
  if (ai?.market_comparable === false) return t('results.card.nonComparableExclusion')
  return null
})
const marketPositionLabel = computed(() => {
  if (
    ai?.target_category !== 'target_only'
    || ai?.market_comparable !== true
    || priceInsight?.market_comparable === false
  ) {
    return t('results.card.excludedMarketPosition')
  }

  const currentPrice = priceInsight?.current_price
  const medianPrice = priceInsight?.market_median_price
  if (
    typeof currentPrice !== 'number'
    || typeof medianPrice !== 'number'
    || medianPrice <= 0
  ) {
    return t('results.card.marketPositionUnavailable')
  }

  const percent = Math.round(Math.abs((currentPrice - medianPrice) / medianPrice) * 100)
  if (percent === 0) return t('results.card.atMedian')
  return currentPrice < medianPrice
    ? t('results.card.belowMedian', { percent })
    : t('results.card.aboveMedian', { percent })
})

const imageUrl = info.商品图片列表?.[0] || info.商品主图链接 || ''
const crawlTime = props.item.爬取时间
  ? formatDateTime(props.item.爬取时间, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  : t('common.unknown')
const matchScore = computed(() => (
  typeof ai?.value_score === 'number' ? ai.value_score : null
))
const isHidden = computed(() => props.item._effective_hidden === true || props.item._status === 'hidden')
const isRuleHidden = computed(() => props.item._hidden_reason === 'rule')
const canToggleBlock = computed(() => props.item._hidden_reason !== 'rule' && props.item._hidden_reason !== 'expired')
const hiddenLabel = computed(() => {
  if (props.item._hidden_reason === 'rule') return t('results.card.blacklisted')
  if (props.item._hidden_reason === 'expired') return t('results.card.expired')
  return t('results.card.hidden')
})

const expanded = ref(false)
</script>

<template>
  <Card class="group flex flex-col h-full border-none shadow-glass hover:shadow-card-hover transition-all duration-300 rounded-2xl overflow-hidden bg-white/80 backdrop-blur-sm" :class="{ 'opacity-50': isHidden }">
    <!-- Image Header -->
    <div class="relative aspect-[4/3] overflow-hidden">
      <div class="absolute inset-0 bg-slate-200 animate-pulse" v-if="!imageUrl"></div>
      <img
        v-else
        :src="imageUrl"
        :alt="info.商品标题"
        class="w-full h-full object-cover transition-transform duration-500 group-hover:scale-110"
        loading="lazy"
      />
      <!-- Hidden overlay -->
      <div v-if="isHidden" class="absolute inset-0 bg-black/30 flex items-center justify-center">
        <span class="text-white/80 text-xs font-semibold uppercase tracking-wider">{{ hiddenLabel }}</span>
      </div>
      <!-- Overlays -->
      <div class="absolute top-3 left-3 flex gap-2">
        <Badge v-if="isRecommended && !isHidden" variant="default" class="bg-emerald-500/90 backdrop-blur-md border-none shadow-sm">
          {{ t('results.card.curated') }}
        </Badge>
        <Badge v-if="isRuleHidden" variant="secondary" class="bg-slate-900/75 text-white border-none backdrop-blur-md shadow-sm">
          {{ t('results.card.blacklisted') }}
        </Badge>
      </div>
      <div class="absolute top-3 right-3 flex gap-1.5">
        <button
          v-if="canToggleBlock"
          type="button"
          @click="emit('toggle-block', props.item)"
          :aria-label="isHidden ? t('results.card.unblock') : t('results.card.block')"
          class="flex rounded-full bg-white/30 p-1.5 text-white backdrop-blur-md border border-white/40 shadow-sm opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100 hover:bg-white/50"
        >
          <EyeOff v-if="!isHidden" class="w-4 h-4" />
          <Eye v-else class="w-4 h-4" />
        </button>
         <a
           :href="info.商品链接"
           target="_blank"
           rel="noopener noreferrer"
           :aria-label="t('results.card.detail')"
           class="flex rounded-full bg-white/30 p-1.5 text-white backdrop-blur-md border border-white/40 shadow-sm opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
         >
            <ExternalLink class="w-4 h-4" />
         </a>
      </div>
    </div>

    <CardHeader class="p-4 pb-2">
      <div class="flex justify-between items-start gap-3">
        <CardTitle class="h-10 min-w-0 flex-grow break-words text-base font-semibold leading-snug text-slate-800 line-clamp-2">
          <a :href="info.商品链接" target="_blank" rel="noopener noreferrer" class="hover:text-primary transition-colors">
            {{ info.商品标题 }}
          </a>
        </CardTitle>
      </div>
      <div class="flex items-baseline gap-1 mt-2">
        <span class="break-all text-2xl font-bold text-rose-600 tracking-tight">{{ info.当前售价 }}</span>
        <span v-if="info['商品原价']" class="text-xs text-slate-400 line-through mb-1">{{ info['商品原价'] }}</span>
      </div>
    </CardHeader>

    <CardContent class="p-4 pt-2 flex-grow">
      <!-- AI Insight Section -->
      <div class="rounded-xl p-3 border border-slate-100" :class="recommendationStatus.bg">
        <div class="flex items-center justify-between mb-2">
          <div class="flex items-center gap-2">
            <component :is="recommendationStatus.icon" class="w-4 h-4" :class="recommendationStatus.text" />
            <span class="text-sm font-bold" :class="recommendationStatus.text">{{ recommendationStatus.label }}</span>
          </div>
          <div class="flex min-w-0 items-center gap-1">
             <span class="truncate text-[10px] font-medium text-slate-400">
               {{ t('results.card.aiStatus') }}: {{ analysisStatusLabel }}
             </span>
             <span v-if="matchScore !== null" class="shrink-0 text-sm font-black" :class="recommendationStatus.text">{{ matchScore }}%</span>
          </div>
        </div>
        <p class="mb-2 text-[11px] font-semibold text-slate-500">
          {{ targetCategoryLabel }}
          <span> · {{ ai?.market_comparable === true ? t('results.card.comparable') : t('results.card.notComparable') }}</span>
        </p>
        
        <div v-if="matchScore !== null && !isAiFailure" class="w-full h-1.5 bg-white/50 rounded-full overflow-hidden mb-3">
          <div 
            class="h-full transition-all duration-1000 ease-out rounded-full" 
            :class="recommendationStatus.color"
            :style="{ width: `${matchScore}%` }"
          ></div>
        </div>

        <p class="mb-1 text-[10px] font-semibold text-slate-400">
          {{ t('results.card.analysisReason') }}
        </p>
        <p class="text-xs leading-relaxed text-slate-600" :class="{ 'line-clamp-2': !expanded }">
           {{ ai?.reason || t('results.card.analyzing') }}
        </p>
        
        <button
          type="button"
          v-if="ai?.reason && ai.reason.length > 50"
          @click="expanded = !expanded" 
          class="mt-1 text-[10px] font-bold uppercase text-primary/70 hover:text-primary transition-colors flex items-center gap-1"
        >
          {{ expanded ? t('results.card.collapse') : t('results.card.expand') }}
          <Info class="w-3 h-3" />
        </button>
      </div>

      <div v-if="exclusionReason" class="mt-3 border-l-2 border-amber-300 bg-amber-50/70 px-3 py-2">
        <p class="text-[10px] font-semibold text-amber-700">
          {{ t('results.card.exclusionReason') }}
        </p>
        <p class="mt-0.5 text-xs leading-5 text-amber-900">
          {{ exclusionReason }}
        </p>
      </div>

      <div class="mt-3 border-t border-slate-100 pt-3">
        <p class="text-[10px] font-semibold text-slate-400">
          {{ t('results.card.marketPosition') }}
        </p>
        <p class="mt-0.5 text-xs font-medium leading-5 text-slate-700">
          {{ marketPositionLabel }}
        </p>
      </div>

      <!-- Price Stats Grid -->
      <div v-if="priceInsight?.observation_count" class="mt-4 grid grid-cols-2 gap-3">
        <div class="bg-slate-50/50 p-2.5 rounded-xl border border-slate-100/50 group/stat">
          <div class="flex items-center gap-1.5 text-[10px] font-medium text-slate-400 mb-1">
            <TrendingUp class="w-3 h-3" />
            {{
              t(
                usesAiMarketScope
                  ? 'results.card.marketAvg'
                  : 'results.card.keywordMarketAvg',
              )
            }}
          </div>
          <div class="text-sm font-bold text-slate-700">
            {{
              priceInsight.market_comparable === false
                ? t('results.card.excludedFromMarket')
                : (priceInsight.market_avg_price ? `¥${priceInsight.market_avg_price}` : '—')
            }}
          </div>
        </div>
        <div class="bg-slate-50/50 p-2.5 rounded-xl border border-slate-100/50">
          <div class="flex items-center gap-1.5 text-[10px] font-medium text-slate-400 mb-1">
            <TrendingDown class="w-3 h-3" /> {{ t('results.card.historicalLow') }}
          </div>
          <div class="text-sm font-bold text-slate-700">
            {{ priceInsight.min_price ? `¥${priceInsight.min_price}` : '—' }}
          </div>
        </div>
      </div>
    </CardContent>

    <CardFooter class="px-4 py-3 bg-slate-50/30 border-t border-slate-100/60 flex items-center justify-between text-[10px]">
      <div class="flex items-center gap-3 text-slate-400">
        <div class="flex items-center gap-1">
          <User class="w-3 h-3" />
          <span class="truncate max-w-[60px]">{{ seller.卖家昵称 || info.卖家昵称 || t('results.card.anonymous') }}</span>
        </div>
        <div class="flex items-center gap-1">
          <Clock class="w-3 h-3" />
          <span>{{ crawlTime }}</span>
        </div>
      </div>
      <a :href="info.商品链接" target="_blank" rel="noopener noreferrer" class="flex items-center gap-1 text-primary font-bold hover:gap-1.5 transition-all">
        {{ t('results.card.detail') }} <ExternalLink class="w-3 h-3" />
      </a>
    </CardFooter>
  </Card>
</template>
