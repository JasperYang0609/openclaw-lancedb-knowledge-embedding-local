import test from 'node:test';
import assert from 'node:assert/strict';
import { isRealDateSummaryRow, privacyGate } from '../src/cli.js';


test('real-date summary validation rejects synthetic inventory indexes', () => {
  assert.equal(isRealDateSummaryRow({
    source_type: 'backup_summary',
    source_path: '/backup/channel/summary/2026-08-23.md'
  }), true);
  assert.equal(isRealDateSummaryRow({
    source_type: 'backup_summary',
    source_path: '/backup/channel/summary/_inventory-index-2026-08-23.md'
  }), false);
  assert.equal(isRealDateSummaryRow({
    source_type: 'project_doc',
    source_path: '/backup/channel/summary/2026-08-23.md'
  }), false);
});


test('privacy gate keeps raw unapproved and exact lookup skipped', () => {
  assert.deepEqual(privacyGate({
    privacy: {
      discordRawApproval: 'NOT_CONFIRMED',
      exactMessageIdValidation: 'SKIPPED_PRIVACY_GATE'
    },
    sources: []
  }), {
    discordRawApproval: 'NOT_CONFIRMED',
    exactMessageIdValidation: 'SKIPPED_PRIVACY_GATE'
  });
});


test('privacy gate rejects raw source without approval', () => {
  assert.throws(() => privacyGate({
    privacy: {
      discordRawApproval: 'NOT_CONFIRMED',
      exactMessageIdValidation: 'SKIPPED_PRIVACY_GATE'
    },
    sources: [{ sourceType: 'discord_raw' }]
  }), /requires APPROVED_EXTERNAL or LOCAL_ONLY/);
});
