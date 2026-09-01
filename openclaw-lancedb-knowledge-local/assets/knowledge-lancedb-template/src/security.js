export const SECRET_PATTERNS = [
  { name: 'openai_key', re: /sk-(?:proj-)?[A-Za-z0-9_-]{20,}/g },
  { name: 'notion_token', re: /ntn_[A-Za-z0-9_-]{20,}/g },
  { name: 'google_api_key', re: /AIza[0-9A-Za-z_-]{20,}/g },
  { name: 'github_token', re: /(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})/g },
  { name: 'aws_access_key_id', re: /\bAKIA[0-9A-Z]{16}\b/g },
  { name: 'slack_token', re: /\bxox[bapsr]-[A-Za-z0-9-]{10,}/g },
  { name: 'discord_bot_token', re: /\b[MNO][A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{25,}\b/g },
  { name: 'telegram_bot_token', re: /\b\d{8,10}:AA[A-Za-z0-9_-]{30,40}\b/g },
  { name: 'stripe_live_key', re: /\bsk_live_[A-Za-z0-9]{16,}\b/g },
  { name: 'pem_private_key', re: /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g },
  { name: 'url_basic_auth', re: /\b[a-z][a-z0-9+.-]*:\/\/[^\s\/:@'"`]+:[^\s\/@'"`]+@/gi },
  { name: 'jwt', re: /eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g },
  { name: 'bearer', re: /Bearer\s+[A-Za-z0-9._~+\/-]+=*/gi },
  { name: 'key_value_secret', re: /\b(?:service\s*role\s*key|api\s*key|secret|token|password)\b\s*[:=]\s*[^\s`'"，,;]+/gi },
  { name: 'cjk_key_value_secret', re: /(?:密碼|金鑰|憑證)\s*[:：]\s*\S+/g }
];

export function redactSecrets(text) {
  let redacted = text;
  const hits = [];
  for (const p of SECRET_PATTERNS) {
    let count = 0;
    redacted = redacted.replace(p.re, () => {
      count += 1;
      return `[REDACTED_${p.name.toUpperCase()}]`;
    });
    if (count > 0) hits.push({ pattern: p.name, count });
  }
  return { text: redacted, hits };
}

export function looksBinary(buf) {
  const sample = buf.subarray(0, Math.min(buf.length, 4096));
  let zeros = 0;
  for (const b of sample) if (b === 0) zeros += 1;
  return sample.length > 0 && zeros / sample.length > 0.01;
}
