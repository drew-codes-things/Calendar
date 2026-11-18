'use strict';

const D = window.CalDates;
const R = window.CalRules;
const HOUR_HEIGHT = 48;
const MIN_EVENT_MINUTES = 25;
const SHORT_BLOCK_MINUTES = 40;
const SCROLL_TO_MINUTE = 6 * 60 + 30;
const NEW_EVENT_HOUR = 9;
const DEFAULT_MINUTES = 60;
const SNAP_MINUTES = 30;
const MONTH_CHIPS = 3;
const PHONE_MONTH_CHIPS = 2;
const AGENDA_DAYS = 60;
const PREVIEW_DELAY_MS = 250;
const TOAST_MS = 3500;
const REMINDER_POLL_MS = 30000;
const REFRESH_MS = 5 * 60 * 1000;
const NOW_LINE_MS = 60000;
const LINK_POLL_MS = 3000;
const LINK_GIVE_UP_MS = 10 * 60 * 1000;
const PHONE_WIDTH = 600;
const DEFAULT_TIMED_REMINDERS = R.TIMED_DEFAULT_REMINDERS;
const SIGNED_OUT_TEXT = 'You are signed out; reload the page to sign in again';
const SIGNED_OUT_STATUS = 401;
const STORE_VIEW = 'calendar.view';
const STORE_HIDDEN = 'calendar.hidden';
const STORE_REMINDER_AFTER = 'calendar.reminderAfter';
const VIEWS = ['month', 'week', 'day', 'agenda'];
const WEEKDAY_CODES = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU'];
const ORDINALS = { 1: 'first', 2: 'second', 3: 'third', 4: 'fourth', '-1': 'last' };
const UNITS = { daily: ['day', 'days'], weekly: ['week', 'weeks'], monthly: ['month', 'months'], yearly: ['year', 'years'] };
const COLOURS = ['#e5383b', '#f08a3c', '#f2b552', '#5fd990', '#2bb5a0', '#5b8def', '#8b6cf0', '#d65db1', '#9a9aa5'];
const TIMED_PRESETS = [[0, 'At the start'], [5, '5 minutes before'], [10, '10 minutes before'], [15, '15 minutes before'],
  [30, '30 minutes before'], [60, '1 hour before'], [120, '2 hours before'], [1440, '1 day before'], [2880, '2 days before'],
  [10080, '1 week before']];
const ALL_DAY_PRESETS = [[-540, 'On the day at 09:00'], [900, 'The day before at 09:00'], [2340, 'Two days before at 09:00'],
  [9540, 'A week before at 09:00']];

const state = {
  session: null,
  calendars: [],
  hidden: new Set(),
  view: 'month',
  anchor: '',
  events: [],
  loadToken: 0,
  editing: null,
  editorStart: {},
  reminderAfter: 0,
  linkTimer: null,
};

function byId(id) {
  return document.getElementById(id);
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function readStored(name, fallback) {
  try {
    const value = window.localStorage.getItem(name);
    return value === null ? fallback : value;
  } catch (error) {
    return fallback;
  }
}

function writeStored(name, value) {
  try {
    window.localStorage.setItem(name, value);
  } catch (error) {
    return;
  }
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(method, path, body, options = {}) {
  const headers = {};
  if (method !== 'GET') headers['X-CSRF-Token'] = state.session ? state.session.csrfToken : '';
  if (options.etag) headers['If-Match'] = options.etag;
  let payload;
  if (options.raw !== undefined) {
    payload = options.raw;
    headers['Content-Type'] = 'text/calendar';
  } else if (body !== undefined) {
    payload = JSON.stringify(body);
    headers['Content-Type'] = 'application/json';
  }
  const response = await fetch(path, { method, headers, body: payload, credentials: 'same-origin' });
  const answer = { redirected: response.redirected, status: response.status, contentType: response.headers.get('Content-Type') };
  if (R.isSignedOutAnswer(answer)) throw new ApiError(SIGNED_OUT_TEXT, SIGNED_OUT_STATUS);
  if (response.status === 204) return null;
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) throw new ApiError((data && data.error) || `the server answered ${response.status}`, response.status);
  return data;
}

let toastTimer = null;
function toast(message, isError = false) {
  const node = byId('toast');
  node.textContent = message;
  node.classList.toggle('error', isError);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, TOAST_MS);
}

function failed(error) {
  toast(error && error.message ? error.message : 'Something went wrong', true);
}

function calendarById(id) {
  return state.calendars.find((calendar) => calendar.id === id);
}

function isPhone() {
  return window.matchMedia(`(max-width: ${PHONE_WIDTH}px)`).matches;
}

function visibleEvents() {
  return state.events.filter((event) => !state.hidden.has(event.calendarId));
}

function eventsOn(day) {
  return visibleEvents().filter((event) => D.touchesDay(event, day)).sort(compareEvents);
}

function compareEvents(first, second) {
  const firstLong = D.isLong(first);
  const secondLong = D.isLong(second);
  if (firstLong !== secondLong) return firstLong ? -1 : 1;
  return first.start.localeCompare(second.start) || first.title.localeCompare(second.title);
}

function timeRangeText(event) {
  if (event.allDay) {
    return event.start === event.end ? 'All day' : `All day, ${D.shortDate(event.start)} to ${D.shortDate(event.end)}`;
  }
  const sameDay = D.dayOf(event.start) === D.dayOf(event.end);
  if (sameDay) return `${D.clockText(event.start)} to ${D.clockText(event.end)}`;
  return `${D.shortDate(D.dayOf(event.start))} ${D.clockText(event.start)} to ${D.shortDate(D.dayOf(event.end))} ${D.clockText(event.end)}`;
}

function rangeForView() {
  if (state.view === 'month') {
    const days = D.monthGridDays(state.anchor);
    return { from: days[0], to: D.addDays(days[days.length - 1], 1) };
  }
  if (state.view === 'week') {
    const first = D.startOfWeek(state.anchor);
    return { from: first, to: D.addDays(first, D.DAYS_IN_WEEK) };
  }
  if (state.view === 'day') return { from: state.anchor, to: D.addDays(state.anchor, 1) };
  return { from: state.anchor, to: D.addDays(state.anchor, AGENDA_DAYS) };
}

function titleForView() {
  if (state.view === 'month') return D.monthTitle(state.anchor);
  if (state.view === 'week') return D.weekTitle(state.anchor);
  if (state.view === 'day') return D.longDate(state.anchor);
  return `From ${D.shortDate(state.anchor)}`;
}

async function loadEvents() {
  const token = ++state.loadToken;
  const range = rangeForView();
  try {
    const data = await api('GET', `/api/events?from=${range.from}&to=${range.to}`);
    if (token !== state.loadToken) return;
    state.events = data.events;
    if (data.today) state.session.today = data.today;
    render();
    if (data.truncated) toast('Too many events to show them all here; narrow the view', true);
  } catch (error) {
    if (token === state.loadToken) failed(error);
  }
}

async function loadCalendars() {
  const data = await api('GET', '/api/calendars');
  state.calendars = data.calendars;
  renderCalendarList();
}

function setView(view) {
  if (!VIEWS.includes(view)) return;
  state.view = view;
  writeStored(STORE_VIEW, view);
  for (const button of document.querySelectorAll('.seg')) button.setAttribute('aria-pressed', String(button.dataset.view === view));
  render();
  loadEvents();
}

function step(direction) {
  if (state.view === 'month') state.anchor = D.addMonths(state.anchor, direction);
  else if (state.view === 'week') state.anchor = D.addDays(state.anchor, direction * D.DAYS_IN_WEEK);
  else if (state.view === 'day') state.anchor = D.addDays(state.anchor, direction);
  else state.anchor = D.addDays(state.anchor, direction * AGENDA_DAYS);
  render();
  loadEvents();
}

function goTo(day, view) {
  state.anchor = day;
  if (view) setView(view);
  else {
    render();
    loadEvents();
  }
}

function render() {
  byId('range-title').textContent = titleForView();
  const container = byId('view');
  container.replaceChildren();
  container.className = `view view-${state.view}`;
  if (state.view === 'month') renderMonth(container);
  else if (state.view === 'agenda') renderAgenda(container);
  else renderTimeGrid(container, state.view === 'week' ? weekDays() : [state.anchor]);
}

function weekDays() {
  const first = D.startOfWeek(state.anchor);
  return Array.from({ length: D.DAYS_IN_WEEK }, (_, index) => D.addDays(first, index));
}

function colourOf(event) {
  const calendar = calendarById(event.calendarId);
  return calendar ? calendar.color : event.color;
}

function eventChip(event, day, className) {
  const chip = element('button', `chip ${className}`);
  chip.type = 'button';
  chip.style.setProperty('--event', colourOf(event));
  const long = D.isLong(event);
  if (long) {
    chip.classList.add('long');
    if (D.dayOf(event.start) < day) chip.classList.add('continues-before');
    if (D.eventLastDay(event) > day) chip.classList.add('continues-after');
  } else {
    chip.append(element('span', 'dot'), element('span', 'chip-time', D.clockText(event.start)));
  }
  chip.append(element('span', 'chip-title', event.title || '(no title)'));
  chip.title = `${event.title}\n${timeRangeText(event)}${event.location ? `\n${event.location}` : ''}`;
  chip.addEventListener('click', (click) => {
    click.stopPropagation();
    openEditor(event);
  });
  return chip;
}

function renderMonth(container) {
  const days = D.monthGridDays(state.anchor);
  const month = state.anchor.slice(0, 7);
  const today = state.session.today;
  const limit = isPhone() ? PHONE_MONTH_CHIPS : MONTH_CHIPS;
  const head = element('div', 'month-head');
  for (const name of D.WEEKDAY_SHORT) head.append(element('div', 'month-weekday', name));
  const grid = element('div', 'month-grid');
  for (const day of days) {
    const cell = element('div', 'day-cell');
    if (day.slice(0, 7) !== month) cell.classList.add('other-month');
    if (day === today) cell.classList.add('today');
    const number = element('button', 'day-number', String(Number(day.slice(8, 10))));
    number.type = 'button';
    number.setAttribute('aria-label', `Open ${D.longDate(day)}`);
    number.addEventListener('click', (click) => {
      click.stopPropagation();
      goTo(day, 'day');
    });
    cell.append(number);
    const todays = eventsOn(day);
    for (const event of todays.slice(0, limit)) cell.append(eventChip(event, day, 'month-chip'));
    if (todays.length > limit) {
      const more = element('button', 'more', `${todays.length - limit} more`);
      more.type = 'button';
      more.addEventListener('click', (click) => {
        click.stopPropagation();
        goTo(day, 'day');
      });
      cell.append(more);
    }
    cell.addEventListener('click', () => openEditor(null, newEventAt(day, NEW_EVENT_HOUR * 60)));
    grid.append(cell);
  }
  container.append(head, grid);
}

function renderAgenda(container) {
  const range = rangeForView();
  const list = element('div', 'agenda');
  let shown = 0;
  for (let day = range.from; day < range.to; day = D.addDays(day, 1)) {
    const todays = eventsOn(day).filter((event) => D.dayOf(event.start) === day || day === range.from);
    if (!todays.length) continue;
    shown += todays.length;
    const group = element('section', 'agenda-day');
    const heading = element('h2', `agenda-date${day === state.session.today ? ' today' : ''}`, D.shortDate(day));
    group.append(heading);
    for (const event of todays) group.append(agendaRow(event));
    list.append(group);
  }
  if (!shown) list.append(element('p', 'empty-state', `Nothing in the ${AGENDA_DAYS} days from ${D.shortDate(range.from)}.`));
  container.append(list);
}

function agendaRow(event) {
  const row = element('button', 'agenda-row');
  row.type = 'button';
  row.style.setProperty('--event', colourOf(event));
  const when = element('span', 'agenda-time', event.allDay ? 'All day' : `${D.clockText(event.start)} to ${D.clockText(event.end)}`);
  const main = element('span', 'agenda-main');
  main.append(element('span', 'agenda-title', event.title || '(no title)'));
  const meta = element('span', 'agenda-meta');
  if (D.isLong(event)) meta.append(element('span', '', timeRangeText(event)));
  if (event.location) meta.append(iconText('i-pin', event.location));
  if (event.recurring) meta.append(iconText('i-repeat', event.repeatText || 'Repeats'));
  if (meta.childNodes.length) main.append(meta);
  row.append(element('span', 'dot'), when, main);
  row.addEventListener('click', () => openEditor(event));
  return row;
}

function iconText(symbol, text) {
  const wrap = element('span', 'icon-text');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'icon tiny');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#${symbol}`);
  svg.append(use);
  wrap.append(svg, element('span', '', text));
  return wrap;
}

function renderTimeGrid(container, days) {
  const today = state.session.today;
  const head = element('div', 'grid-head');
  head.style.setProperty('--days', days.length);
  head.append(element('div', 'gutter'));
  for (const day of days) {
    const label = element('button', `grid-day${day === today ? ' today' : ''}`);
    label.type = 'button';
    label.append(element('span', 'grid-weekday', D.WEEKDAY_SHORT[D.weekdayIndex(day)]), element('span', 'grid-date', String(Number(day.slice(8, 10)))));
    label.addEventListener('click', () => goTo(day, 'day'));
    head.append(label);
  }
  const allDay = element('div', 'all-day-row');
  allDay.style.setProperty('--days', days.length);
  allDay.append(element('div', 'gutter gutter-label', 'all day'));
  for (const day of days) {
    const cell = element('div', 'all-day-cell');
    for (const event of eventsOn(day).filter((item) => item.allDay || coversWholeDay(item, day))) cell.append(eventChip(event, day, 'all-day-chip'));
    cell.addEventListener('click', () => openEditor(null, { ...newEventAt(day, 0), allDay: true, start: day, end: day, reminders: [] }));
    allDay.append(cell);
  }
  const scroller = element('div', 'grid-scroll');
  const body = element('div', 'grid-body');
  body.style.setProperty('--days', days.length);
  body.style.setProperty('--hour', `${HOUR_HEIGHT}px`);
  const hours = element('div', 'gutter hours');
  for (let hour = 0; hour < 24; hour += 1) hours.append(element('div', 'hour-label', hour ? `${String(hour).padStart(2, '0')}:00` : ''));
  body.append(hours);
  const nowLocal = D.zonedNow(state.session.timeZone);
  for (const day of days) body.append(dayColumn(day, day === D.dayOf(nowLocal) ? D.minutesOf(nowLocal) : null));
  scroller.append(head, allDay, body);
  container.append(scroller);
  requestAnimationFrame(() => {
    scroller.style.setProperty('--head-height', `${head.offsetHeight}px`);
    scroller.scrollTop = (SCROLL_TO_MINUTE / 60) * HOUR_HEIGHT;
  });
}

function dayColumn(day, nowMinute) {
  const column = element('div', 'day-column');
  column.dataset.day = day;
  const timed = eventsOn(day).filter((event) => !event.allDay && !coversWholeDay(event, day));
  const segments = timed.map((event) => D.segmentOn(event, day));
  const placed = D.layoutColumns(segments, MIN_EVENT_MINUTES);
  timed.forEach((event, index) => column.append(timedBlock(event, segments[index], placed[index])));
  if (nowMinute !== null) {
    const line = element('div', 'now-line');
    line.style.top = `${nowMinute * HOUR_HEIGHT / 60}px`;
    column.append(line);
  }
  column.addEventListener('click', (click) => {
    if (click.target !== column) return;
    const rect = column.getBoundingClientRect();
    const minute = Math.floor((click.clientY - rect.top) / HOUR_HEIGHT * 60 / SNAP_MINUTES) * SNAP_MINUTES;
    openEditor(null, newEventAt(day, Math.max(0, Math.min(minute, D.MINUTES_PER_DAY - SNAP_MINUTES))));
  });
  return column;
}

function coversWholeDay(event, day) {
  const segment = D.segmentOn(event, day);
  return D.isLong(event) && segment.start === 0 && segment.end === D.MINUTES_PER_DAY;
}

function timedBlock(event, segment, place) {
  const block = element('button', 'block');
  block.type = 'button';
  block.style.setProperty('--event', colourOf(event));
  const length = Math.max(segment.end - segment.start, MIN_EVENT_MINUTES);
  block.style.top = `${segment.start * HOUR_HEIGHT / 60}px`;
  block.style.height = `${length * HOUR_HEIGHT / 60 - 2}px`;
  block.style.left = `${place.column / place.columns * 100}%`;
  block.style.width = `${100 / place.columns}%`;
  if (length < SHORT_BLOCK_MINUTES) {
    block.classList.add('short');
    block.append(element('span', 'block-title', `${event.title || '(no title)'}, ${D.clockText(event.start)}`));
  } else {
    block.append(element('span', 'block-title', event.title || '(no title)'), element('span', 'block-time', timeRangeText(event)));
    if (event.location) block.append(element('span', 'block-place', event.location));
  }
  block.title = `${event.title}\n${timeRangeText(event)}`;
  block.addEventListener('click', () => openEditor(event));
  return block;
}

function renderCalendarList() {
  const list = byId('calendar-list');
  list.replaceChildren();
  for (const calendar of state.calendars) {
    const item = element('li', 'calendar-item');
    const label = element('label', 'calendar-toggle');
    const box = element('input');
    box.type = 'checkbox';
    box.checked = !state.hidden.has(calendar.id);
    box.style.setProperty('--event', calendar.color);
    box.addEventListener('change', () => {
      if (box.checked) state.hidden.delete(calendar.id);
      else state.hidden.add(calendar.id);
      writeStored(STORE_HIDDEN, JSON.stringify([...state.hidden]));
      render();
    });
    label.append(box, element('span', 'calendar-name', calendar.name));
    const edit = element('button', 'icon-btn small edit-calendar');
    edit.type = 'button';
    edit.setAttribute('aria-label', `Edit ${calendar.name}`);
    edit.append(iconText('i-gear', '').firstChild);
    edit.addEventListener('click', () => openCalendarDialog(calendar));
    item.append(label, edit);
    list.append(item);
  }
  const options = state.calendars.map((calendar) => [calendar.id, calendar.name]);
  fillSelect(byId('ed-calendar'), options);
  fillSelect(byId('import-calendar'), options);
}

function fillSelect(select, options, chosen) {
  const keep = chosen !== undefined ? String(chosen) : select.value;
  select.replaceChildren(...options.map(([value, text]) => {
    const option = element('option', '', text);
    option.value = String(value);
    return option;
  }));
  if (options.some(([value]) => String(value) === keep)) select.value = keep;
}

function clockMinutes(clock) {
  const [hours, minutes] = clock.split(':').map(Number);
  return hours * 60 + minutes;
}

function clockString(minutes) {
  return `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;
}

function newEventAt(day, minute) {
  const startDate = `${day}T${clockString(minute)}`;
  const endDate = `${day}T${clockString(Math.min(minute + DEFAULT_MINUTES, D.MINUTES_PER_DAY - 1))}`;
  const visible = state.calendars.find((calendar) => !state.hidden.has(calendar.id)) || state.calendars[0];
  return { title: '', calendarId: visible ? visible.id : '', allDay: false, start: startDate, end: endDate, location: '', notes: '',
    repeat: null, reminders: DEFAULT_TIMED_REMINDERS.slice() };
}

function nthWeekday(day) {
  const week = Math.floor((Number(day.slice(8, 10)) - 1) / 7) + 1;
  return week > 4 ? -1 : week;
}

function showError(id, error) {
  const node = byId(id);
  node.textContent = error ? error.message || String(error) : '';
  node.hidden = !error;
}

function openEditor(event, defaults) {
  const source = event || defaults;
  state.editing = event;
  showError('ed-error', null);
  byId('ed-title').value = source.title || '';
  fillSelect(byId('ed-calendar'), state.calendars.map((calendar) => [calendar.id, calendar.name]), source.calendarId);
  byId('ed-allday').checked = source.allDay;
  byId('ed-start-date').value = D.dayOf(source.start);
  byId('ed-end-date').value = D.dayOf(source.end);
  byId('ed-start-time').value = source.allDay ? '09:00' : D.clockText(source.start);
  byId('ed-end-time').value = source.allDay ? '10:00' : D.clockText(source.end);
  byId('ed-location').value = source.location || '';
  byId('ed-notes').value = source.notes || '';
  fillRepeat(source.repeat, D.dayOf(source.start));
  fillReminders(source.reminders || [], source.allDay);
  const note = byId('ed-note');
  note.hidden = !(event && event.recurring);
  note.textContent = event && event.recurring
    ? `${event.repeatText || 'Repeats'}${event.changedInstance ? '. This date was changed on its own.' : ''}` : '';
  byId('ed-delete').hidden = !event;
  noteEditorStart();
  syncAllDay();
  byId('editor').showModal();
  if (!event) byId('ed-title').focus();
}

function fillRepeat(repeat, startDay) {
  const select = byId('ed-repeat');
  const customOption = select.querySelector('option[value="custom"]');
  customOption.hidden = !(repeat && repeat.custom);
  select.value = repeat ? (repeat.custom ? 'custom' : repeat.freq) : '';
  byId('ed-interval').value = repeat && repeat.interval ? repeat.interval : 1;
  const chosen = new Set(repeat && repeat.weekdays ? repeat.weekdays : [WEEKDAY_CODES[D.weekdayIndex(startDay)]]);
  const toggles = byId('ed-weekdays');
  toggles.replaceChildren(...WEEKDAY_CODES.map((code, index) => {
    const toggle = element('button', 'day-toggle', D.WEEKDAY_SHORT[index]);
    toggle.type = 'button';
    toggle.dataset.code = code;
    toggle.setAttribute('aria-pressed', String(chosen.has(code)));
    toggle.addEventListener('click', () => toggle.setAttribute('aria-pressed', String(toggle.getAttribute('aria-pressed') !== 'true')));
    return toggle;
  }));
  fillMonthly(startDay, repeat && repeat.monthlyBy);
  const ends = repeat && repeat.until ? 'until' : repeat && repeat.count ? 'count' : 'never';
  byId('ed-ends').value = ends;
  byId('ed-until').value = repeat && repeat.until ? repeat.until : D.addMonths(startDay, 3);
  byId('ed-count').value = repeat && repeat.count ? repeat.count : 10;
  syncRepeat();
}

function fillMonthly(startDay, chosen) {
  const nth = nthWeekday(startDay);
  fillSelect(byId('ed-monthly'), [
    ['day', `Day ${Number(startDay.slice(8, 10))}`],
    ['weekday', `The ${ORDINALS[nth]} ${D.WEEKDAY_LONG[D.weekdayIndex(startDay)]}`],
  ], chosen || 'day');
}

function syncRepeat() {
  const freq = byId('ed-repeat').value;
  const active = freq !== '' && freq !== 'custom';
  byId('repeat-more').hidden = !active;
  if (!active) return;
  const interval = Number(byId('ed-interval').value) || 1;
  byId('ed-unit').textContent = UNITS[freq][interval === 1 ? 0 : 1];
  byId('ed-weekdays').hidden = freq !== 'weekly';
  byId('ed-monthly-wrap').hidden = freq !== 'monthly';
  byId('ed-until-wrap').hidden = byId('ed-ends').value !== 'until';
  byId('ed-count-wrap').hidden = byId('ed-ends').value !== 'count';
}

function reminderOptions(allDay, value) {
  const presets = (allDay ? ALL_DAY_PRESETS : TIMED_PRESETS).slice();
  if (value !== undefined && !presets.some(([minutes]) => minutes === value)) {
    presets.push([value, value >= 0 ? `${value} minutes before` : `${-value} minutes after the start`]);
  }
  return presets;
}

function reminderRow(allDay, value) {
  const row = element('li', 'reminder-row');
  const select = element('select', 'reminder-select');
  select.setAttribute('aria-label', 'Reminder');
  fillSelect(select, reminderOptions(allDay, value), value === undefined ? reminderOptions(allDay)[0][0] : value);
  const remove = element('button', 'icon-btn small');
  remove.type = 'button';
  remove.setAttribute('aria-label', 'Remove this reminder');
  remove.append(iconText('i-x', '').firstChild);
  remove.addEventListener('click', () => row.remove());
  row.append(iconText('i-bell', '').firstChild, select, remove);
  return row;
}

function fillReminders(minutes, allDay) {
  byId('ed-reminders').replaceChildren(...minutes.map((value) => reminderRow(allDay, value)));
}

function syncAllDay() {
  const allDay = byId('ed-allday').checked;
  for (const field of document.querySelectorAll('.time-field')) field.hidden = allDay;
}

function editorTimes() {
  const allDay = byId('ed-allday').checked;
  const startDate = byId('ed-start-date').value;
  const endDate = byId('ed-end-date').value;
  if (allDay) return { allDay, start: startDate, end: endDate };
  return { allDay, start: `${startDate}T${byId('ed-start-time').value}`, end: `${endDate}T${byId('ed-end-time').value}` };
}

function editorRepeat() {
  const freq = byId('ed-repeat').value;
  if (freq === '') return null;
  if (freq === 'custom') return undefined;
  const repeat = { freq, interval: Number(byId('ed-interval').value) || 1 };
  if (freq === 'weekly') {
    repeat.weekdays = [...byId('ed-weekdays').querySelectorAll('[aria-pressed="true"]')].map((toggle) => toggle.dataset.code);
  }
  if (freq === 'monthly') repeat.monthlyBy = byId('ed-monthly').value;
  const ends = byId('ed-ends').value;
  if (ends === 'until') repeat.until = byId('ed-until').value;
  if (ends === 'count') repeat.count = Number(byId('ed-count').value);
  return repeat;
}

function editorReminders() {
  return [...byId('ed-reminders').querySelectorAll('select')].map((select) => Number(select.value));
}

function editorFields() {
  const fields = {
    title: byId('ed-title').value,
    calendarId: byId('ed-calendar').value,
    location: byId('ed-location').value,
    notes: byId('ed-notes').value,
    reminders: editorReminders(),
    ...editorTimes(),
  };
  const repeat = editorRepeat();
  if (repeat !== undefined) fields.repeat = repeat;
  return fields;
}

function askScope(title, text) {
  const dialog = byId('scope');
  byId('scope-title').textContent = title;
  byId('scope-text').textContent = text;
  dialog.returnValue = 'cancel';
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue), { once: true });
  });
}

function askConfirm(title, text, action) {
  const dialog = byId('confirm');
  byId('confirm-title').textContent = title;
  byId('confirm-text').textContent = text;
  byId('confirm-ok').textContent = action;
  dialog.returnValue = 'cancel';
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
  });
}

function eventPath(event) {
  return `/api/calendars/${encodeURIComponent(event.calendarId)}/events/${encodeURIComponent(event.itemId)}`;
}

async function saveEditor(submit) {
  submit.preventDefault();
  const fields = editorFields();
  if (!fields.title.trim()) {
    showError('ed-error', new Error('Give the event a title'));
    return;
  }
  const event = state.editing;
  try {
    if (!event) {
      await api('POST', '/api/events', fields);
      toast('Event added');
    } else {
      let scope = 'all';
      const repeatWasChanged = R.repeatChanged(event.repeat, fields.repeat);
      if (event.occurrence && !repeatWasChanged && fields.calendarId === event.calendarId) {
        scope = await askScope('Save a repeating event', 'Change only this date, or every date of the event?');
        if (scope === 'cancel') return;
      } else if (event.recurring && repeatWasChanged) {
        const sure = await askConfirm('Change how it repeats', 'Dates you changed on their own go back to the new pattern.', 'Change all dates');
        if (!sure) return;
      }
      const body = { ...fields };
      if (scope === 'one') {
        delete body.repeat;
        delete body.calendarId;
      }
      const query = event.occurrence ? `?scope=${scope}&occurrence=${encodeURIComponent(event.occurrence)}` : `?scope=${scope}`;
      await api('PATCH', `${eventPath(event)}${query}`, body, { etag: event.etag });
      toast('Saved');
    }
    byId('editor').close();
    await loadEvents();
  } catch (error) {
    showError('ed-error', error);
  }
}

async function deleteFromEditor() {
  const event = state.editing;
  if (!event) return;
  let scope = 'all';
  if (event.occurrence) {
    scope = await askScope('Delete a repeating event', 'Delete only this date, or every date of the event?');
    if (scope === 'cancel') return;
  } else if (!await askConfirm('Delete this event?', event.title, 'Delete')) {
    return;
  }
  try {
    const query = event.occurrence ? `?scope=${scope}&occurrence=${encodeURIComponent(event.occurrence)}` : `?scope=${scope}`;
    await api('DELETE', `${eventPath(event)}${query}`, undefined, { etag: event.etag });
    byId('editor').close();
    toast('Deleted');
    await loadEvents();
  } catch (error) {
    showError('ed-error', error);
  }
}

function noteEditorStart() {
  state.editorStart = { date: byId('ed-start-date').value, time: byId('ed-start-time').value };
}

function shiftEndWithStart(previousStart) {
  const allDay = byId('ed-allday').checked;
  const startDate = byId('ed-start-date').value;
  const endDate = byId('ed-end-date').value;
  if (!startDate || !endDate || !previousStart.date) return;
  const dayShift = D.daysBetween(previousStart.date, startDate);
  byId('ed-end-date').value = D.addDays(endDate, dayShift);
  if (allDay || !previousStart.time) return;
  const moved = clockMinutes(byId('ed-start-time').value) - clockMinutes(previousStart.time);
  const endMinutes = clockMinutes(byId('ed-end-time').value) + moved;
  const wrapped = ((endMinutes % D.MINUTES_PER_DAY) + D.MINUTES_PER_DAY) % D.MINUTES_PER_DAY;
  const extraDays = Math.floor(endMinutes / D.MINUTES_PER_DAY);
  byId('ed-end-time').value = clockString(wrapped);
  if (extraDays) byId('ed-end-date').value = D.addDays(byId('ed-end-date').value, extraDays);
}

let previewTimer = null;
function schedulePreview() {
  clearTimeout(previewTimer);
  const text = byId('quick-text').value.trim();
  const preview = byId('quick-preview');
  if (!text) {
    preview.replaceChildren();
    return;
  }
  previewTimer = setTimeout(async () => {
    try {
      const fields = await api('POST', '/api/parse', { text });
      if (byId('quick-text').value.trim() !== text) return;
      const calendar = calendarById(fields.calendarId);
      const parts = [fields.title, fields.allDay ? timeRangeText(fields) : `${D.shortDate(D.dayOf(fields.start))}, ${timeRangeText(fields)}`];
      if (fields.location) parts.push(`at ${fields.location}`);
      if (fields.repeat) parts.push(`repeats ${fields.repeat.freq}`);
      if (calendar) parts.push(calendar.name);
      preview.replaceChildren(element('span', 'hint', parts.join(' · ')));
    } catch (error) {
      if (byId('quick-text').value.trim() === text) preview.replaceChildren(element('span', 'error', error.message));
    }
  }, PREVIEW_DELAY_MS);
}

async function submitQuickAdd(submit) {
  submit.preventDefault();
  const text = byId('quick-text').value.trim();
  if (!text) return;
  try {
    const created = await api('POST', '/api/quick', { text });
    byId('quick-text').value = '';
    byId('quick-preview').replaceChildren();
    toast(`Added ${created.understood.title} on ${D.shortDate(D.dayOf(created.understood.start))}`);
    goTo(D.dayOf(created.understood.start));
  } catch (error) {
    byId('quick-preview').replaceChildren(element('span', 'error', error.message));
  }
}

function openCalendarDialog(calendar) {
  const dialog = byId('calendar-dialog');
  dialog.dataset.calendar = calendar ? calendar.id : '';
  byId('calendar-heading').textContent = calendar ? 'Edit calendar' : 'New calendar';
  byId('cal-name').value = calendar ? calendar.name : '';
  byId('cal-delete').hidden = !calendar || state.calendars.length < 2;
  showError('cal-error', null);
  const chosen = calendar ? calendar.color : COLOURS[state.calendars.length % COLOURS.length];
  const swatches = byId('cal-swatches');
  swatches.replaceChildren(...COLOURS.map((colour) => {
    const swatch = element('button', 'swatch');
    swatch.type = 'button';
    swatch.style.setProperty('--event', colour);
    swatch.dataset.colour = colour;
    swatch.setAttribute('role', 'radio');
    swatch.setAttribute('aria-label', colour);
    swatch.setAttribute('aria-checked', String(colour === chosen));
    swatch.addEventListener('click', () => {
      for (const other of swatches.children) other.setAttribute('aria-checked', String(other === swatch));
    });
    return swatch;
  }));
  if (!COLOURS.includes(chosen)) swatches.firstChild.setAttribute('aria-checked', 'true');
  dialog.showModal();
}

async function saveCalendar(submit) {
  submit.preventDefault();
  const dialog = byId('calendar-dialog');
  const id = dialog.dataset.calendar;
  const chosen = byId('cal-swatches').querySelector('[aria-checked="true"]');
  const body = { name: byId('cal-name').value, color: chosen ? chosen.dataset.colour : COLOURS[0] };
  try {
    if (id) await api('PATCH', `/api/calendars/${encodeURIComponent(id)}`, body);
    else await api('POST', '/api/calendars', body);
    dialog.close();
    await loadCalendars();
    render();
  } catch (error) {
    showError('cal-error', error);
  }
}

async function deleteCalendar() {
  const id = byId('calendar-dialog').dataset.calendar;
  const calendar = calendarById(id);
  if (!calendar) return;
  const sure = await askConfirm(`Delete ${calendar.name}?`, 'Every event in it is deleted too, here and on synced phones.', 'Delete calendar');
  if (!sure) return;
  try {
    await api('DELETE', `/api/calendars/${encodeURIComponent(id)}`);
    byId('calendar-dialog').close();
    await loadCalendars();
    await loadEvents();
  } catch (error) {
    showError('cal-error', error);
  }
}

function startImport() {
  byId('import-file').value = '';
  byId('import-file').click();
}

function chooseImportTarget() {
  const file = byId('import-file').files[0];
  if (!file) return;
  const limit = state.session.importLimits.bytes;
  byId('import-file-name').textContent = file.size > limit
    ? `${file.name} is larger than ${Math.round(limit / 1048576)} MB, too large to import.` : file.name;
  byId('import-go').disabled = file.size > limit;
  byId('import-result').textContent = '';
  byId('import-dialog').showModal();
}

async function runImport() {
  const file = byId('import-file').files[0];
  if (!file) return;
  const button = byId('import-go');
  button.disabled = true;
  byId('import-result').textContent = 'Importing...';
  try {
    const text = await file.text();
    const calendar = byId('import-calendar').value;
    const result = await api('POST', `/api/import?calendar=${encodeURIComponent(calendar)}`, undefined, { raw: text });
    const parts = [`${result.imported} imported`];
    if (result.alreadyThere) parts.push(`${result.alreadyThere} already there`);
    if (result.skipped) parts.push(`${result.skipped} skipped (not events)`);
    if (result.refused.length) parts.push(`${result.refused.length} refused: ${result.refused.map((item) => `${item.title} (${item.reason})`).join('; ')}`);
    byId('import-result').textContent = parts.join(', ');
    await loadEvents();
  } catch (error) {
    byId('import-result').textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function stopLinkPolling() {
  clearTimeout(state.linkTimer);
  state.linkTimer = null;
}

function renderArty() {
  const arty = state.session.arty;
  const status = byId('arty-status');
  byId('arty-link').hidden = !arty.configured || arty.linked;
  byId('arty-test').hidden = !arty.linked;
  byId('arty-unlink').hidden = !arty.linked;
  if (!arty.configured) status.textContent = 'Arty is not set up for this calendar (ARTY_URL is empty), so reminders show only while this page is open.';
  else if (arty.linked) status.textContent = 'Reminders go to Arty, which sends them to Updates in the phone and desktop apps. They also show here while the page is open.';
  else status.textContent = 'Reminders show only while this page is open. Link Arty to get them on your phone too.';
}

async function startArtyLink() {
  try {
    const started = await api('POST', '/api/arty/link');
    const code = byId('arty-code');
    code.hidden = false;
    code.replaceChildren(element('span', '', 'Approve code '), element('code', '', started.userCode), element('span', '', ' on '));
    const link = element('a', '', 'Arty devices in starfire');
    link.href = `${state.session.arty.approveUrl}?code=${encodeURIComponent(started.userCode)}`;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    code.append(link);
    const giveUpAt = Date.now() + LINK_GIVE_UP_MS;
    const poll = async () => {
      try {
        const answer = await api('GET', '/api/arty/link');
        if (answer.status === 'waiting' && Date.now() < giveUpAt && byId('settings').open) {
          state.linkTimer = setTimeout(poll, LINK_POLL_MS);
          return;
        }
        code.hidden = true;
        state.session.arty.linked = answer.status === 'linked';
        toast(answer.status === 'linked' ? 'Arty will send your reminders' : `Linking stopped: ${answer.status}`, answer.status !== 'linked');
        renderArty();
      } catch (error) {
        failed(error);
      }
    };
    state.linkTimer = setTimeout(poll, LINK_POLL_MS);
  } catch (error) {
    failed(error);
  }
}

async function unlinkArty() {
  const sure = await askConfirm('Stop sending reminders through Arty?', 'Remove the "Calendar reminders" device in starfire too, so its key stops working.', 'Stop');
  if (!sure) return;
  try {
    await api('DELETE', '/api/arty/link');
    state.session.arty.linked = false;
    renderArty();
  } catch (error) {
    failed(error);
  }
}

async function testArty() {
  try {
    const answer = await api('POST', '/api/arty/test');
    toast(`Arty: ${answer.result}`);
  } catch (error) {
    failed(error);
  }
}

function renderBrowserStatus() {
  const supported = 'Notification' in window;
  const permission = supported ? Notification.permission : 'unsupported';
  byId('browser-allow').hidden = permission !== 'default';
  const texts = {
    granted: 'This browser shows reminders as notifications while the calendar is open in a tab.',
    denied: 'This browser blocks notifications from the calendar; reminders appear as a banner in the page instead.',
    default: 'Allow notifications to see reminders even while this tab is in the background.',
    unsupported: 'This browser has no notifications; reminders appear as a banner in the page.',
  };
  byId('browser-status').textContent = texts[permission];
}

async function loadPasswords() {
  const data = await api('GET', '/api/app-passwords');
  const list = byId('password-list');
  list.replaceChildren();
  if (!data.passwords.length) list.append(element('li', 'faint', 'No app passwords yet.'));
  for (const password of data.passwords) {
    const row = element('li', 'password-row');
    const used = password.lastUsedAt ? `last used ${new Date(password.lastUsedAt * 1000).toLocaleString('en-GB')}` : 'not used yet';
    const text = element('span', 'password-text');
    text.append(element('strong', '', password.name), element('span', 'faint', ` · made ${new Date(password.createdAt * 1000).toLocaleDateString('en-GB')}, ${used}`));
    const revoke = element('button', 'btn small-btn', 'Revoke');
    revoke.type = 'button';
    revoke.addEventListener('click', async () => {
      if (!await askConfirm(`Revoke ${password.name}?`, 'That device stops syncing at once.', 'Revoke')) return;
      try {
        await api('DELETE', `/api/app-passwords/${password.id}`);
        await loadPasswords();
      } catch (error) {
        failed(error);
      }
    });
    row.append(text, revoke);
    list.append(row);
  }
}

async function createPassword() {
  try {
    const made = await api('POST', '/api/app-passwords', { name: byId('pw-name').value });
    byId('pw-name').value = '';
    byId('pw-value').textContent = made.password;
    byId('pw-fresh').hidden = false;
    await loadPasswords();
  } catch (error) {
    failed(error);
  }
}

async function copyPassword() {
  try {
    await navigator.clipboard.writeText(byId('pw-value').textContent);
    toast('Copied');
  } catch (error) {
    toast('Copying is blocked here; select the password and copy it by hand', true);
  }
}

async function openSettings() {
  const caldav = state.session.caldav;
  byId('caldav-status').textContent = caldav.enabled
    ? 'Phone sync is on. Each phone signs in with its own app password; revoke one and that phone stops syncing.'
    : 'Phone sync is switched off on the server for now (CALDAV_ENABLED=false). You can make app passwords ready; they work once it is switched on.';
  byId('caldav-url').textContent = caldav.url;
  byId('caldav-user').textContent = caldav.user;
  byId('pw-fresh').hidden = true;
  byId('pw-value').textContent = '';
  byId('arty-code').hidden = true;
  byId('version-line').textContent = `Calendar ${state.session.version}, times in ${state.session.timeZone}`;
  renderArty();
  renderBrowserStatus();
  byId('settings').showModal();
  try {
    await loadPasswords();
  } catch (error) {
    failed(error);
  }
}

function showReminder(reminder) {
  byId('banner-text').textContent = `${reminder.title}: ${reminder.message}`;
  byId('banner').hidden = false;
  if ('Notification' in window && Notification.permission === 'granted') {
    try {
      new Notification(reminder.title, { body: reminder.message, icon: '/static/icon.svg', tag: `calendar-${reminder.id}` });
    } catch (error) {
      return;
    }
  }
}

async function pollReminders() {
  try {
    const data = await api('GET', `/api/reminders?after=${state.reminderAfter}`);
    for (const reminder of data.reminders) showReminder(reminder);
    state.reminderAfter = R.nextReminderCursor(state.reminderAfter, data.latest);
    writeStored(STORE_REMINDER_AFTER, String(state.reminderAfter));
  } catch (error) {
    return;
  } finally {
    setTimeout(pollReminders, REMINDER_POLL_MS);
  }
}

function toggleSidebar(open) {
  const sidebar = byId('sidebar');
  const shouldOpen = open === undefined ? !sidebar.classList.contains('open') : open;
  sidebar.classList.toggle('open', shouldOpen);
  byId('scrim').hidden = !shouldOpen;
  byId('menu-btn').setAttribute('aria-expanded', String(shouldOpen));
}

function isTyping(target) {
  return target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName));
}

function onKey(press) {
  if (press.ctrlKey || press.metaKey || press.altKey || isTyping(press.target) || document.querySelector('dialog[open]')) return;
  const actions = {
    t: () => goTo(state.session.today),
    j: () => step(1),
    k: () => step(-1),
    ArrowRight: () => step(1),
    ArrowLeft: () => step(-1),
    m: () => setView('month'),
    w: () => setView('week'),
    d: () => setView('day'),
    a: () => setView('agenda'),
    n: () => openEditor(null, newEventAt(state.anchor, NEW_EVENT_HOUR * 60)),
    '/': () => byId('quick-text').focus(),
    '?': () => openSettings(),
  };
  const action = actions[press.key];
  if (!action) return;
  press.preventDefault();
  action();
}

function remember() {
  try {
    state.hidden = new Set(JSON.parse(readStored(STORE_HIDDEN, '[]')));
  } catch (error) {
    state.hidden = new Set();
  }
  const storedView = readStored(STORE_VIEW, '');
  state.view = VIEWS.includes(storedView) ? storedView : (isPhone() ? 'agenda' : 'month');
  state.reminderAfter = Number(readStored(STORE_REMINDER_AFTER, '0')) || 0;
}

function wire() {
  byId('today-btn').addEventListener('click', () => goTo(state.session.today));
  byId('prev-btn').addEventListener('click', () => step(-1));
  byId('next-btn').addEventListener('click', () => step(1));
  for (const button of document.querySelectorAll('.seg')) button.addEventListener('click', () => setView(button.dataset.view));
  byId('settings-btn').addEventListener('click', openSettings);
  byId('menu-btn').addEventListener('click', () => toggleSidebar());
  byId('scrim').addEventListener('click', () => toggleSidebar(false));
  const createNew = () => {
    toggleSidebar(false);
    openEditor(null, newEventAt(state.view === 'month' && state.anchor.slice(0, 7) === state.session.today.slice(0, 7) ? state.session.today : state.anchor, NEW_EVENT_HOUR * 60));
  };
  byId('create-btn').addEventListener('click', createNew);
  byId('fab').addEventListener('click', createNew);
  byId('new-calendar').addEventListener('click', () => openCalendarDialog(null));
  byId('calendar-form').addEventListener('submit', saveCalendar);
  byId('cal-delete').addEventListener('click', deleteCalendar);
  byId('import-btn').addEventListener('click', startImport);
  byId('import-file').addEventListener('change', chooseImportTarget);
  byId('import-go').addEventListener('click', runImport);
  byId('quick-text').addEventListener('input', schedulePreview);
  byId('quick-add').addEventListener('submit', submitQuickAdd);
  byId('editor-form').addEventListener('submit', saveEditor);
  byId('ed-cancel').addEventListener('click', () => byId('editor').close());
  byId('ed-delete').addEventListener('click', deleteFromEditor);
  byId('ed-allday').addEventListener('change', () => {
    syncAllDay();
    const allDay = byId('ed-allday').checked;
    fillReminders(R.remindersAfterAllDayToggle(editorReminders(), allDay), allDay);
  });
  byId('ed-repeat').addEventListener('change', syncRepeat);
  byId('ed-interval').addEventListener('input', syncRepeat);
  byId('ed-ends').addEventListener('change', syncRepeat);
  byId('ed-add-reminder').addEventListener('click', () => byId('ed-reminders').append(reminderRow(byId('ed-allday').checked)));
  for (const id of ['ed-start-date', 'ed-start-time']) {
    byId(id).addEventListener('change', () => {
      shiftEndWithStart(state.editorStart);
      fillMonthly(byId('ed-start-date').value, byId('ed-monthly').value);
      noteEditorStart();
    });
  }
  byId('arty-link').addEventListener('click', startArtyLink);
  byId('arty-unlink').addEventListener('click', unlinkArty);
  byId('arty-test').addEventListener('click', testArty);
  byId('browser-allow').addEventListener('click', async () => {
    await Notification.requestPermission();
    renderBrowserStatus();
  });
  byId('settings').addEventListener('close', stopLinkPolling);
  byId('pw-create').addEventListener('click', createPassword);
  byId('pw-copy').addEventListener('click', copyPassword);
  byId('banner-close').addEventListener('click', () => { byId('banner').hidden = true; });
  document.addEventListener('keydown', onKey);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadEvents(); });
  window.addEventListener('resize', () => { if (state.view === 'month') render(); });
  setInterval(loadEvents, REFRESH_MS);
  setInterval(() => { if (state.view === 'week' || state.view === 'day') render(); }, NOW_LINE_MS);
}

function updateReminderNote() {
  byId('reminder-note').textContent = state.session.arty.linked
    ? 'Reminders go to Arty and show here.' : 'Reminders show while this page is open. Settings has more.';
}

async function start() {
  try {
    state.session = await api('GET', '/api/session');
  } catch (error) {
    byId('view').append(element('p', 'empty-state', `Could not start: ${error.message}`));
    return;
  }
  remember();
  state.anchor = state.session.today;
  wire();
  for (const button of document.querySelectorAll('.seg')) button.setAttribute('aria-pressed', String(button.dataset.view === state.view));
  updateReminderNote();
  try {
    await loadCalendars();
  } catch (error) {
    failed(error);
  }
  render();
  await loadEvents();
  pollReminders();
}

start();
