"""End-to-end booking flow for the Warwick Web Room Booking system.

Wizard shape (all one ASP.NET page posting back to Book.aspx):

    1/2/3  filters + calendar + times   -> ShowOptionsBtn
    4      grid of available rooms      -> SelectOptionButton
    5      booking details form         -> MakeBookingBtn  (postback, not submit)
    6      confirmation
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from .client import WRBClient, WRBError

GRID_ID = "ctl00_Main_OptionSelector_OptionsGrid"

# Signals read off the page after confirming.
BOOKED_RE = re.compile(r"has been reserved for you|booking requested", re.I)
REF_RE = re.compile(r"\b(BK[A-Z0-9]{4,10})\b")
LIMIT_RE = re.compile(r"maximum .{0,30}booking|reached the maximum|too many bookings",
                      re.I)


@dataclass
class Option:
    """One bookable room/time offered on the options grid."""
    option_id: str
    checkbox: str
    room: str
    size: int
    time: str
    description: str
    provisional: bool = False

    def __str__(self):
        flag = " [provisional]" if self.provisional else ""
        return "%s  %-22s size=%-4s %s%s" % (
            self.time, self.room, self.size, self.description, flag)


@dataclass
class BookingRequest:
    day: date
    start: str = "14:00"
    end: str = "15:00"
    size: int = 4
    reason: str = "Group study session"
    zone: str = None                 # label, e.g. "Main Site"
    suitabilities: list = field(default_factory=list)
    rooms: list = field(default_factory=list)   # ordered preference, substrings
    telephone: str = ""


class Booker:
    def __init__(self, client=None, **kw):
        self.c = client or WRBClient(**kw)

    # ------------------------------------------------------------- helpers
    def _opt_value(self, suffix, label):
        """Map a visible option label (e.g. '14:00') to its <option> value."""
        for value, text in self.c.options(suffix):
            if text.strip() == label.strip():
                return value
        raise WRBError("no option %r in %s (have: %s)" % (
            label, suffix, [t for _, t in self.c.options(suffix)][:15]))

    def _size_value(self, wanted):
        """Snap a group size to a value the dropdown actually offers.

        The list is sparse (1-5, then 10, 15, 20, 30, 40, ...); ASP.NET event
        validation rejects anything else outright, so asking for 6 people has
        to become 10 rather than blowing up mid-run.
        """
        sizes = []
        for value, _ in self.c.options("Room1$ReqSize"):
            try:
                sizes.append(int(value))
            except (TypeError, ValueError):
                continue
        if not sizes:
            return str(wanted)
        bigger = [s for s in sorted(sizes) if s >= int(wanted)]
        return str(bigger[0] if bigger else max(sizes))

    # ------------------------------------------------------------- step 1-3
    def search(self, req):
        """Fill the filter/date/time page and return the offered options."""
        c = self.c
        c.select_date(req.day)

        extra = {
            c.control("Room1$ReqSize"): self._size_value(req.size),
            c.control("Time1$StartTimeList"): self._opt_value("Time1$StartTimeList", req.start),
            c.control("Time1$EndTimeList"): self._opt_value("Time1$EndTimeList", req.end),
        }
        dur = self._duration_label(req.start, req.end)
        if dur:
            try:
                extra[c.control("Time1$DurList")] = self._opt_value("Time1$DurList", dur)
            except WRBError:
                pass
        if req.zone:
            extra[c.control("Room1$ZoneList")] = self._opt_value("Room1$ZoneList", req.zone)
        if req.suitabilities:
            extra[c.control("Room1$SuitabilityList")] = [
                self._opt_value("Room1$SuitabilityList", s) for s in req.suitabilities]

        c.click(c.control("ShowOptionsBtn"), extra=extra)
        return self.options()

    @staticmethod
    def _duration_label(start, end):
        try:
            s = datetime.strptime(start, "%H:%M")
            e = datetime.strptime(end, "%H:%M")
        except ValueError:
            return None
        hrs = int((e - s).total_seconds() // 3600)
        return "%d:00" % hrs if hrs > 0 else None

    def options(self):
        """Parse the options grid on the current page."""
        grid = self.c.page.find("table", id=GRID_ID)
        if grid is None:
            msg = self.c.text()
            if "no options" in msg.lower() or "not available" in msg.lower():
                return []
            raise WRBError("options grid missing; page says: %s" % msg[:300])
        out = []
        for tr in grid.find_all("tr")[1:]:
            cb = tr.find("input", attrs={"type": "checkbox"})
            if not cb:
                continue
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            try:
                size = int(tds[5])
            except (IndexError, ValueError):
                size = 0
            out.append(Option(
                option_id=cb.get("value"), checkbox=cb.get("name"),
                room=tds[3] if len(tds) > 3 else "?", size=size,
                time=tds[1] if len(tds) > 1 else "?",
                description=tds[6] if len(tds) > 6 else "",
                provisional="P" in (tds[8] if len(tds) > 8 else "")))
        return out

    @staticmethod
    def pick(options, prefer=(), strict=False):
        """Choose an option, honouring the preference order.

        With strict=True a preference list is a whitelist: if none of the wanted
        rooms is free we book nothing rather than grabbing a room the user did
        not ask for.
        """
        for want in prefer:
            for o in options:
                if want.lower() in o.room.lower():
                    return o
        if strict and prefer:
            return None
        return max(options, key=lambda o: o.size) if options else None

    # --------------------------------------------------------------- step 4
    def select(self, option):
        c = self.c
        c.click(c.control("SelectOptionButton"), extra={
            option.checkbox: option.option_id,
            c.control("OptionSelector$SelectedItem"): option.option_id,
            c.control("OptionSelector$ItemsCount"): "1",
        })
        if not c.control("BookingForm1$meaningfulName"):
            title = c.page.title.get_text(strip=True) if c.page.title else "?"
            raise WRBError("did not reach the booking details form (got %r)" % title)
        c.log("[select] on details form for", option.room)
        return self.details()

    def details(self):
        """Current values of the booking details form, keyed by short name."""
        out = {}
        for tag in self.c.page.find_all(["input", "select", "textarea"]):
            n = tag.get("name") or ""
            if "BookingForm1$" not in n:
                continue
            short = n.split("BookingForm1$")[1]
            if tag.name == "select":
                sel = [o.get("value") for o in tag.find_all("option")
                       if o.has_attr("selected")]
                out[short] = sel[0] if sel else None
            elif tag.name == "textarea":
                out[short] = tag.get_text(strip=True)
            else:
                out[short] = tag.get("value", "")
        return out

    # --------------------------------------------------------------- step 5
    def confirm(self, req, dry_run=True):
        """Fill the mandatory declarations and (unless dry_run) submit."""
        c = self.c
        ctl = lambda s: c.control("BookingForm1$" + s)
        extra = {
            ctl("meaningfulName"): req.reason[:35],
            ctl("FoodDrink"): "Yes",       # "food and drink will not be taken in"
            ctl("Layout"): "Yes",          # "furniture cannot be moved"
            ctl("acceptConditions"): "Yes",
            ctl("SocietyClub"): "Yes",     # this account only ever books on behalf of a society
        }
        if req.telephone:
            extra[ctl("tel")] = req.telephone
        extra = {k: v for k, v in extra.items() if k}

        if dry_run:
            c.log("[dry-run] would submit:",
                  {k.split("$")[-1]: v for k, v in extra.items()})
            return None

        c.postback("ctl00$Main$MakeBookingBtn", extra=extra)
        return self.result()

    def result(self):
        """Classify the page reached after clicking Confirm Reservation."""
        txt = self.c.text()
        errs = [e for e in self.c.errors() if e.strip()]
        ok = bool(BOOKED_RE.search(txt)) and not errs
        ref = None
        m = REF_RE.search(txt)
        if m:
            ref = m.group(1)
        limited = bool(LIMIT_RE.search(txt))
        return {"ok": ok, "reference": ref, "limit_hit": limited,
                "errors": errs, "text": txt}

    # ------------------------------------------------------------- bookings
    def my_bookings(self):
        c = self.c
        c._absorb(c.s.get(c.base + "MyBookings.aspx"))
        rows = []
        for t in c.page.find_all("table"):
            for tr in t.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(cells) >= 4 and any(
                        re.search(r"\d{2}/\d{2}/\d{4}", x) for x in cells):
                    rows.append(cells)
        return rows
