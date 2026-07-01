import { useEffect, useState } from 'react';
import { API_BASE } from '../../api/finclaw';
import { MarkdownView } from '../MarkdownView';
import { MemoryDocumentView } from '../MemoryDocumentView';
import './MemoryPanel.css';

type MemoryType = 'profile' | 'playbook' | 'convictions';

interface MemoryMetadata {
  entry_count?: number;
  log_count?: number;
  current_level?: string;
  chapter_count?: number;
  dimension_count?: number;
  active_count?: number;
  watching_count?: number;
  last_updated?: string;
  core_updated_at?: string;
  last_candidate_created_at?: string;
  pending_candidate_count?: number;
  file_size?: number;
}

interface MemoryData {
  success: boolean;
  content: string;
  metadata: MemoryMetadata;
}

interface MemoryCandidate {
  candidate_id: string;
  target: MemoryType;
  content: string;
  evidence?: string;
  confidence?: number;
  operation?: string;
  status?: string;
  reason?: string;
  created_at?: string;
  updated_at?: string;
}

interface MemoryConflict {
  conflict_id: string;
  changed_file: MemoryType;
  changed_content: string;
  conflicts_with?: Array<{ file: MemoryType; content: string; reason?: string }>;
  conflict_type?: string;
  severity?: string;
  llm_reason?: string;
  status?: string;
  reason?: string;
  created_at?: string;
}

interface ResearchRecordSummary {
  record_id: string;
  title: string;
  subject_type?: string;
  updated_at?: string;
  latest_thread_id?: string | null;
  user_goal?: string;
  core_conclusion?: string;
  gap_count?: number;
  file_size?: number;
}

const MEMORY_TYPES: MemoryType[] = ['profile', 'playbook', 'convictions'];

const TITLES: Record<MemoryType, string> = {
  profile: '用户画像',
  playbook: '研究框架',
  convictions: '当前投资判断',
};

const ACCENTS: Record<MemoryType, string> = {
  profile: '#2563eb',
  playbook: '#059669',
  convictions: '#b45309',
};

function Icon({ type }: { type: MemoryType }) {
  return <span className="memory-dot" style={{ background: ACCENTS[type] }} />;
}

function formatCount(type: MemoryType, metadata?: MemoryMetadata): string {
  if (!metadata) return '未加载';
  if (type === 'profile') return `${metadata.current_level ?? 'Level ?'} · LOG ${metadata.log_count ?? 0}/8`;
  if (type === 'playbook') return `${metadata.dimension_count ?? metadata.chapter_count ?? 0} 个维度`;
  return `${metadata.active_count ?? 0} active / ${metadata.watching_count ?? 0} watching`;
}

function formatCandidateAction(operation?: string): string {
  const value = String(operation || 'ADD').toUpperCase();
  if (value === 'UPDATE') return '更新';
  if (value === 'WEAKEN') return '降级观察';
  if (value === 'ARCHIVE' || value === 'CONFLICT') return '归档';
  return '写入';
}

function formatMemoryTime(value?: string | null): string {
  if (!value || value === '未知') return '未知';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatMetadataSummary(type: MemoryType, metadata?: MemoryMetadata): string {
  if (!metadata) return '未加载';
  const pending = metadata.pending_candidate_count ? ` · ${metadata.pending_candidate_count} 待确认` : '';
  const candidate = metadata.last_candidate_created_at ? ` · 候选 ${formatMemoryTime(metadata.last_candidate_created_at)}` : '';
  return `${formatCount(type, metadata)} · 核心 ${formatMemoryTime(metadata.core_updated_at || metadata.last_updated)}${pending}${candidate}`;
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  });
  const contentType = response.headers.get('content-type') ?? '';
  const payload = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof payload === 'object' && payload ? (payload as { detail?: string }).detail : String(payload);
    throw new Error(detail || `请求失败：${response.status}`);
  }
  return payload as T;
}

const MemoryPanel = () => {
  const [expandedCard, setExpandedCard] = useState<MemoryType | null>('profile');
  const [memoryData, setMemoryData] = useState<Record<MemoryType, MemoryData | null>>({
    profile: null,
    playbook: null,
    convictions: null,
  });
  const [indexContent, setIndexContent] = useState('');
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([]);
  const [conflicts, setConflicts] = useState<MemoryConflict[]>([]);
  const [researchRecords, setResearchRecords] = useState<ResearchRecordSummary[]>([]);
  const [editingType, setEditingType] = useState<MemoryType | null>(null);
  const [draftContent, setDraftContent] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [openCandidate, setOpenCandidate] = useState<string | null>(null);
  const [openConflict, setOpenConflict] = useState<string | null>(null);

  useEffect(() => {
    void loadAll();
  }, []);

  async function loadAll(silent = false) {
    if (!silent) setBusy(true);
    setError('');
    try {
      const [profile, playbook, convictions, index, candidatePayload, conflictPayload] = await Promise.all([
        fetchJson<MemoryData>('/api/memory/profile'),
        fetchJson<MemoryData>('/api/memory/playbook'),
        fetchJson<MemoryData>('/api/memory/convictions'),
        fetchJson<{ content: string }>('/api/memory/index'),
        fetchJson<{ candidates: MemoryCandidate[] }>('/api/memory/candidates?status=pending'),
        fetchJson<{ conflicts: MemoryConflict[] }>('/api/memory/conflicts?status=pending'),
      ]);
      const recordsPayload = await fetchJson<{ records: ResearchRecordSummary[] }>('/api/research/records?limit=20');
      setMemoryData({ profile, playbook, convictions });
      setIndexContent(index.content ?? '');
      setCandidates(candidatePayload.candidates ?? []);
      setConflicts(conflictPayload.conflicts ?? []);
      setResearchRecords(recordsPayload.records ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : '记忆系统加载失败');
    } finally {
      if (!silent) setBusy(false);
    }
  }

  function toggleCard(type: MemoryType, expanded: boolean) {
    if (expanded) {
      setExpandedCard(null);
      return;
    }
    setExpandedCard(type);
    void loadAll(true);
  }

  function startEdit(type: MemoryType) {
    if (type === 'profile') return;
    setEditingType(type);
    setDraftContent(memoryData[type]?.content ?? '');
    setMessage('');
    setError('');
  }

  async function saveCore(type: MemoryType) {
    if (type === 'profile') {
      setEditingType(null);
      setError('用户画像由 Agent 自动维护，不支持手动编辑');
      return;
    }
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const result = await fetchJson<{ conflicts?: MemoryConflict[]; message?: string }>(`/api/memory/${type}`, {
        method: 'PUT',
        body: JSON.stringify({ content: draftContent, reason: '用户在右栏手动编辑' }),
      });
      setEditingType(null);
      setMessage((result.conflicts?.length ?? 0) > 0 ? `已保存，但检测到 ${result.conflicts?.length} 条潜在冲突` : (result.message ?? '已保存'));
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败');
    } finally {
      setBusy(false);
    }
  }

  async function approveCandidate(candidateId: string) {
    await mutate(`/api/memory/candidates/${candidateId}/approve`, { method: 'POST' }, '候选已确认');
  }

  async function rejectCandidate(candidateId: string) {
    await mutate(`/api/memory/candidates/${candidateId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason: '用户在右栏拒绝' }),
    }, '候选已拒绝');
  }

  async function resolveConflict(conflictId: string, resolution: string) {
    await mutate(`/api/memory/conflicts/${conflictId}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ resolution, note: '用户在右栏处理冲突' }),
    }, '冲突已处理');
  }

  async function mutate(path: string, init: RequestInit, okMessage: string) {
    setBusy(true);
    setError('');
    setMessage('');
    try {
      await fetchJson(path, init);
      setMessage(okMessage);
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="memory-panel">
      <header className="memory-panel-header">
        <div>
          <h2>长期记忆</h2>
          <p>核心记忆由用户确认，候选与冲突在这里审查。</p>
        </div>
        <button className="memory-ghost-btn" onClick={() => void loadAll()} disabled={busy}>
          刷新
        </button>
      </header>

      {error && <div className="memory-alert memory-alert-error">{error}</div>}
      {message && <div className="memory-alert memory-alert-info">{message}</div>}

      <section className="memory-index-card">
        <div className="memory-section-title">
          <span>记忆索引</span>
          <span>{candidates.length} 待审查 / {conflicts.length} 冲突</span>
        </div>
        <div className="memory-index-content">
          <MarkdownView content={indexContent || '暂无索引'} variant="compact" />
        </div>
      </section>

      <main className="memory-cards">
        {MEMORY_TYPES.map((type) => {
          const data = memoryData[type];
          const expanded = expandedCard === type;
          const editing = editingType === type;
          return (
            <article key={type} className={`memory-card memory-card-${type} ${expanded ? 'expanded' : ''}`}>
              <button className="memory-card-header" onClick={() => toggleCard(type, expanded)}>
                <div className="memory-card-title">
                  <Icon type={type} />
                  <span>{TITLES[type]}</span>
                </div>
                <div className="memory-card-summary">
                  {formatMetadataSummary(type, data?.metadata)}
                </div>
              </button>

              {expanded && (
                <div className="memory-card-content">
                  <div className="memory-card-toolbar">
                    {editing ? (
                      <>
                        <button className="memory-primary-btn" onClick={() => void saveCore(type)} disabled={busy}>保存</button>
                        <button className="memory-ghost-btn" onClick={() => setEditingType(null)} disabled={busy}>取消</button>
                      </>
                    ) : type === 'profile' ? (
                      <span className="memory-readonly-note">只读 · 由 Agent 自动维护</span>
                    ) : (
                      <button className="memory-ghost-btn" onClick={() => startEdit(type)}>编辑核心记忆</button>
                    )}
                  </div>
                  {editing ? (
                    <textarea className="memory-editor" value={draftContent} onChange={(event) => setDraftContent(event.target.value)} />
                  ) : (
                    <div className="memory-markdown">
                      <MemoryDocumentView type={type} content={data?.content || ''} />
                    </div>
                  )}
                </div>
              )}
            </article>
          );
        })}

        <section className="memory-review-card">
          <div className="memory-section-title">
            <span>研究档案</span>
            <span>{researchRecords.length}</span>
          </div>
          {researchRecords.length === 0 ? (
            <div className="memory-empty">暂无研究档案，完成 Research Thread 后会自动沉淀。</div>
          ) : researchRecords.map((record) => (
            <div className="memory-review-item" key={record.record_id}>
              <div className="memory-review-head">
                <span>{record.title}</span>
                <span>{record.updated_at ?? ''}</span>
              </div>
              <p>{record.core_conclusion || record.record_id}</p>
              <div className="memory-review-detail">
                <MarkdownView content={`${record.user_goal || record.record_id}\n\n待验证判断：${record.gap_count ?? 0}`} variant="compact" />
              </div>
            </div>
          ))}
        </section>

        <section className="memory-review-card">
          <div className="memory-section-title">
            <span>候选记忆</span>
            <span>{candidates.length}</span>
          </div>
          {candidates.length === 0 ? (
            <div className="memory-empty">暂无待确认候选</div>
          ) : candidates.map((candidate) => (
            <div className="memory-review-item" key={candidate.candidate_id}>
              <button className="memory-review-head" onClick={() => setOpenCandidate(openCandidate === candidate.candidate_id ? null : candidate.candidate_id)}>
                <span>{formatCandidateAction(candidate.operation)} {TITLES[candidate.target]} · {candidate.operation ?? 'ADD'} · {Math.round((candidate.confidence ?? 0) * 100)}%</span>
                <span>{candidate.created_at ?? ''}</span>
              </button>
              <div className="memory-review-preview">
                <MemoryDocumentView type={candidate.target} content={candidate.content} compact />
              </div>
              {openCandidate === candidate.candidate_id && (
                <div className="memory-review-detail">
                  {candidate.evidence && <MarkdownView content={`**证据**：${candidate.evidence}`} variant="compact" />}
                  {candidate.reason && <MarkdownView content={`**原因**：${candidate.reason}`} variant="compact" />}
                  <div className="memory-row-actions">
                    <button className="memory-primary-btn" onClick={() => void approveCandidate(candidate.candidate_id)} disabled={busy}>确认写入</button>
                    <button className="memory-danger-btn" onClick={() => void rejectCandidate(candidate.candidate_id)} disabled={busy}>拒绝</button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </section>

        <section className="memory-review-card">
          <div className="memory-section-title">
            <span>冲突记忆</span>
            <span>{conflicts.length}</span>
          </div>
          {conflicts.length === 0 ? (
            <div className="memory-empty">暂无待处理冲突</div>
          ) : conflicts.map((conflict) => (
            <div className="memory-review-item conflict" key={conflict.conflict_id}>
              <button className="memory-review-head" onClick={() => setOpenConflict(openConflict === conflict.conflict_id ? null : conflict.conflict_id)}>
                <span>{TITLES[conflict.changed_file]} · {conflict.severity ?? 'medium'} · {conflict.conflict_type ?? 'semantic_conflict'}</span>
                <span>{conflict.created_at ?? ''}</span>
              </button>
              <div className="memory-review-preview">
                <MemoryDocumentView type={conflict.changed_file} content={conflict.changed_content} compact />
              </div>
              {openConflict === conflict.conflict_id && (
                <div className="memory-review-detail">
                  <MarkdownView content={`**原因**：${conflict.llm_reason || conflict.reason || '潜在语义冲突'}`} variant="compact" />
                  {(conflict.conflicts_with ?? []).map((item, index) => (
                    <div className="memory-conflict-related" key={`${conflict.conflict_id}-${index}`}>
                      <strong>{TITLES[item.file]}</strong>
                      <MemoryDocumentView type={item.file} content={item.content} compact />
                    </div>
                  ))}
                  <div className="memory-row-actions">
                    <button className="memory-primary-btn" onClick={() => void resolveConflict(conflict.conflict_id, 'keep_new')} disabled={busy}>采用新记忆</button>
                    <button className="memory-ghost-btn" onClick={() => void resolveConflict(conflict.conflict_id, 'keep_old')} disabled={busy}>保留旧记忆</button>
                    <button className="memory-danger-btn" onClick={() => void resolveConflict(conflict.conflict_id, 'ignored')} disabled={busy}>忽略</button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </section>
      </main>
    </div>
  );
};

export { MemoryPanel };
