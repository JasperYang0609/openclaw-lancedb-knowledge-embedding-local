import fs from 'node:fs';
import path from 'node:path';

function escapeRegex(s) {
  return s.replace(/[|\\{}()[\]^$+?.]/g, '\\$&');
}

export function globToRegExp(pattern) {
  let s = '';
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i];
    if (c === '*') {
      if (pattern[i + 1] === '*') {
        i++;
        if (pattern[i + 1] === '/') {
          i++;
          s += '(?:.*\/)?';
        } else {
          s += '.*';
        }
      } else {
        s += '[^/]*';
      }
    } else if (c === '?') {
      s += '[^/]';
    } else {
      s += escapeRegex(c);
    }
  }
  return new RegExp('^' + s + '$');
}

export function matchAny(rel, patterns = []) {
  return patterns.some((p) => globToRegExp(p).test(rel));
}

export function walkFiles(root) {
  const out = [];
  if (!fs.existsSync(root)) return out;
  const stack = [root];
  while (stack.length) {
    const cur = stack.pop();
    let entries;
    try { entries = fs.readdirSync(cur, { withFileTypes: true }); } catch { continue; }
    for (const ent of entries) {
      const p = path.join(cur, ent.name);
      if (ent.isDirectory()) {
        if (['node_modules', '.git', 'build', 'dist', '.dart_tool', '.next', 'data', 'output', '.lancedb'].includes(ent.name)) continue;
        stack.push(p);
      } else if (ent.isFile()) {
        out.push(p);
      }
    }
  }
  return out;
}
