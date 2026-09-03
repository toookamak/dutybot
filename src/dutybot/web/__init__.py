"""Localhost Web UI: login, watch list, thresholds, journal. No privileged actions."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

from dutybot.config import (
    Config,
    WatchService,
    parse_probe,
    slug_id,
    validate_service_id,
    validate_unit_name,
    verify_password,
)
from dutybot import services

log = logging.getLogger("dutybot.web")

SESSION_TTL = 12 * 3600
COOKIE = "dutybot_session"

def _web_secret(cfg: Config) -> bytes:
    path = cfg.state / "web-secret"
    if path.is_file():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    val = os.urandom(32)
    path.write_bytes(val)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return val


def _sign_sid(cfg: Config, sid: str) -> str:
    sig = hmac.new(_web_secret(cfg), sid.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{sid}.{sig}"


def _parse_sid(cfg: Config, raw: str) -> str:
    if not raw:
        return ""
    if "." not in raw:
        return raw
    sid, sig = raw.rsplit(".", 1)
    expect = hmac.new(_web_secret(cfg), sid.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return ""
    return sid



_TEMPLATES = Path(__file__).resolve().parent / "templates"
_STATIC = Path(__file__).resolve().parent / "static"


def _sessions_dir(cfg: Config) -> Path:
    d = cfg.state / "web-sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_csrf() -> str:
    return secrets.token_urlsafe(24)


def _save_session(cfg: Config, sid: str, data: dict) -> None:
    path = _sessions_dir(cfg) / sid
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_session(cfg: Config, sid: str) -> Optional[dict]:
    if not sid or not all(c.isalnum() or c in "-_" for c in sid):
        return None
    path = _sessions_dir(cfg) / sid
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(data.get("ts", 0)) > SESSION_TTL:
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return data


def _drop_session(cfg: Config, sid: str) -> None:
    if not sid:
        return
    path = _sessions_dir(cfg) / sid
    try:
        path.unlink()
    except OSError:
        pass


def _jinja() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )


def html_response(text: str, *, status: int = 200) -> web.Response:
    """aiohttp 3.10+ forbids charset inside content_type."""
    return web.Response(text=text, status=status, content_type="text/html", charset="utf-8")


def create_app(cfg: Config) -> web.Application:
    env = _jinja()
    app = web.Application()
    app["cfg"] = cfg
    app["jinja"] = env

    def render(name: str, **ctx) -> str:
        return env.get_template(name).render(**ctx)

    def current_session(request: web.Request) -> Optional[dict]:
        sid = _parse_sid(cfg, request.cookies.get(COOKIE, ""))
        return _load_session(cfg, sid)

    def require_login(handler):
        async def wrapped(request: web.Request):
            sess = current_session(request)
            if not sess:
                raise web.HTTPFound("/login")
            request["session"] = sess
            request["session_id"] = _parse_sid(cfg, request.cookies.get(COOKIE, ""))
            return await handler(request)

        return wrapped

    async def login_get(request: web.Request) -> web.Response:
        if current_session(request):
            raise web.HTTPFound("/")
        html = render("login.html", error=None)
        return html_response(html)

    async def login_post(request: web.Request) -> web.Response:
        form = await request.post()
        user = str(form.get("username") or "")
        password = str(form.get("password") or "")
        ok_user = hmac.compare_digest(user.encode("utf-8"), cfg.web_user.encode("utf-8"))
        ok_pw = verify_password(password, cfg.web_password_hash)
        if not (ok_user and ok_pw):
            html = render("login.html", error="用户名或口令错误")
            return html_response(html, status=401)
        sid = secrets.token_urlsafe(32)
        _save_session(cfg, sid, {"user": cfg.web_user, "ts": time.time(), "csrf": _new_csrf()})
        resp = web.HTTPFound("/")
        resp.set_cookie(COOKIE, _sign_sid(cfg, sid), httponly=True, samesite="Lax", max_age=SESSION_TTL, path="/")
        raise resp

    async def logout_any(request: web.Request) -> web.Response:
        sid = _parse_sid(cfg, request.cookies.get(COOKIE, ""))
        _drop_session(cfg, sid)
        resp = web.HTTPFound("/login")
        resp.del_cookie(COOKIE, path="/")
        raise resp

    @require_login
    async def index(request: web.Request) -> web.Response:
        watch = cfg.reload_watch()
        html = render(
            "watch.html",
            user=cfg.web_user,
            services=watch.services,
            csrf=request["session"].get("csrf"),
            error=request.rel_url.query.get("e"),
            notice=request.rel_url.query.get("n"),
            telegram_on=cfg.telegram_ok,
        )
        return html_response(html)

    def _csrf_ok(form, sess) -> bool:
        got = str(form.get("csrf") or "")
        exp = str(sess.get("csrf") or "")
        return bool(got) and hmac.compare_digest(got, exp)

    @require_login
    async def watch_add(request: web.Request) -> web.Response:
        form = await request.post()
        if not _csrf_ok(form, request["session"]):
            raise web.HTTPFound("/?e=csrf")
        unit = str(form.get("unit") or "").strip()
        name = str(form.get("name") or "").strip()
        probe_raw = str(form.get("probe") or "").strip()
        sid = str(form.get("id") or "").strip() or slug_id(unit)
        watch = cfg.reload_watch()
        try:
            if not validate_unit_name(unit):
                raise ValueError("unit 名无效")
            if not validate_service_id(sid):
                sid = slug_id(unit)
            probe = parse_probe(probe_raw or None)
            watch.add(WatchService(id=sid, name=name or sid, unit=unit, probe=probe))
            watch.save()
        except ValueError as exc:
            raise web.HTTPFound(f"/?e={str(exc)}")
        raise web.HTTPFound("/?n=added")

    @require_login
    async def watch_edit(request: web.Request) -> web.Response:
        form = await request.post()
        if not _csrf_ok(form, request["session"]):
            raise web.HTTPFound("/?e=csrf")
        sid = str(form.get("id") or "").strip()
        watch = cfg.reload_watch()
        svc = watch.by_id(sid)
        if not svc:
            raise web.HTTPFound("/?e=missing")
        unit = str(form.get("unit") or svc.unit).strip()
        name = str(form.get("name") or svc.name).strip()
        probe_raw = str(form.get("probe") or "")
        try:
            if not validate_unit_name(unit):
                raise ValueError("unit 名无效")
            probe = parse_probe(probe_raw or None)
            watch.replace(WatchService(id=sid, name=name or sid, unit=unit, probe=probe))
            watch.save()
        except ValueError as exc:
            raise web.HTTPFound(f"/?e={str(exc)}")
        raise web.HTTPFound("/?n=saved")

    @require_login
    async def watch_delete(request: web.Request) -> web.Response:
        form = await request.post()
        if not _csrf_ok(form, request["session"]):
            raise web.HTTPFound("/?e=csrf")
        sid = str(form.get("id") or "").strip()
        watch = cfg.reload_watch()
        watch.remove_id(sid)
        watch.save()
        raise web.HTTPFound("/?n=deleted")

    @require_login
    async def settings_get(request: web.Request) -> web.Response:
        watch = cfg.reload_watch()
        html = render(
            "settings.html",
            user=cfg.web_user,
            th=watch.merged_thresholds(),
            csrf=request["session"].get("csrf"),
            telegram_on=cfg.telegram_ok,
            notice=request.rel_url.query.get("n"),
        )
        return html_response(html)

    @require_login
    async def settings_post(request: web.Request) -> web.Response:
        form = await request.post()
        if not _csrf_ok(form, request["session"]):
            raise web.HTTPFound("/thresholds?n=csrf")
        watch = cfg.reload_watch()
        keys = list(watch.merged_thresholds().keys())
        new_th = dict(watch.thresholds or {})
        for k in keys:
            if k in form:
                raw = str(form.get(k) or "").strip()
                if raw == "":
                    continue
                try:
                    new_th[k] = int(raw) if "." not in raw else float(raw)
                except ValueError:
                    continue
        watch.thresholds = new_th
        watch.save()
        raise web.HTTPFound("/thresholds?n=saved")

    @require_login
    async def logs_get(request: web.Request) -> web.Response:
        watch = cfg.reload_watch()
        units = ["dutybot.service"] + [s.unit for s in watch.services]
        chosen = request.rel_url.query.get("unit") or "dutybot.service"
        if chosen not in units:
            chosen = "dutybot.service"
        hours_s = request.rel_url.query.get("hours") or ""
        try:
            hours = int(hours_s) if hours_s else 1
        except ValueError:
            hours = 1
        hours = max(1, min(hours, 168))
        since = request.rel_url.query.get("since") or f"-{hours}h"
        if hours_s:
            since = f"-{hours}h"
        prio = request.rel_url.query.get("priority") or ""
        text, err = services.read_journal([chosen], since=since, priority=prio or None, lines=200)
        html = render(
            "logs.html",
            user=cfg.web_user,
            units=units,
            chosen=chosen,
            since=since,
            hours=hours,
            priority=prio,
            log_text=text or "",
            error=err,
            csrf=request["session"].get("csrf"),
            telegram_on=cfg.telegram_ok,
        )
        return html_response(html)

    app.router.add_get("/login", login_get)
    app.router.add_post("/login", login_post)
    app.router.add_get("/logout", logout_any)
    app.router.add_post("/logout", logout_any)
    app.router.add_get("/", index)
    app.router.add_post("/watch/add", watch_add)
    app.router.add_post("/watch/edit", watch_edit)
    app.router.add_post("/watch/delete", watch_delete)
    app.router.add_get("/settings", settings_get)
    app.router.add_post("/settings", settings_post)
    app.router.add_get("/thresholds", settings_get)
    app.router.add_post("/thresholds", settings_post)
    app.router.add_get("/logs", logs_get)
    if _STATIC.is_dir():
        app.router.add_static("/static", str(_STATIC))
    return app


async def start_web(cfg: Config):
    if not cfg.web_ok:
        log.info("Web 未配置（WEB_USER 为空），不监听 HTTP")
        return None
    bind = (cfg.web_bind or "").strip()
    if bind in {"0.0.0.0", "::", "[::]", "*"}:
        log.error("WEB_BIND=%s 拒绝监听，请改用 127.0.0.1 并经反向代理", bind)
        return None
    app = create_app(cfg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, bind, cfg.web_port)
    await site.start()
    log.info("Web 监听 http://%s:%s", bind, cfg.web_port)
    return runner
