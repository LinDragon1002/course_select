import time

from django.conf import settings
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.sessions.backends.base import UpdateError
from django.utils.cache import patch_vary_headers
from django.utils.http import http_date


ADMIN_SESSION_COOKIE_NAME = "ntub_admin_sessionid"
ADMIN_SESSION_COOKIE_PATH = "/admin/"


class PathAwareSessionMiddleware(SessionMiddleware):
    """Keep frontend and Django Admin authentication in separate cookies."""

    @staticmethod
    def _is_admin_request(request):
        return request.path == "/admin" or request.path.startswith("/admin/")

    def _cookie_name(self, request):
        if self._is_admin_request(request):
            return ADMIN_SESSION_COOKIE_NAME
        return settings.SESSION_COOKIE_NAME

    def _cookie_path(self, request):
        if self._is_admin_request(request):
            return ADMIN_SESSION_COOKIE_PATH
        return settings.SESSION_COOKIE_PATH

    def process_request(self, request):
        cookie_name = self._cookie_name(request)
        request._session_cookie_name = cookie_name
        request._session_cookie_path = self._cookie_path(request)
        session_key = request.COOKIES.get(cookie_name)
        request.session = self.SessionStore(session_key)

    def process_response(self, request, response):
        try:
            accessed = request.session.accessed
            modified = request.session.modified
            empty = request.session.is_empty()
            cookie_name = request._session_cookie_name
            cookie_path = request._session_cookie_path
        except AttributeError:
            return response

        if cookie_name in request.COOKIES and empty:
            response.delete_cookie(
                cookie_name,
                path=cookie_path,
                domain=settings.SESSION_COOKIE_DOMAIN,
                samesite=settings.SESSION_COOKIE_SAMESITE,
            )
            need_vary_cookie = True
        else:
            need_vary_cookie = accessed
            if (modified or settings.SESSION_SAVE_EVERY_REQUEST) and not empty:
                if request.session.get_expire_at_browser_close():
                    max_age = None
                    expires = None
                else:
                    max_age = request.session.get_expiry_age()
                    expires = http_date(time.time() + max_age)

                if response.status_code < 500:
                    try:
                        request.session.save()
                    except UpdateError as exc:
                        raise SessionInterrupted(
                            "The request's session was deleted before the "
                            "request completed. The user may have logged "
                            "out in a concurrent request, for example."
                        ) from exc
                    response.set_cookie(
                        cookie_name,
                        request.session.session_key,
                        max_age=max_age,
                        expires=expires,
                        domain=settings.SESSION_COOKIE_DOMAIN,
                        path=cookie_path,
                        secure=settings.SESSION_COOKIE_SECURE or None,
                        httponly=settings.SESSION_COOKIE_HTTPONLY or None,
                        samesite=settings.SESSION_COOKIE_SAMESITE,
                    )
                    need_vary_cookie = True

        if need_vary_cookie:
            patch_vary_headers(response, ("Cookie",))
        return response
