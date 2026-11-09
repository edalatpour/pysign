import json
import logging
import queue
import threading
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from .storage import read_json, write_json

logger = logging.getLogger(__name__)


class APIError(Exception):
    pass


class CredentialRevoked(APIError):
    pass


class RestartRequested(Exception):
    pass


class ResetRequested(Exception):
    pass


def validate_command(data, command):
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        raise ValueError(f"Invalid player {command} request.")
    request_id = str(UUID(data["id"]))
    if request_id != data["id"] or not isinstance(data.get("expires_at"), str):
        raise ValueError(f"Invalid player {command} request.")
    expires = datetime.fromisoformat(data["expires_at"])
    if expires.tzinfo is None:
        raise ValueError(f"Player {command} expiry must include a timezone.")
    return {"id": request_id, "expires_at": expires.isoformat()}


def validate_restart(data):
    return validate_command(data, "restart")


def command_receipt(state_dir, server, command):
    receipt = read_json(state_dir / f"{command}.json")
    if receipt is not None and (
        not isinstance(receipt, dict) or receipt.get("server") != server
        or not isinstance(receipt.get("id"), str)
        or str(UUID(receipt["id"])) != receipt["id"]
        or type(receipt.get("acknowledged")) is not bool
    ):
        raise ValueError(f"Invalid saved {command} receipt. Reset locally before changing servers.")
    return receipt


def restart_pending(data):
    return datetime.fromisoformat(data["expires_at"]) > datetime.now(timezone.utc)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never forward the device bearer credential elsewhere.
        return None


def server_url(value, allow_http=False):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https") or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or parsed.path not in ("", "/")
    ):
        raise ValueError("Server must be an absolute HTTP(S) origin without credentials or a path.")
    if parsed.scheme != "https" and not allow_http:
        raise ValueError("HTTPS is required. Use --allow-http only on a trusted development network.")
    return value.rstrip("/")


def validate_config(data):
    if not isinstance(data, dict) or data.get("state") not in ("paired", "unpaired"):
        raise ValueError("Invalid configuration state.")
    slides = data.get("slides")
    if not isinstance(slides, list):
        raise ValueError("Configuration slides must be a list.")
    validated = []
    for slide in slides:
        if not isinstance(slide, dict):
            raise ValueError("Each slide must be an object.")
        url, duration, title = slide.get("url"), slide.get("duration"), slide.get("title")
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError("Invalid slide URL.")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
        ):
            raise ValueError("Slides must use HTTP(S) URLs without embedded credentials.")
        if type(duration) is not int or not 1 <= duration <= 86400:
            raise ValueError("Slide duration must be 1 to 86400 seconds.")
        if not isinstance(title, str) or len(title) > 100:
            raise ValueError("Invalid slide title.")
        validated.append({"title": title, "url": url, "duration": duration})
    if data["state"] == "unpaired" and validated:
        raise ValueError("An unpaired device cannot have a playlist.")
    config = {"state": data["state"], "slides": validated}
    if data["state"] == "paired" and "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("Device name must contain 1 to 100 characters.")
        config["name"] = name
    return config


class API:
    def __init__(self, server):
        self.server = server
        # Ignore environment proxy settings for the credential-bearing device channel.
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def call(self, path, token=None, data=None):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.server + path, data=body, headers=headers)
        try:
            with self.opener.open(request, timeout=10) as response:
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise APIError("Server response is too large.")
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise APIError("Expected a JSON object from the server.")
                return result
        except HTTPError as exc:
            if exc.code == 401:
                raise CredentialRevoked("Device credential was revoked. Reset locally to pair again.") from exc
            raise APIError(f"Server returned HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise APIError(f"Cannot retrieve device configuration: {type(exc).__name__}.") from exc


class ConfigWorker(threading.Thread):
    def __init__(self, server, state_dir, name, interval=30):
        super().__init__(name="pisign-config", daemon=True)
        self.server, self.state_dir, self.name_hint = server, state_dir, name
        self.interval = interval
        self.updates = queue.Queue(maxsize=1)
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.api = API(server)
        self.restart_receipt = command_receipt(state_dir, server, "restart")
        self.reset_receipt = command_receipt(state_dir, server, "reset")

    def publish(self, event):
        try:
            self.updates.get_nowait()
        except queue.Empty:
            pass
        self.updates.put_nowait(event)

    def credentials(self):
        path = self.state_dir / "credentials.json"
        credentials = read_json(path)
        if credentials is None:
            result = self.api.call("/api/devices/register/", data={"name": self.name_hint})
            token = result.get("token")
            if not isinstance(token, str) or not 32 <= len(token) <= 128:
                raise ValueError("Invalid registration credential.")
            credentials = {"server": self.server, "token": token}
            write_json(path, credentials)
        if not isinstance(credentials, dict) or credentials.get("server") != self.server:
            raise ValueError("Saved credentials belong to another server. Reset locally first.")
        token = credentials.get("token")
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ValueError("Saved credentials are invalid. Reset locally first.")
        return token

    def acknowledge_restart(self, token):
        receipt = self.restart_receipt
        if receipt is None or receipt["acknowledged"] or not self.ready.is_set():
            return
        try:
            result = self.api.call(
                f"/api/device/restarts/{receipt['id']}/completed/", token=token, data={},
            )
            if result.get("status") not in ("completed", "expired", "obsolete"):
                raise ValueError("Invalid player restart acknowledgement.")
            updated = {**receipt, "acknowledged": True}
            write_json(self.state_dir / "restart.json", updated)
            self.restart_receipt = updated
            logger.info("Player restart %s acknowledgement: %s.", receipt["id"], result["status"])
        except CredentialRevoked:
            raise
        except APIError as exc:
            logger.warning("Cannot acknowledge player restart: %s Retrying in %s seconds.", exc, self.interval)

    def run(self):
        while not self.stopping.is_set():
            try:
                token = self.credentials()
                if self.stopping.is_set():
                    return
                if self.reset_receipt is not None and not self.reset_receipt["acknowledged"]:
                    receipt = self.reset_receipt
                    result = self.api.call(
                        f"/api/device/resets/{receipt['id']}/completed/", token=token, data={},
                    )
                    if result.get("status") not in ("completed", "obsolete"):
                        raise ValueError("Invalid player reset acknowledgement.")
                    updated = {**receipt, "acknowledged": True}
                    write_json(self.state_dir / "reset.json", updated)
                    self.reset_receipt = updated
                    logger.info("Player reset %s acknowledgement: %s.", receipt["id"], result["status"])
                response = self.api.call("/api/device/config/", token=token)
                if self.stopping.is_set():
                    return
                config = validate_config(response)
                if config["state"] == "paired" and "reset" in response:
                    reset = validate_command(response["reset"], "reset")
                    if restart_pending(reset) and (
                        self.reset_receipt is None or self.reset_receipt["id"] != reset["id"]
                    ):
                        self.publish(("config", {**config, "reset": reset}))
                        self.stopping.wait(self.interval)
                        continue
                if config["state"] == "unpaired":
                    (self.state_dir / "playlist.json").unlink(missing_ok=True)
                    pairing = self.api.call("/api/device/pairing/", token=token, data={})
                    code = pairing.get("code")
                    if not isinstance(code, str) or len(code) != 10 or not code.isalnum():
                        raise ValueError("Invalid pairing code.")
                    config["code"] = code
                else:
                    write_json(self.state_dir / "playlist.json", {"server": self.server, "config": config})
                    self.acknowledge_restart(token)
                    if "restart" in response:
                        restart = validate_restart(response["restart"])
                        if restart_pending(restart) and (
                            self.restart_receipt is None or self.restart_receipt["id"] != restart["id"]
                        ):
                            config["restart"] = restart
                self.publish(("config", config))
            except CredentialRevoked as exc:
                (self.state_dir / "playlist.json").unlink(missing_ok=True)
                logger.error("%s", exc)
                self.publish(("fatal", str(exc)))
                return
            except APIError as exc:
                # Polling is separate from playback, so an outage cannot stall rotation.
                logger.warning("%s Retrying in %s seconds.", exc, self.interval)
                self.publish(("offline", str(exc)))
            except (OSError, ValueError) as exc:
                logger.error("Player state/configuration error: %s", exc)
                self.publish(("fatal", str(exc)))
                return
            self.stopping.wait(self.interval)
