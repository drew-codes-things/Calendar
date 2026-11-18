import base64
import http.client
import logging
import threading
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

from .errors import CalendarError

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CALENDARSERVER = "http://calendarserver.org/ns/"
APPLE = "http://apple.com/ns/ical/"
STORE_TIMEOUT_SECONDS = 20
MAX_STORE_ANSWER_BYTES = 64 * 1024 * 1024
OK_STATUSES = (200, 201, 204, 207)
EVENT_COMPONENT = "VEVENT"
UNSAFE_SEGMENTS = ("", ".", "..")
UNORDERED = 999

log = logging.getLogger("calendar.davstore")

LIST_CALENDARS_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    f'<D:propfind xmlns:D="{DAV}" xmlns:C="{CALDAV}" xmlns:CS="{CALENDARSERVER}" xmlns:I="{APPLE}">'
    "<D:prop><D:resourcetype/><D:displayname/><CS:getctag/><I:calendar-color/><I:calendar-order/>"
    "<C:supported-calendar-component-set/></D:prop></D:propfind>"
)
ALL_EVENTS_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    f'<C:calendar-query xmlns:D="{DAV}" xmlns:C="{CALDAV}">'
    '<D:prop><D:getetag/><C:calendar-data/></D:prop>'
    '<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT"/></C:comp-filter></C:filter>'
    "</C:calendar-query>"
)


class StoreError(CalendarError):
    pass


class ChangedElsewhere(CalendarError):
    def __init__(self):
        super().__init__("this event was changed somewhere else (perhaps on a phone) a moment ago; reload and try again", 409)


@dataclass(frozen=True)
class CalendarInfo:
    name: str
    display_name: str
    color: str
    ctag: str
    order: int


@dataclass(frozen=True)
class StoredItem:
    name: str
    etag: str
    text: str


@dataclass
class _CachedCalendar:
    ctag: str
    items: list[StoredItem] = field(default_factory=list)


def is_safe_segment(name: str) -> bool:
    return isinstance(name, str) and name not in UNSAFE_SEGMENTS and "/" not in name and "\\" not in name \
        and name.isprintable() and len(name) <= 255


def _tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _text_of(element) -> str:
    return "" if element is None or element.text is None else element.text


def _last_segment(href: str) -> str:
    return unquote(urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1])


def _ok_props(response):
    for propstat in response.findall(_tag(DAV, "propstat")):
        status = _text_of(propstat.find(_tag(DAV, "status")))
        if " 200 " in status:
            prop = propstat.find(_tag(DAV, "prop"))
            if prop is not None:
                yield prop


def _holds_events(prop) -> bool:
    supported = prop.find(_tag(CALDAV, "supported-calendar-component-set"))
    if supported is None:
        return True
    return any(comp.get("name", "").upper() == EVENT_COMPONENT for comp in supported)


def _calendar_from(response) -> CalendarInfo | None:
    href = _text_of(response.find(_tag(DAV, "href")))
    found = {}
    for prop in _ok_props(response):
        for child in prop:
            found[child.tag] = child
    resource_type = found.get(_tag(DAV, "resourcetype"))
    if resource_type is None or resource_type.find(_tag(CALDAV, "calendar")) is None:
        return None
    holder = ElementTree.Element("prop")
    holder.extend(found.values())
    if not _holds_events(holder):
        return None
    name = _last_segment(href)
    order_text = _text_of(found.get(_tag(APPLE, "calendar-order"))).strip()
    return CalendarInfo(
        name=name,
        display_name=_text_of(found.get(_tag(DAV, "displayname"))).strip() or name,
        color=_text_of(found.get(_tag(APPLE, "calendar-color"))).strip(),
        ctag=_text_of(found.get(_tag(CALENDARSERVER, "getctag"))),
        order=int(order_text) if order_text.isdigit() else UNORDERED,
    )


def _items_from(document) -> list[StoredItem]:
    items = []
    for response in document.findall(_tag(DAV, "response")):
        href = _text_of(response.find(_tag(DAV, "href")))
        for prop in _ok_props(response):
            data = prop.find(_tag(CALDAV, "calendar-data"))
            if data is not None and data.text:
                items.append(StoredItem(_last_segment(href), _text_of(prop.find(_tag(DAV, "getetag"))), data.text))
    return items


def _property_xml(display_name: str | None, color: str | None, order: int | None = None) -> str:
    parts = []
    if order is not None:
        parts.append(f"<I:calendar-order>{order}</I:calendar-order>")
    if display_name is not None:
        parts.append(f"<D:displayname>{escape(display_name)}</D:displayname>")
    if color is not None:
        parts.append(f"<I:calendar-color>{escape(color)}</I:calendar-color>")
    return "".join(parts)


class DavStore:
    def __init__(self, base_url: str, user: str, password: str, timeout: float = STORE_TIMEOUT_SECONDS):
        parts = urlsplit(base_url)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 80
        self.user = user
        self.timeout = timeout
        credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.authorization = f"Basic {credentials}"
        self.lock = threading.Lock()
        self.cache: dict[str, _CachedCalendar] = {}
        self.skipped_names: set[str] = set()

    def _home(self) -> str:
        return f"/{quote(self.user)}/"

    def _calendar_path(self, calendar: str) -> str:
        if not is_safe_segment(calendar):
            raise CalendarError("that calendar does not exist", 404)
        return f"{self._home()}{quote(calendar, safe='')}/"

    def _item_path(self, calendar: str, item: str) -> str:
        if not is_safe_segment(item):
            raise CalendarError("that event does not exist", 404)
        return f"{self._calendar_path(calendar)}{quote(item, safe='')}"

    def _send(self, method: str, path: str, body: str | bytes | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        payload = body.encode("utf-8") if isinstance(body, str) else body
        sent_headers = {"Authorization": self.authorization, **(headers or {})}
        try:
            connection.request(method, path, body=payload, headers=sent_headers)
            response = connection.getresponse()
            data = response.read(MAX_STORE_ANSWER_BYTES + 1)
        except (OSError, http.client.HTTPException) as error:
            raise StoreError(f"the calendar store did not answer ({error.__class__.__name__}); is calendar-store running?", 503) from error
        finally:
            connection.close()
        if len(data) > MAX_STORE_ANSWER_BYTES:
            raise StoreError("the calendar store sent more than this app reads at once", 502)
        if response.status == 401:
            raise StoreError("the calendar store refused this app's password; restart calendar-store so it reads the current one", 502)
        return response.status, response.headers, data

    def _xml(self, method: str, path: str, body: str, depth: str):
        status, _, data = self._send(method, path, body, {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"})
        if status == 404:
            raise CalendarError("that calendar does not exist", 404)
        if status != 207:
            raise StoreError(f"the calendar store answered {status} to {method}", 502)
        try:
            return ElementTree.fromstring(data)
        except ElementTree.ParseError as error:
            raise StoreError("the calendar store sent XML this app could not read", 502) from error

    def calendars(self) -> list[CalendarInfo]:
        document = self._xml("PROPFIND", self._home(), LIST_CALENDARS_BODY, "1")
        found = [_calendar_from(response) for response in document.findall(_tag(DAV, "response"))]
        usable = [info for info in found if info is not None and self._is_usable(info)]
        return sorted(usable, key=lambda info: (info.order, info.display_name.lower()))

    def _is_usable(self, info: CalendarInfo) -> bool:
        if is_safe_segment(info.name):
            return True
        with self.lock:
            first_time = info.name not in self.skipped_names
            self.skipped_names.add(info.name)
        if first_time:
            log.warning("skipped the calendar %r made elsewhere: this app cannot use that name in its addresses", info.name)
        return False

    def calendar(self, name: str) -> CalendarInfo:
        for info in self.calendars():
            if info.name == name:
                return info
        raise CalendarError("that calendar does not exist", 404)

    def create_calendar(self, display_name: str, color: str, order: int | None = None) -> str:
        name = uuid.uuid4().hex
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<C:mkcalendar xmlns:D="{DAV}" xmlns:C="{CALDAV}" xmlns:I="{APPLE}"><D:set><D:prop>'
            f"{_property_xml(display_name, color, order)}"
            f'<C:supported-calendar-component-set><C:comp name="{EVENT_COMPONENT}"/></C:supported-calendar-component-set>'
            "</D:prop></D:set></C:mkcalendar>"
        )
        status, _, _ = self._send("MKCALENDAR", self._calendar_path(name), body, {"Content-Type": "application/xml; charset=utf-8"})
        if status != 201:
            raise StoreError(f"the calendar store answered {status} when making a calendar", 502)
        return name

    def change_calendar(self, name: str, display_name: str | None, color: str | None) -> None:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<D:propertyupdate xmlns:D="{DAV}" xmlns:I="{APPLE}"><D:set><D:prop>'
            f"{_property_xml(display_name, color)}</D:prop></D:set></D:propertyupdate>"
        )
        self._xml("PROPPATCH", self._calendar_path(name), body, "0")
        self._forget(name)

    def delete_calendar(self, name: str) -> None:
        status, _, _ = self._send("DELETE", self._calendar_path(name))
        if status == 404:
            raise CalendarError("that calendar does not exist", 404)
        if status not in OK_STATUSES:
            raise StoreError(f"the calendar store answered {status} when deleting a calendar", 502)
        self._forget(name)

    def _forget(self, name: str) -> None:
        with self.lock:
            self.cache.pop(name, None)

    def items(self, info: CalendarInfo) -> list[StoredItem]:
        with self.lock:
            cached = self.cache.get(info.name)
            if cached is not None and cached.ctag == info.ctag and info.ctag:
                return cached.items
        document = self._xml("REPORT", self._calendar_path(info.name), ALL_EVENTS_BODY, "1")
        items = _items_from(document)
        with self.lock:
            self.cache[info.name] = _CachedCalendar(info.ctag, items)
        return items

    def item(self, calendar: str, item: str) -> StoredItem:
        status, headers, data = self._send("GET", self._item_path(calendar, item))
        if status == 404:
            raise CalendarError("that event does not exist", 404)
        if status != 200:
            raise StoreError(f"the calendar store answered {status} when reading an event", 502)
        return StoredItem(item, headers.get("ETag", ""), data.decode("utf-8", "replace"))

    def put(self, calendar: str, item: str, text: str, etag: str | None) -> str:
        condition = {"If-Match": etag} if etag else {"If-None-Match": "*"}
        status, headers, data = self._send("PUT", self._item_path(calendar, item), text,
                                           {"Content-Type": "text/calendar; charset=utf-8", **condition})
        if status == 412:
            raise ChangedElsewhere()
        if status == 409:
            raise CalendarError("an event with the same UID is already in that calendar", 409)
        if status not in (200, 201, 204):
            detail = data.decode("utf-8", "replace").strip()[:200]
            raise StoreError(f"the calendar store refused the event ({status}): {detail}", 400 if status == 400 else 502)
        self._forget(calendar)
        return headers.get("ETag", "")

    def delete(self, calendar: str, item: str, etag: str | None) -> None:
        headers = {"If-Match": etag} if etag else {}
        status, _, _ = self._send("DELETE", self._item_path(calendar, item), headers=headers)
        if status == 412:
            raise ChangedElsewhere()
        if status == 404:
            raise CalendarError("that event does not exist", 404)
        if status not in OK_STATUSES:
            raise StoreError(f"the calendar store answered {status} when deleting an event", 502)
        self._forget(calendar)

    def is_reachable(self) -> bool:
        try:
            self.calendars()
        except CalendarError:
            return False
        return True
