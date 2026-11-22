"""Google Calendar access through the user's own OAuth client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


class CalendarNotConnected(RuntimeError):
    pass


@dataclass(frozen=True)
class Event:
    id: str
    title: str
    start: datetime | None  # None for all-day events
    end: datetime | None
    all_day_date: str | None
    location: str
    link: str

    def describe(self) -> str:
        when = f"all day {self.all_day_date}" if self.all_day_date else (
            f"{self.start:%a %Y-%m-%d %H:%M}-{self.end:%H:%M}" if self.start and self.end else "?"
        )
        where = f" @ {self.location}" if self.location else ""
        return f"{when}  {self.title}{where}  [id={self.id}]"


def authorize(client_secret: Path, token_path: Path) -> None:
    """Run the browser consent flow on a machine with a browser and save the token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)


def is_connected(token_path: Path) -> bool:
    return token_path.exists()


def _service(token_path: Path):
    if not token_path.exists():
        raise CalendarNotConnected("Google Calendar is not connected (no google_token.json).")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        else:
            raise CalendarNotConnected("Google token is invalid; run `friday google-auth` again.")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse(item: dict, tz) -> Event:
    s, e = item.get("start", {}), item.get("end", {})
    start = datetime.fromisoformat(s["dateTime"]).astimezone(tz) if "dateTime" in s else None
    end = datetime.fromisoformat(e["dateTime"]).astimezone(tz) if "dateTime" in e else None
    return Event(
        id=item["id"],
        title=item.get("summary", "(no title)"),
        start=start,
        end=end,
        all_day_date=s.get("date"),
        location=item.get("location", ""),
        link=item.get("hangoutLink", "") or item.get("htmlLink", ""),
    )


def list_events(token_path: Path, start: datetime, end: datetime, tz, calendar_id: str = "primary") -> list[Event]:
    svc = _service(token_path)
    items, page = [], None
    while True:
        resp = svc.events().list(
            calendarId=calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
            pageToken=page,
        ).execute()
        items.extend(resp.get("items", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    return [_parse(i, tz) for i in items if i.get("status") != "cancelled"]


def create_event(
    token_path: Path,
    title: str,
    start: datetime,
    end: datetime,
    description: str = "",
    location: str = "",
    calendar_id: str = "primary",
) -> dict:
    body = {
        "summary": title,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    return _service(token_path).events().insert(calendarId=calendar_id, body=body).execute()


def delete_event(token_path: Path, event_id: str, calendar_id: str = "primary") -> None:
    _service(token_path).events().delete(calendarId=calendar_id, eventId=event_id).execute()
