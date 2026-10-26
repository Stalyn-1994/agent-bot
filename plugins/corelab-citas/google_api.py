"""Google Calendar REST client: fixed Google endpoints, bounded I/O, no redirects or logging of tokens."""
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .common import Credentials

SCOPES = ["https://www.googleapis.com/auth/calendar.events.owned",
          "https://www.googleapis.com/auth/calendar.calendars.readonly"]
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_credential_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status):
        self.status = int(status)
        super().__init__("Google Calendar devolvio HTTP " + str(self.status))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http(method, url, data=None, headers=None):
    request = Request(url, data=data, headers=headers or {}, method=method)
    try:
        with build_opener(NoRedirect).open(request, timeout=10) as response:
            payload = response.read(4_000_000)
            return json.loads(payload) if payload else {}
    except HTTPError as exc:
        # Do not echo error bodies or request URLs; either can contain private information.
        raise ApiError(exc.code) from None
    except (URLError, TimeoutError, OSError):
        raise ApiError(0) from None


class Google:
    def __init__(self, directory, calendar_id, *, credentials=None, transport=None):
        self.credentials = credentials or Credentials(directory / "google-token.bin")
        self.calendar_id = calendar_id
        self.transport = transport or http
        self.base = "https://www.googleapis.com/calendar/v3/calendars/" + quote(calendar_id, safe="")

    def token(self, force=False):
        with _credential_lock:
            stored = self.credentials.read()
            if not force and stored.get("access_token") and stored.get("expires_at", 0) > time.time() + 60:
                return stored["access_token"]
            response = self.transport("POST", TOKEN_URL, urlencode({
                "client_id": stored["client_id"], "client_secret": stored["client_secret"],
                "refresh_token": stored["refresh_token"], "grant_type": "refresh_token"}).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"})
            if not response.get("access_token"):
                raise RuntimeError("Google no devolvio un token de acceso")
            stored.update(access_token=response["access_token"],
                          expires_at=time.time() + int(response.get("expires_in", 3600)))
            if response.get("refresh_token"):
                stored["refresh_token"] = response["refresh_token"]
            self.credentials.write(stored)
            return stored["access_token"]

    def request(self, method, suffix="", body=None, params=None):
        url = self.base + suffix
        if params:
            url += "?" + urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(2):
            token = self.token(force=attempt > 0)
            try:
                return self.transport(method, url, data,
                    {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            except ApiError as exc:
                if exc.status != 401 or attempt:
                    raise

    def metadata(self):
        return self.request("GET")

    def events(self, start, end):
        result, page = [], None
        for _ in range(10):
            params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                      "singleEvents": "true", "showDeleted": "false", "maxResults": 2500}
            if page:
                params["pageToken"] = page
            response = self.request("GET", "/events", params=params)
            result.extend(response.get("items", []))
            page = response.get("nextPageToken")
            if not page:
                return result
        raise RuntimeError("Demasiados eventos para verificar los cupos de forma segura")

    def event(self, event_id):
        return self.request("GET", "/events/" + quote(event_id, safe=""))

    def create(self, body):
        return self.request("POST", "/events", body)

    def delete(self, event_id):
        try:
            self.request("DELETE", "/events/" + quote(event_id, safe=""))
        except ApiError as exc:
            if exc.status not in {404, 410}:
                raise
