"""Manual backup/restore client for the optional SQLite companion service."""

import json
import sys

from storage import validate_profile


CONFIG_NAME = "IR_COMPANION_URL"
PROFILE_ENDPOINT = "/api/v1/profile"
MAX_URL_LENGTH = 160
REQUEST_TIMEOUT_SECONDS = 8


class CompanionSyncError(RuntimeError):
    pass


def load_configured_url(settings_module=None):
    """Read ``IR_COMPANION_URL`` from the badge's root ``secrets.py``."""

    if settings_module is None:
        inserted = False
        try:
            sys.path.insert(0, "/")
            inserted = True
            import secrets as settings_module
        except Exception:
            return None
        finally:
            if inserted:
                try:
                    sys.path.pop(0)
                except (IndexError, ValueError):
                    pass
    value = getattr(settings_module, CONFIG_NAME, None)
    if not isinstance(value, str):
        return None
    value = value.strip().rstrip("/")
    if not value:
        return None
    return value


def validate_base_url(value):
    if not isinstance(value, str):
        raise CompanionSyncError("companion URL is not configured")
    value = value.strip().rstrip("/")
    if not value or len(value) > MAX_URL_LENGTH:
        raise CompanionSyncError("companion URL has an invalid length")
    if not (value.startswith("http://") or value.startswith("https://")):
        raise CompanionSyncError("companion URL must start with http:// or https://")
    if any(character in " \t\r\n" for character in value):
        raise CompanionSyncError("companion URL cannot contain spaces")
    return value


def _load_requests():
    try:
        import requests

        return requests
    except ImportError:
        try:
            import urequests

            return urequests
        except ImportError:
            raise CompanionSyncError("HTTP requests module is unavailable")


class CompanionSync:
    def __init__(self, base_url=None, request_module=None):
        if base_url is None:
            base_url = load_configured_url()
        self.base_url = None if base_url is None else validate_base_url(base_url)
        self._requests = request_module

    @property
    def configured(self):
        return self.base_url is not None

    @property
    def endpoint(self):
        if not self.configured:
            raise CompanionSyncError(
                "set %s in /secrets.py" % CONFIG_NAME
            )
        return self.base_url + PROFILE_ENDPOINT

    def _module(self):
        if self._requests is None:
            self._requests = _load_requests()
        return self._requests

    def _decode(self, response):
        try:
            status = getattr(response, "status_code", None)
            if status is None:
                status = getattr(response, "status", 0)
            if int(status) != 200:
                raise CompanionSyncError("companion returned HTTP %s" % status)
            json_method = getattr(response, "json", None)
            if json_method is not None:
                payload = json_method()
            else:
                payload = json.loads(response.text)
            if not isinstance(payload, dict) or "data" not in payload:
                raise CompanionSyncError("companion response is malformed")
            return payload["data"]
        except CompanionSyncError:
            raise
        except Exception as error:
            raise CompanionSyncError("invalid companion response: " + str(error))
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()

    def _request(self, method_name, **kwargs):
        method = getattr(self._module(), method_name)
        socket_module = None
        setter = None
        previous_timeout = None
        try:
            import socket as socket_module

            setter = getattr(socket_module, "setdefaulttimeout", None)
            getter = getattr(socket_module, "getdefaulttimeout", None)
            if setter is not None:
                previous_timeout = getter() if getter is not None else None
                setter(REQUEST_TIMEOUT_SECONDS)
        except (ImportError, AttributeError, OSError):
            setter = None

        try:
            try:
                timeout_kwargs = dict(kwargs)
                timeout_kwargs["timeout"] = REQUEST_TIMEOUT_SECONDS
                return method(self.endpoint, **timeout_kwargs)
            except TypeError:
                # requests/urequests builds without a timeout keyword still
                # inherit the bounded socket default above.
                return method(self.endpoint, **kwargs)
        finally:
            if setter is not None:
                try:
                    setter(previous_timeout)
                except (AttributeError, OSError, TypeError):
                    pass

    def push_profile(self, profile):
        profile = validate_profile(profile)
        encoded = json.dumps(profile).encode("utf-8")
        try:
            response = self._request(
                "put",
                data=encoded,
                headers={"Content-Type": "application/json"},
            )
        except Exception as error:
            raise CompanionSyncError("backup connection failed: " + str(error))
        acknowledgement = self._decode(response)
        if (
            not isinstance(acknowledgement, dict)
            or acknowledgement.get("stored") is not True
            or not isinstance(acknowledgement.get("device_count"), int)
        ):
            raise CompanionSyncError("companion acknowledgement is malformed")
        return acknowledgement

    def pull_profile(self):
        try:
            response = self._request("get")
        except Exception as error:
            raise CompanionSyncError("restore connection failed: " + str(error))
        return validate_profile(self._decode(response))
