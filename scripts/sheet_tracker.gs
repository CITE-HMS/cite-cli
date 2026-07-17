// ============================================================
// CITE License Renewal — Gmail → Google Calendar + Sheet
//
// When cite-cli reports a completed renewal, this script:
//   1. appends the renewal to the tracking spreadsheet; and
//   2. creates one recurring all-day Google Calendar event whose
//      three weekly occurrences are 14 days before expiration,
//      7 days before expiration, and on the expiration date.
//
// ============================================================

const SPREADSHEET_ID_HERE = "1AkgeEjVUKQfCCow-crwRODXnvROrxp8GMcWObHJ0Oao";
const SHEET_NAME          = "CITE License Tracker";
const GMAIL_LABEL         = "cite-processed";
const SUBJECT_PREFIX      = "[cite-cli] NIS-Elements license renewed";
const PROCESSED_KEY_PREFIX = "cite-renewal:";

function processRenewalEmails() {
  const label      = _getOrCreateLabel(GMAIL_LABEL);
  const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_ID_HERE);
  const sheet       = spreadsheet.getSheetByName(SHEET_NAME);
  const calendar    = CalendarApp.getDefaultCalendar();
  const properties  = PropertiesService.getScriptProperties();

  if (!sheet) {
    throw new Error(`Sheet not found: ${SHEET_NAME}`);
  }

  // Do not exclude labeled threads here. Gmail groups later renewals with the
  // same subject into an existing thread, which may already have the label.
  // The per-renewal Script Property below provides the real deduplication.
  const threads = GmailApp.search(`subject:"${SUBJECT_PREFIX}"`);
  Logger.log(`Found ${threads.length} matching thread(s).`);

  for (const thread of threads) {
    let containsRenewalMessage = false;

    // Process every message, not only the first one. A thread can contain
    // renewal confirmations from multiple years.
    for (const message of thread.getMessages()) {
      if (!message.getSubject().includes(SUBJECT_PREFIX)) continue;
      containsRenewalMessage = true;

      const data = _parseEmail(message.getPlainBody(), message.getSubject());
      if (!data) {
        Logger.log(`Could not parse renewal email: ${message.getSubject()}`);
        continue;
      }

      const processedKey = _processedKey(data);
      const processedAt = properties.getProperty(processedKey);
      if (processedAt) {
        Logger.log(
          `Skipping ${processedKey}: already marked processed at ${processedAt}.`,
        );
        continue;
      }

      // Set the processed key only after both operations succeed. If Calendar
      // creation fails after the row is appended, the next trigger run retries;
      // the Sheet and Calendar checks prevent duplicates.
      _appendRowIfNew(sheet, data);
      _createCalendarSeriesIfNew(calendar, data);
      properties.setProperty(processedKey, new Date().toISOString());
    }

    // This label is useful for visual organization only. It is deliberately
    // not used to decide which messages should be processed.
    if (containsRenewalMessage) thread.addLabel(label);
  }
}

// ── Parsing ──────────────────────────────────────────────────

function _parseEmail(body, subject) {
  // Subject: "[cite-cli] NIS-Elements license renewed on HOSTNAME"
  const hostMatch = subject.match(/renewed on (.+)$/i);
  // Body lines (from _notify.py send_apply_success_email):
  //   HASP ID:     09882A98 (decimal 12345678)
  //   Old expiry:  2025-08-01
  //   New expiry:  2026-08-01 (365 days from now)
  const haspMatch = body.match(/^HASP ID:\s+([0-9A-Fa-f]+)/m);
  const oldMatch  = body.match(/^Old expiry:\s+(\d{4}-\d{2}-\d{2})/m);
  const newMatch  = body.match(/^New expiry:\s+(\d{4}-\d{2}-\d{2})/m);

  if (!haspMatch || !oldMatch || !newMatch) return null;

  return {
    host:      hostMatch ? hostMatch[1].trim() : "unknown",
    haspId:    haspMatch[1].trim().toUpperCase(),
    oldExpiry: oldMatch[1].trim(),
    newExpiry: newMatch[1].trim(),
  };
}

// ── Sheet ─────────────────────────────────────────────────────

function _appendRowIfNew(sheet, data) {
  const rows = sheet.getDataRange().getValues();
  for (let i = 1; i < rows.length; i++) {
    // Columns: Timestamp | Name | HASP ID | Old Expiry | New Expiry
    const rowHasp   = String(rows[i][2]).trim().toUpperCase();
    const rowExpiry = _sheetDateToIso(rows[i][4]);
    if (rowHasp === data.haspId && rowExpiry === data.newExpiry) {
      return; // already logged
    }
  }

  sheet.appendRow([
    new Date(),
    data.host,
    data.haspId,
    data.oldExpiry,
    data.newExpiry,
  ]);
}

// ── Calendar ──────────────────────────────────────────────────

function _createCalendarSeriesIfNew(calendar, data) {
  const expiryDate = _parseDate(data.newExpiry);
  Logger.log(
    `_createCalendarSeriesIfNew: host=${data.host} hasp=${data.haspId} ` +
      `newExpiry=${data.newExpiry} expiryDate=${expiryDate} ` +
      `calendarId=${calendar.getId()}`,
  );

  // Historical confirmation emails may be encountered during the first run.
  // Keep their Sheet rows, but do not create reminder events for expired
  // licenses.
  if (_isBeforeToday(expiryDate)) {
    Logger.log(`Skipping: expiryDate ${expiryDate} is before today.`);
    return;
  }

  const firstReminder = new Date(expiryDate);
  firstReminder.setDate(firstReminder.getDate() - 14);

  const stationName = _stationName(data.host);
  const title = `⚠️ ${stationName} Elements expires ${data.newExpiry}`;

  // This also protects against a duplicate if Calendar creation succeeded but
  // saving the Script Property failed on the previous run.
  if (_eventExistsOnDay(calendar, firstReminder, title)) {
    const existingTitles = calendar
      .getEventsForDay(firstReminder)
      .map(e => e.getTitle());
    Logger.log(
      `Skipping: an event titled "${title}" already exists on ` +
        `${firstReminder}. All events found that day: ` +
        `${JSON.stringify(existingTitles)}`,
    );
    return;
  }

  const recurrence = CalendarApp.newRecurrence()
    .addWeeklyRule()
    .times(3);

  const description = [
    "NIS-Elements license renewal reminder.",
    "",
    `Station: ${data.host}`,
    `HASP: ${data.haspId}`,
    `Expiration date: ${data.newExpiry}`,
    "",
    "Occurrences: 14 days before expiration, 7 days before expiration, " +
      "and expiration day.",
  ].join("\n");

  const eventSeries = calendar.createAllDayEventSeries(
    title,
    firstReminder,
    recurrence,
    {description: description},
  );

  Logger.log(
    `Created calendar event series: ${title} (ID: ${eventSeries.getId()})`,
  );
}

function _eventExistsOnDay(calendar, date, title) {
  return calendar.getEventsForDay(date).some(event => event.getTitle() === title);
}

// ── Utilities ─────────────────────────────────────────────────

function _stationName(host) {
  // "Station 2 (Dongle 142841)" -> "Station 2". Falls back to the raw
  // host string for hosts with no dongle suffix (e.g. an unrecognized
  // HASP ID, where host is just the machine hostname).
  return host.replace(/\s*\(Dongle[^)]*\)\s*$/i, "").trim();
}

function _processedKey(data) {
  return `${PROCESSED_KEY_PREFIX}${data.haspId}:${data.newExpiry}`;
}

function _parseDate(isoString) {
  const [year, month, day] = isoString.split("-").map(Number);
  // Local noon avoids date shifts around time-zone and daylight-saving
  // boundaries. Calendar uses only the day for an all-day event.
  return new Date(year, month - 1, day, 12, 0, 0);
}

function _isBeforeToday(date) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const candidate = new Date(date);
  candidate.setHours(0, 0, 0, 0);
  return candidate < today;
}

function _sheetDateToIso(value) {
  if (value instanceof Date && !isNaN(value.getTime())) {
    return Utilities.formatDate(
      value,
      Session.getScriptTimeZone(),
      "yyyy-MM-dd",
    );
  }
  return String(value).trim();
}

function _getOrCreateLabel(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}
