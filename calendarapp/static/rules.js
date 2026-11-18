'use strict';

const TIMED_DEFAULT_REMINDERS = [30];
const ALL_DAY_DEFAULT_REMINDERS = [];
const DEFAULT_MONTHLY_BY = 'day';
const JSON_TYPE = 'application/json';
const NO_CONTENT = 204;
const FIRST_SUCCESS = 200;
const LAST_SUCCESS = 299;

function comparableRepeat(rule) {
  if (!rule) return null;
  return {
    freq: rule.freq,
    interval: Number(rule.interval) || 1,
    weekdays: rule.freq === 'weekly' ? (rule.weekdays || []).slice().sort().join(',') : '',
    monthlyBy: rule.freq === 'monthly' ? rule.monthlyBy || DEFAULT_MONTHLY_BY : '',
    until: rule.until || '',
    count: Number(rule.count) || 0,
  };
}

function repeatChanged(before, after) {
  if (after === undefined) return false;
  const old = comparableRepeat(before);
  const now = comparableRepeat(after);
  if (old === null || now === null) return old !== now;
  return Object.keys(old).some((field) => old[field] !== now[field]);
}

function nextReminderCursor(current, latest) {
  const serverStartedAgain = latest < current;
  return serverStartedAgain ? latest : Math.max(current, latest);
}

function sameReminders(first, second) {
  return first.length === second.length && first.every((value, index) => value === second[index]);
}

function remindersAfterAllDayToggle(current, allDay) {
  const leftDefault = allDay ? TIMED_DEFAULT_REMINDERS : ALL_DAY_DEFAULT_REMINDERS;
  const isUntouched = current.length === 0 || sameReminders(current, leftDefault);
  if (!isUntouched) return current.slice();
  return (allDay ? ALL_DAY_DEFAULT_REMINDERS : TIMED_DEFAULT_REMINDERS).slice();
}

function isSignedOutAnswer(answer) {
  if (answer.redirected) return true;
  const succeeded = answer.status >= FIRST_SUCCESS && answer.status <= LAST_SUCCESS;
  if (!succeeded || answer.status === NO_CONTENT) return false;
  return !(answer.contentType || '').toLowerCase().startsWith(JSON_TYPE);
}

const CalRules = { TIMED_DEFAULT_REMINDERS, repeatChanged, nextReminderCursor, remindersAfterAllDayToggle, isSignedOutAnswer };

if (typeof module !== 'undefined') module.exports = CalRules;
if (typeof window !== 'undefined') window.CalRules = CalRules;
