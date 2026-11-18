'use strict';

const MS_PER_DAY = 86400000;
const MINUTES_PER_DAY = 1440;
const DAYS_IN_WEEK = 7;
const MONTH_GRID_DAYS = 42;
const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const WEEKDAY_LONG = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

function dayNumber(day) {
  const [year, month, date] = day.slice(0, 10).split('-').map(Number);
  return Math.round(Date.UTC(year, month - 1, date) / MS_PER_DAY);
}

function dayString(number) {
  return new Date(number * MS_PER_DAY).toISOString().slice(0, 10);
}

function addDays(day, amount) {
  return dayString(dayNumber(day) + amount);
}

function daysBetween(first, second) {
  return dayNumber(second) - dayNumber(first);
}

function weekdayIndex(day) {
  return (new Date(dayNumber(day) * MS_PER_DAY).getUTCDay() + 6) % DAYS_IN_WEEK;
}

function startOfWeek(day) {
  return addDays(day, -weekdayIndex(day));
}

function startOfMonth(day) {
  return `${day.slice(0, 7)}-01`;
}

function daysInMonth(year, month) {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

function addMonths(day, amount) {
  const [year, month, date] = day.slice(0, 10).split('-').map(Number);
  const index = year * 12 + (month - 1) + amount;
  const newYear = Math.floor(index / 12);
  const newMonth = index % 12 + 1;
  const newDate = Math.min(date, daysInMonth(newYear, newMonth));
  return `${String(newYear).padStart(4, '0')}-${String(newMonth).padStart(2, '0')}-${String(newDate).padStart(2, '0')}`;
}

function monthGridStart(day) {
  return startOfWeek(startOfMonth(day));
}

function monthGridDays(day) {
  const first = monthGridStart(day);
  return Array.from({ length: MONTH_GRID_DAYS }, (_, index) => addDays(first, index));
}

function dayOf(local) {
  return local.slice(0, 10);
}

function minutesOf(local) {
  if (local.length < 16) return 0;
  return Number(local.slice(11, 13)) * 60 + Number(local.slice(14, 16));
}

function clockText(local) {
  return local.slice(11, 16);
}

function monthTitle(day) {
  const [year, month] = day.split('-').map(Number);
  return `${MONTH_NAMES[month - 1]} ${year}`;
}

function shortDate(day) {
  const [, month, date] = day.split('-').map(Number);
  return `${WEEKDAY_SHORT[weekdayIndex(day)]} ${date} ${MONTH_NAMES[month - 1].slice(0, 3)}`;
}

function longDate(day) {
  const [year, month, date] = day.split('-').map(Number);
  return `${WEEKDAY_LONG[weekdayIndex(day)]} ${date} ${MONTH_NAMES[month - 1]} ${year}`;
}

function weekTitle(day) {
  const first = startOfWeek(day);
  const last = addDays(first, DAYS_IN_WEEK - 1);
  const [firstYear, firstMonth, firstDate] = first.split('-').map(Number);
  const [lastYear, lastMonth, lastDate] = last.split('-').map(Number);
  if (firstMonth === lastMonth) return `${firstDate} to ${lastDate} ${MONTH_NAMES[lastMonth - 1]} ${lastYear}`;
  const firstPart = `${firstDate} ${MONTH_NAMES[firstMonth - 1].slice(0, 3)}${firstYear === lastYear ? '' : ` ${firstYear}`}`;
  return `${firstPart} to ${lastDate} ${MONTH_NAMES[lastMonth - 1].slice(0, 3)} ${lastYear}`;
}

function eventLastDay(event) {
  if (event.allDay) return event.end;
  const endDay = dayOf(event.end);
  const endsAtMidnight = minutesOf(event.end) === 0 && endDay > dayOf(event.start);
  return endsAtMidnight ? addDays(endDay, -1) : endDay;
}

function touchesDay(event, day) {
  return dayOf(event.start) <= day && eventLastDay(event) >= day;
}

function isLong(event) {
  return event.allDay || eventLastDay(event) > dayOf(event.start);
}

function segmentOn(event, day) {
  const startMinute = dayOf(event.start) < day ? 0 : minutesOf(event.start);
  const endMinute = dayOf(event.end) > day ? MINUTES_PER_DAY : minutesOf(event.end);
  return { start: startMinute, end: Math.max(endMinute, startMinute) };
}

function layoutColumns(segments, minimumMinutes) {
  const sorted = segments.map((segment, index) => ({ ...segment, index }))
    .sort((first, second) => first.start - second.start || second.end - first.end);
  const placed = new Array(segments.length);
  let cluster = [];
  let clusterEnd = -1;
  let columnEnds = [];
  const finish = () => {
    for (const item of cluster) placed[item.index].columns = columnEnds.length;
    cluster = [];
    columnEnds = [];
  };
  for (const item of sorted) {
    const end = Math.max(item.end, item.start + minimumMinutes);
    if (item.start >= clusterEnd) finish();
    let column = columnEnds.findIndex((columnEnd) => columnEnd <= item.start);
    if (column === -1) {
      column = columnEnds.length;
      columnEnds.push(end);
    } else {
      columnEnds[column] = end;
    }
    placed[item.index] = { column, columns: 1 };
    cluster.push(item);
    clusterEnd = Math.max(clusterEnd, end);
  }
  finish();
  return placed;
}

function zonedNow(timeZone, now = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', {
    timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(now).map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

const CalDates = {
  MINUTES_PER_DAY, DAYS_IN_WEEK, WEEKDAY_SHORT, WEEKDAY_LONG, MONTH_NAMES,
  dayNumber, dayString, addDays, daysBetween, weekdayIndex, startOfWeek, startOfMonth, addMonths, monthGridStart,
  monthGridDays, dayOf, minutesOf, clockText, monthTitle, shortDate, longDate, weekTitle, eventLastDay, touchesDay,
  isLong, segmentOn, layoutColumns, zonedNow,
};

if (typeof module !== 'undefined') module.exports = CalDates;
if (typeof window !== 'undefined') window.CalDates = CalDates;
