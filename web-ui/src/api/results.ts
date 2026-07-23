import type { ResultInsights, ResultItem } from '@/types/result.d.ts'
import { http } from '@/lib/http'

export const DECISION_VIEW_KEYS = [
  'worth_viewing',
  'comparable_targets',
  'bundles',
  'excluded',
  'ai_issues',
] as const

export type DecisionView = typeof DECISION_VIEW_KEYS[number]

export interface DecisionSummary {
  all_count: number;
  target_only_count: number;
  target_bundle_count: number;
  not_target_count: number;
  uncertain_count: number;
  comparable_count: number;
  excluded_count: number;
  ai_recommended_count: number;
  ai_not_recommended_count: number;
  ai_issue_count: number;
}

export interface ResultContentResponse {
  total_items: number;
  page: number;
  limit: number;
  items: ResultItem[];
}

export interface TaskDecisionResultResponse extends ResultContentResponse {
  decision_view: DecisionView;
  current_view_count: number;
  decision_summary: DecisionSummary;
}

export interface GetResultContentParams {
  recommended_only?: boolean;
  ai_recommended_only?: boolean;
  keyword_recommended_only?: boolean;
  include_hidden?: boolean;
  sort_by?: 'crawl_time' | 'publish_time' | 'price' | 'keyword_hit_count';
  sort_order?: 'asc' | 'desc';
  page?: number;
  limit?: number;
}

export async function getResultFiles(): Promise<string[]> {
  const data = await http('/api/results/files')
  return data.files || []
}

export async function deleteResultFile(filename: string): Promise<{ message: string }> {
  return await http(`/api/results/files/${filename}`, { method: 'DELETE' })
}

export async function getResultContent(
  filename: string,
  params: GetResultContentParams = {}
): Promise<ResultContentResponse> {
  return await http(`/api/results/${filename}`, { params: params as Record<string, any> })
}

export async function getTaskResultContent(
  taskId: number,
  decisionView: DecisionView,
  params: Pick<GetResultContentParams, 'include_hidden' | 'page' | 'limit'> = {}
): Promise<TaskDecisionResultResponse> {
  return await http(`/api/results/tasks/${taskId}`, {
    params: {
      decision_view: decisionView,
      ...params,
    },
  })
}

export async function getResultInsights(filename: string): Promise<ResultInsights> {
  return await http(`/api/results/${filename}/insights`)
}

export async function getResultBlacklistRules(filename: string): Promise<{ keywords: string[] }> {
  return await http(`/api/results/${filename}/blacklist-rules`)
}

export async function updateResultBlacklistRules(filename: string, keywords: string[]): Promise<{ message: string; keywords: string[] }> {
  return await http(`/api/results/${filename}/blacklist-rules`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keywords }),
  })
}

export function buildResultExportUrl(filename: string, params: GetResultContentParams = {}): string {
  const searchParams = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null) {
      searchParams.set(key, String(value))
    }
  })
  const queryString = searchParams.toString()
  return `/api/results/${encodeURIComponent(filename)}/export${queryString ? `?${queryString}` : ''}`
}

export function downloadResultExport(filename: string, params: GetResultContentParams = {}) {
  const url = buildResultExportUrl(filename, params)
  const link = document.createElement('a')
  link.href = url
  link.download = ''
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}

export async function updateItemStatus(filename: string, itemId: string, status: string): Promise<{ message: string; status: string }> {
  return await http(`/api/results/${filename}/items/${itemId}/status`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  })
}
