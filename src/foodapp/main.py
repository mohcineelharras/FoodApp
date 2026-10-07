"""HTTP routes for the pickup counter."""

from __future__ import annotations

import html
import logging
import sqlite3
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.templating import Jinja2Templates

from foodapp.accounts import authenticate, get_user, register
from foodapp.catalog import list_categories, list_menu
from foodapp.config import Settings, configure_logging, load_settings
from foodapp.db import init_db, open_db
from foodapp.errors import AppError, OrderError
from foodapp.format import format_cents
from foodapp.middleware import BodyLimitMiddleware, SecurityHeadersMiddleware
from foodapp.ordering import (
    add_item,
    cart_lines,
    get_order,
    list_orders,
    place_order,
    remove_item,
    update_qty,
)
from foodapp.security import (
    clean_search,
    client_ip,
    new_token,
    parse_item_id,
    parse_qty,
    safe_next,
    tokens_match,
)
from foodapp.sessions import (
    Session,
    delete_session,
    dump_cart,
    load_existing,
    load_or_create,
    set_checkout_nonce,
    set_flash,
    take_flash,
)

logger = logging.getLogger("foodapp")

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
templates.env.filters["quote"] = lambda value: quote(str(value), safe="")

_ERROR_COPY = {
    400: ("Bad request", "The request could not be processed."),
    403: ("Not allowed", "Refresh the page and try again."),
    404: ("Page not found", "That page is not on the menu."),
    405: ("Not available", "That action is not available."),
    413: ("Too large", "That submission is too large."),
    429: ("Slow down", "Too many attempts. Wait a few minutes and try again."),
    500: ("Something went wrong", "The kitchen hit a snag. Try again."),
}

def _fallback_page(heading: str, message: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"en\"><meta charset=\"utf-8\"><title>"
        + html.escape(heading)
        + "</title><body><h1>"
        + html.escape(heading)
        + "</h1><p>"
        + html.escape(message)
        + '</p><p><a href="/">Back to the menu</a></p></body></html>'
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    active = load_settings(settings or Settings.from_env())
    init_db(active.db_path)
    app = FastAPI(title="FoodApp", docs_url=None, redoc_url=None, openapi_url=None, debug=False)
    app.state.settings = active

    app.add_middleware(BodyLimitMiddleware, max_bytes=active.max_body_bytes)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(active.allowed_hosts),
        www_redirect=False,
    )
    app.add_middleware(SecurityHeadersMiddleware, settings=active)

    @app.exception_handler(AppError)
    def handle_app_error(request: Request, exc: AppError) -> Response:
        return _error_page(request, exc.status_code, exc.heading, exc.message)

    @app.exception_handler(HTTPException)
    def handle_http_error(request: Request, exc: HTTPException) -> Response:
        heading, message = _ERROR_COPY.get(exc.status_code, _ERROR_COPY[500])
        return _error_page(request, exc.status_code, heading, message)

    @app.exception_handler(RequestValidationError)
    def handle_validation(request: Request, _exc: RequestValidationError) -> Response:
        heading, message = _ERROR_COPY[400]
        return _error_page(request, 400, heading, message)

    @app.exception_handler(Exception)
    def handle_unexpected(request: Request, exc: Exception) -> Response:
        logger.error("unhandled error", exc_info=exc)
        heading, message = _ERROR_COPY[500]
        return _error_page(request, 500, heading, message)

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            with open_db(active.db_path) as conn:
                conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/")
    def menu(request: Request) -> Response:
        settings = _settings(request)
        category_arg = request.query_params.get("category", "")
        search = clean_search(request.query_params.get("q", ""))
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            user = get_user(conn, session.user_id)
            flash = take_flash(conn, session)
            categories = list_categories(conn)
            category = category_arg if category_arg in categories else ""
            rows = list_menu(conn, category or None, search)
            items = [_menu_item(row, session.cart) for row in rows]
        if items:
            empty_message = ""
        elif search:
            empty_message = f"No dishes match “{search}”."
        elif category:
            empty_message = f"Nothing in {category} right now."
        else:
            empty_message = "The menu is empty."
        category_links = [
            {"label": name, "href": _menu_href(name, search), "selected": name == category}
            for name in categories
        ]
        return _html(
            request,
            "menu.html",
            session,
            token,
            user,
            flash,
            title="Menu",
            active="menu",
            items=items,
            categories=category_links,
            category=category,
            search=search,
            all_href=_menu_href("", search),
            return_to=_menu_href(category, search),
            empty_message=empty_message,
        )

    @app.get("/cart")
    def cart(request: Request) -> Response:
        settings = _settings(request)
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            user = get_user(conn, session.user_id)
            flash = take_flash(conn, session)
            lines, total, ready = cart_lines(conn, session.cart)
        return _html(
            request,
            "cart.html",
            session,
            token,
            user,
            flash,
            title="Cart",
            active="cart",
            lines=lines,
            total=format_cents(total),
            ready=ready,
        )

    @app.post("/cart/add")
    def cart_add(request: Request, form: dict[str, str] = Depends(form_data)) -> Response:
        settings = _settings(request)
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            _require_csrf(session, form)
            token_hash_value = session.token_hash
        message = add_item(settings.db_path, token_hash_value, _require_item_id(form.get("item_id")))
        with open_db(settings.db_path) as conn:
            set_flash(conn, token_hash_value, message)
        return _redirect(safe_next(form.get("return_to"), "/"), settings, token)

    @app.post("/cart/update")
    def cart_update(request: Request, form: dict[str, str] = Depends(form_data)) -> Response:
        settings = _settings(request)
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            _require_csrf(session, form)
            token_hash_value = session.token_hash
        item_id = _require_item_id(form.get("item_id"))
        qty = parse_qty(form.get("qty"))
        if qty is None:
            message = "Choose a quantity between 1 and 20."
        else:
            message = update_qty(settings.db_path, token_hash_value, item_id, qty)
        with open_db(settings.db_path) as conn:
            set_flash(conn, token_hash_value, message)
        return _redirect("/cart", settings, token)

    @app.post("/cart/remove")
    def cart_remove(request: Request, form: dict[str, str] = Depends(form_data)) -> Response:
        settings = _settings(request)
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            _require_csrf(session, form)
            token_hash_value = session.token_hash
        message = remove_item(settings.db_path, token_hash_value, _require_item_id(form.get("item_id")))
        with open_db(settings.db_path) as conn:
            set_flash(conn, token_hash_value, message)
        return _redirect("/cart", settings, token)

    @app.get("/checkout")
    def checkout(request: Request) -> Response:
        settings = _settings(request)
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            user = get_user(conn, session.user_id)
            flash = take_flash(conn, session)
            lines, total, ready = cart_lines(conn, session.cart)
            nonce = ""
            if user and lines and ready:
                nonce = new_token()
                set_checkout_nonce(conn, session.token_hash, nonce, dump_cart(session.cart))
        return _html(
            request,
            "checkout.html",
            session,
            token,
            user,
            flash,
            title="Checkout",
            active="cart",
            lines=lines,
            total=format_cents(total),
            ready=ready,
            nonce=nonce,
            pickup_name=user["name"] if user else "",
        )

    @app.post("/checkout")
    def checkout_submit(request: Request, form: dict[str, str] = Depends(form_data)) -> Response:
        settings = _settings(request)
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            _require_csrf(session, form)
            if session.user_id is None:
                set_flash(conn, session.token_hash, "Sign in to place this pickup order.")
                return _redirect("/login?next=/checkout", settings, token)
            token_hash_value = session.token_hash
        try:
            order_id = place_order(
                settings.db_path,
                token_hash_value,
                form.get("pickup_name", ""),
                form.get("note", ""),
                form.get("checkout_nonce", ""),
            )
        except OrderError as exc:
            with open_db(settings.db_path) as conn:
                set_flash(conn, token_hash_value, exc.message)
            return _redirect("/checkout", settings, token)
        with open_db(settings.db_path) as conn:
            set_flash(conn, token_hash_value, f"Order {order_id} is in. We'll hold it at the counter.")
        return _redirect(f"/orders/{order_id}", settings, token)

    @app.get("/orders")
    def orders(request: Request) -> Response:
        settings = _settings(request)
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            if session.user_id is None:
                return _redirect("/login?next=/orders", settings, token)
            user = get_user(conn, session.user_id)
            flash = take_flash(conn, session)
            history = list_orders(conn, session.user_id)
        return _html(
            request,
            "orders.html",
            session,
            token,
            user,
            flash,
            title="Orders",
            active="orders",
            orders=history,
        )

    @app.get("/orders/{order_id}")
    def order_detail(request: Request, order_id: str) -> Response:
        settings = _settings(request)
        if not order_id.isdigit():
            raise AppError(404, *_ERROR_COPY[404])
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            if session.user_id is None:
                return _redirect(f"/login?next=/orders/{int(order_id)}", settings, token)
            user = get_user(conn, session.user_id)
            flash = take_flash(conn, session)
            order = get_order(conn, session.user_id, int(order_id))
        if order is None:
            raise AppError(404, *_ERROR_COPY[404])
        return _html(
            request,
            "order.html",
            session,
            token,
            user,
            flash,
            title=f"Order {order['id']}",
            active="orders",
            order=order,
        )

    @app.get("/login")
    def login_form(request: Request) -> Response:
        settings = _settings(request)
        nxt = safe_next(request.query_params.get("next"))
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            if session.user_id is not None:
                return _redirect(nxt, settings, token)
            user = None
            flash = take_flash(conn, session)
        return _html(
            request,
            "login.html",
            session,
            token,
            user,
            flash,
            title="Sign in",
            active="",
            next_url=nxt,
            email="",
            error="",
        )

    @app.post("/login")
    def login_submit(request: Request, form: dict[str, str] = Depends(form_data)) -> Response:
        settings = _settings(request)
        nxt = safe_next(form.get("next"))
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            _require_csrf(session, form)
        result = authenticate(
            settings,
            session,
            email=form.get("email", ""),
            password=form.get("password", ""),
            ip=client_ip(request, settings),
        )
        if result.limited:
            raise AppError(429, *_ERROR_COPY[429])
        if result.raw_token:
            return _redirect(nxt, settings, result.raw_token, max_age=settings.session_ttl)
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            user = None
            flash = take_flash(conn, session)
        return _html(
            request,
            "login.html",
            session,
            token,
            user,
            flash,
            status_code=401,
            title="Sign in",
            active="",
            next_url=nxt,
            email=result.email,
            error=result.error or "Email or password is incorrect.",
        )

    @app.get("/register")
    def register_form(request: Request) -> Response:
        settings = _settings(request)
        nxt = safe_next(request.query_params.get("next"))
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            if session.user_id is not None:
                return _redirect(nxt, settings, token)
            flash = take_flash(conn, session)
        return _html(
            request,
            "register.html",
            session,
            token,
            None,
            flash,
            title="Create account",
            active="",
            next_url=nxt,
            name="",
            email="",
            error="",
        )

    @app.post("/register")
    def register_submit(request: Request, form: dict[str, str] = Depends(form_data)) -> Response:
        settings = _settings(request)
        nxt = safe_next(form.get("next"))
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            _require_csrf(session, form)
        result = register(
            settings,
            session,
            name=form.get("name", ""),
            email=form.get("email", ""),
            password=form.get("password", ""),
            password_confirm=form.get("password_confirm", ""),
            ip=client_ip(request, settings),
        )
        if result.limited:
            raise AppError(429, *_ERROR_COPY[429])
        if result.raw_token:
            return _redirect(nxt, settings, result.raw_token, max_age=settings.session_ttl)
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            flash = take_flash(conn, session)
        return _html(
            request,
            "register.html",
            session,
            token,
            None,
            flash,
            status_code=400,
            title="Create account",
            active="",
            next_url=nxt,
            name=result.name,
            email=result.email,
            error=result.error or "Check the form and try again.",
        )

    @app.get("/account")
    def account(request: Request) -> Response:
        settings = _settings(request)
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            if session.user_id is None:
                return _redirect("/login?next=/account", settings, token)
            user = get_user(conn, session.user_id)
            flash = take_flash(conn, session)
        return _html(
            request,
            "account.html",
            session,
            token,
            user,
            flash,
            title="Account",
            active="account",
        )

    @app.post("/logout")
    def logout(request: Request, form: dict[str, str] = Depends(form_data)) -> Response:
        settings = _settings(request)
        with open_db(settings.db_path) as conn:
            session, token = load_or_create(
                conn, request.cookies.get("session"), settings, client_ip(request, settings)
            )
            _require_csrf(session, form)
            delete_session(conn, session.token_hash)
        return _redirect("/", settings, token=None, clear=True)

    if active.env == "test":

        @app.get("/__test__/boom")
        def boom() -> Response:
            raise RuntimeError("secret boom marker")

    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    return app


async def form_data(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" not in content_type and "multipart/form-data" not in content_type:
        raise AppError(400, "Unsupported request", "Submit the form from the FoodApp page.")
    form = await request.form()
    parsed: dict[str, str] = {}
    for index, (key, value) in enumerate(form.multi_items()):
        if index >= 16:
            raise AppError(400, "Unsupported request", "Submit the form from the FoodApp page.")
        if not isinstance(key, str) or not isinstance(value, str):
            raise AppError(400, "Unsupported request", "Submit the form from the FoodApp page.")
        if len(key) > 40 or len(value) > 2000:
            raise AppError(400, "Unsupported request", "Submit the form from the FoodApp page.")
        parsed[key] = value
    return parsed


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _require_csrf(session: Session, form: dict[str, str]) -> None:
    if not tokens_match(session.csrf_token, form.get("csrf_token", "")):
        raise AppError(403, "Form expired", "Refresh the page and submit it again.")


def _require_item_id(value: object) -> int:
    item_id = parse_item_id(value)
    if item_id is None:
        raise AppError(400, "Bad request", "That dish is not on the menu.")
    return item_id


def _menu_href(category: str, search: str) -> str:
    params: dict[str, str] = {}
    if category:
        params["category"] = category
    if search:
        params["q"] = search
    if not params:
        return "/"
    return "/?" + urlencode(params)


def _menu_item(row, cart: dict[int, int]) -> dict[str, object]:
    stock = int(row["stock"])
    available = int(row["available"]) == 1
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "category": row["category"],
        "price": format_cents(int(row["price_cents"])),
        "stock": stock,
        "orderable": available and stock > 0,
        "unavailable": not available,
        "low": available and 0 < stock < 5,
        "in_cart": cart.get(int(row["id"]), 0),
    }


def _html(
    request: Request,
    template_name: str,
    session: Session,
    token: str | None,
    user: dict[str, str] | None,
    flash: str | None,
    *,
    status_code: int = 200,
    **extra: object,
) -> Response:
    context = {
        "csrf_token": session.csrf_token,
        "user": user,
        "cart_count": sum(session.cart.values()),
        "flash": flash,
        "error": extra.pop("error", ""),
        "active": extra.pop("active", ""),
        "title": extra.pop("title", "FoodApp"),
        **extra,
    }
    response = templates.TemplateResponse(request, template_name, context, status_code=status_code)
    if token:
        active = _settings(request)
        _set_cookie(response, token, active, active.anon_session_ttl)
    return response


def _redirect(
    location: str,
    settings: Settings,
    token: str | None,
    clear: bool = False,
    max_age: int | None = None,
) -> RedirectResponse:
    response = RedirectResponse(location, status_code=303)
    if clear:
        response.delete_cookie(
            "session",
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
    elif token:
        _set_cookie(response, token, settings, settings.anon_session_ttl if max_age is None else max_age)
    return response


def _set_cookie(response: Response, token: str, settings: Settings, max_age: int) -> None:
    response.set_cookie(
        "session",
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _error_page(request: Request, status: int, heading: str, message: str) -> Response:
    settings = getattr(request.app.state, "settings", None)
    try:
        if settings is None:
            raise RuntimeError("settings unavailable")
        with open_db(settings.db_path) as conn:
            session = load_existing(conn, request.cookies.get("session"), settings)
            user = get_user(conn, session.user_id) if session is not None else None
            cart_count = sum(session.cart.values()) if session is not None else 0
            csrf = session.csrf_token if session is not None else ""
        response = templates.TemplateResponse(
            request,
            "error.html",
            {
                "csrf_token": csrf,
                "user": user,
                "cart_count": cart_count,
                "flash": None,
                "error": "",
                "active": "",
                "title": heading,
                "heading": heading,
                "message": message,
            },
            status_code=status,
        )
        return response
    except Exception:
        logger.exception("error page failed")
        return HTMLResponse(_fallback_page(heading, message), status_code=status)
