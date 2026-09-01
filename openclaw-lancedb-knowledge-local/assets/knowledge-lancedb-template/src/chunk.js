import path from 'node:path';
import crypto from 'node:crypto';

function sha256(text) {
  return crypto.createHash('sha256').update(text).digest('hex');
}

function estimateTokens(text) {
  // CJK-heavy text is underestimated by whitespace split; char/3 is a safer rough bound.
  return Math.max(text.split(/\s+/).filter(Boolean).length, Math.ceil([...text].length / 3));
}

function hardSplit(text, maxChars, overlapChars) {
  const chunks = [];
  const overlap = Math.max(0, Math.min(overlapChars, Math.floor(maxChars / 2)));
  let start = 0;
  while (start < text.length) {
    const target = Math.min(text.length, start + maxChars);
    let end = target;
    if (target < text.length) {
      const window = text.slice(start, target);
      const candidates = [
        window.lastIndexOf('\n'), window.lastIndexOf('。'), window.lastIndexOf('！'),
        window.lastIndexOf('？'), window.lastIndexOf('. '), window.lastIndexOf(' ')
      ];
      const boundary = Math.max(...candidates);
      if (boundary >= Math.floor(maxChars * 0.55)) end = start + boundary + 1;
    }
    const part = text.slice(start, end).trim();
    if (part) chunks.push(part);
    if (end >= text.length) break;
    start = Math.max(start + 1, end - overlap);
  }
  return chunks;
}

function splitOversized(text, maxChars = 3600, overlapChars = 350) {
  if (text.length <= maxChars) return [text];
  const paragraphs = text.split(/\n{2,}/).map((part) => part.trim()).filter(Boolean);
  const chunks = [];
  let current = '';
  const flush = () => {
    if (current) chunks.push(current);
    current = '';
  };
  for (const paragraph of paragraphs) {
    if (paragraph.length > maxChars) {
      flush();
      chunks.push(...hardSplit(paragraph, maxChars, overlapChars));
      continue;
    }
    const candidate = current ? `${current}\n\n${paragraph}` : paragraph;
    if (candidate.length <= maxChars) {
      current = candidate;
      continue;
    }
    const previous = current;
    flush();
    if (overlapChars > 0 && previous) {
      const tail = previous.slice(-Math.min(overlapChars, previous.length)).trim();
      const withOverlap = tail ? `${tail}\n\n${paragraph}` : paragraph;
      current = withOverlap.length <= maxChars ? withOverlap : paragraph;
    } else {
      current = paragraph;
    }
  }
  flush();
  return chunks;
}

function inferTitle(relPath, content) {
  const h1 = content.match(/^#\s+(.+)$/m);
  if (h1) return h1[1].trim().slice(0, 160);
  return path.basename(relPath, path.extname(relPath));
}

function inferDate(relPath, content) {
  const m = (relPath + '\n' + content.slice(0, 2000)).match(/(20\d{2}[-/]\d{2}[-/]\d{2})/);
  return m ? m[1].replaceAll('/', '-') : '';
}

function inferChannel(absPath) {
  const marker = '/頻道紀錄/';
  const idx = absPath.indexOf(marker);
  if (idx < 0) return '';
  const rest = absPath.slice(idx + marker.length).split('/');
  return rest[0] || '';
}

function inferProject(source, relPath) {
  // Respect source.project when set (the source map is authoritative); heuristics are only a
  // fallback for sources without a project label.
  if (source.project) return source.project;
  // Heuristics match relPath only; absPath almost always contains strings like .openclaw or
  // backup and would misclassify everything as OpenClawOps.
  const s = relPath.toLowerCase();
  if (/vaso|documentcenter|documentdetail|flutterflow/.test(s)) return 'VASO';
  if (/beango|咖啡|cafe/.test(s)) return 'BeanGo';
  if (/新人|trainee/.test(s)) return 'TraineeApp';
  if (/股票|stock|anan-stock|安安股票/.test(s)) return 'StockMonitor';
  // Word boundary keeps embedded "ig" in words like config / insight from matching IGResearch.
  if (/\big\b|instagram/.test(s)) return 'IGResearch';
  // Check AnsaiBrand before the OpenClawOps fallback so brand files whose names contain words
  // like backup can still match.
  if (/品牌|brand|ansai/.test(s)) return 'AnsaiBrand';
  if (/openclaw|backup|cron|memory|heartbeat/.test(s)) return 'OpenClawOps';
  return 'General';
}

export function chunkMarkdown({ source, absPath, relPath, content, options = {} }) {
  const title = inferTitle(relPath, content);
  const date = inferDate(relPath, content);
  const channel = inferChannel(absPath);
  const project = inferProject(source, relPath);
  const sections = [];
  const lines = content.split(/\r?\n/);
  let currentHeading = title;
  let buf = [];

  function flush() {
    const text = buf.join('\n').trim();
    if (text.length >= 40) sections.push({ heading: currentHeading, text });
    buf = [];
  }

  for (const line of lines) {
    const hm = line.match(/^(#{1,4})\s+(.+)$/);
    if (hm && buf.length > 0) {
      flush();
      currentHeading = hm[2].trim();
      buf.push(line);
    } else {
      if (hm) currentHeading = hm[2].trim();
      buf.push(line);
    }
  }
  flush();

  const chunks = [];
  let chunkIndex = 0;
  for (const section of sections.length ? sections : [{ heading: title, text: content }]) {
    for (const part of splitOversized(
      section.text,
      Number(options.maxChars) || 3600,
      options.overlapChars === 0 ? 0 : (Number(options.overlapChars) || 350)
    )) {
      const trimmed = part.trim();
      if (trimmed.length < 40) continue;
      chunks.push({
        id: sha256(`${absPath}\n${chunkIndex}\n${sha256(trimmed)}`),
        source_id: source.id,
        source_path: absPath,
        rel_path: relPath,
        source_type: source.sourceType,
        project,
        channel,
        title,
        heading: section.heading || title,
        date,
        chunk_index: chunkIndex++,
        chunk_text: trimmed,
        token_estimate: estimateTokens(trimmed),
        content_sha256: sha256(trimmed)
      });
    }
  }
  return chunks;
}
