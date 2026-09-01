const TAG_RULES = [
  ['decision', /(decision|decided|choose|chosen|approved|決策|決定|選擇|核准|拍板)/i],
  ['procedure', /(sop|procedure|workflow|runbook|步驟|流程|操作|處理方式|先.+再.+最後)/i],
  ['bug', /(bug|error|failure|failed|timeout|exception|root cause|根因|錯誤|故障|失敗|異常)/i],
  ['risk', /(risk|warning|danger|security|privacy|風險|警告|安全|隱私|機密)/i],
  ['action', /(action item|\baction\b|next step|todo|follow[- ]?up|下一步|待辦|需處理|行動)/i],
  ['status', /(status|progress|current|latest|done|blocked|狀態|進度|目前|最新|完成|阻塞)/i],
  ['lesson', /(lesson|learning|pitfall|retrospective|教訓|心得|踩坑|復盤)/i],
  ['architecture', /(architecture|schema|database|api|component|架構|資料庫|元件|欄位)/i],
  ['deployment', /(deploy|release|migration|rollback|ci\b|cd\b|部署|發布|上線|遷移|回滾)/i],
  ['security', /(credential|secret|token|permission|rls|auth|憑證|金鑰|權限|驗證)/i],
  ['data', /(data|dataset|index|embedding|vector|metadata|資料|索引|向量|標籤)/i],
  ['meeting', /(meeting|minutes|discussion|會議|討論|紀錄)/i]
];

const TYPE_PRECEDENCE = [
  ['decision', 'decision'],
  ['procedure', 'sop'],
  ['bug', 'bug'],
  ['risk', 'risk'],
  ['status', 'status'],
  ['lesson', 'lesson'],
  ['architecture', 'reference']
];

export function deriveDeterministicMetadata(chunk = {}) {
  const text = [chunk.source_type, chunk.title, chunk.heading, chunk.chunk_text].filter(Boolean).join('\n');
  const tags = [];
  for (const [tag, pattern] of TAG_RULES) {
    if (pattern.test(text)) tags.push(tag);
    if (tags.length >= 12) break;
  }
  let docType = 'general';
  for (const [tag, type] of TYPE_PRECEDENCE) {
    if (tags.includes(tag)) { docType = type; break; }
  }
  let importance = 2;
  if (tags.some((tag) => ['procedure', 'action', 'status', 'architecture', 'data'].includes(tag))) importance = 3;
  if (tags.some((tag) => ['decision', 'bug', 'risk', 'security', 'deployment'].includes(tag))) importance = 4;
  if (/(critical|urgent|production outage|data loss|credential leak|重大|緊急|資安事件|資料遺失|金鑰外洩)/i.test(text)) importance = 5;
  return {
    deterministic_doc_type: docType,
    deterministic_tags_json: JSON.stringify(tags),
    deterministic_importance: Math.max(1, Math.min(5, importance)),
    deterministic_metadata_version: 1
  };
}
