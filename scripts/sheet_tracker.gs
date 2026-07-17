// ============================================================
// CITE License Renewal — Gmail → Google Sheet (Sheet-only)
//
// Slimmed version: calendar events are now created by cite-cli
// itself (as .ics email invites sent on renewal detection), so
// this script ONLY appends renewals to the tracking spreadsheet.
//
// Setup:
//   1. Paste into script.google.com (replacing the previous version)
//   2. Run processRenewalEmails() once manually to grant permissions
//   3. Keep the existing time-driven trigger: processRenewalEmails → every hour
// ============================================================

const SPREADSHEET_ID_HERE = "1AkgeEjVUKQfCCow-crwRODXnvROrxp8GMcWObHJ0Oao"
const SHEET_NAME     = "CITE License Tracker";
const GMAIL_LABEL    = "cite-processed";
const SUBJECT_PREFIX = "[cite-cli] NIS-Elements license renewed";

function processRenewalEmails() {
  const label  = _getOrCreateLabel(GMAIL_LABEL);
  const sheets = SpreadsheetApp.openById(SPREADSHEET_ID_HERE);
  const sheet  = sheets.getSheetByName(SHEET_NAME);

  // Only look at threads not yet processed
  const threads = GmailApp.search(`subject:"${SUBJECT_PREFIX}" -label:${GMAIL_LABEL}`);

  for (const thread of threads) {
    const msg  = thread.getMessages()[0];
    const data = _parseEmail(msg.getPlainBody(), msg.getSubject());

    if (data) {
      _appendRowIfNew(sheet, data);
    }

    // Mark as processed regardless (prevents retry loops on unparsable emails)
    thread.addLabel(label);
  }
}

// ── Parsing ──────────────────────────────────────────────────

function _parseEmail(body, subject) {
  // Subject: "[cite-cli] NIS-Elements license renewed on HOSTNAME"
  const hostMatch  = subject.match(/renewed on (.+)$/i);
  // Body lines (from _notify.py send_apply_success_email):
  //   HASP ID:     09882A98 (decimal 12345678)
  //   Old expiry:  2025-08-01
  //   New expiry:  2026-08-01 (365 days from now)
  const haspMatch  = body.match(/^HASP ID:\s+([0-9A-Fa-f]+)/m);
  const oldMatch   = body.match(/^Old expiry:\s+(\d{4}-\d{2}-\d{2})/m);
  const newMatch   = body.match(/^New expiry:\s+(\d{4}-\d{2}-\d{2})/m);

  if (!haspMatch || !oldMatch || !newMatch) return null;

  return {
    host:      hostMatch ? hostMatch[1].trim() : "unknown",
    haspId:    haspMatch[1].trim(),
    oldExpiry: oldMatch[1].trim(),
    newExpiry: newMatch[1].trim(),
  };
}

// ── Sheet ─────────────────────────────────────────────────────

function _appendRowIfNew(sheet, data) {
  const rows = sheet.getDataRange().getValues();
  for (let i = 1; i < rows.length; i++) {
    // Columns: Timestamp | Name | HASP ID | Old Expiry | New Expiry
    if (String(rows[i][2]) === data.haspId && String(rows[i][4]) === data.newExpiry) {
      return; // already logged
    }
  }
  sheet.appendRow([new Date(), data.host, data.haspId, data.oldExpiry, data.newExpiry]);
}

// ── Utilities ─────────────────────────────────────────────────

function _getOrCreateLabel(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}
