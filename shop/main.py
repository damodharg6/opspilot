"""Shop service: a small car-parts store that OpsPilot monitors and debugs."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import resource
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from common.context import request_id_var
from common.logging import configure_logging
from shop.db import Base, Database
from shop.faults import FAULT_NAMES, FaultRegistry
from shop.models import Order, Product
from shop.pricing import calculate_total, calculate_total_v2
from shop.settings import ShopSettings, get_settings

log = logging.getLogger("shop")

SEED_PRODUCTS = [
    ("Engine Oil 5W-30", Decimal("650.00"), 500),
    ("Brake Pads (Front)", Decimal("1800.00"), 300),
    ("Air Filter", Decimal("450.00"), 500),
    ("Spark Plug Set", Decimal("1200.00"), 300),
]


# --- schemas -------------------------------------------------------------------------
class OrderIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0, le=50)


class OrderOut(BaseModel):
    id: int
    product_id: int
    quantity: int
    total: float


class FaultIn(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


def _order_out(order: Order) -> OrderOut:
    return OrderOut(
        id=order.id, product_id=order.product_id, quantity=order.quantity, total=float(order.total)
    )


def current_rss_mb() -> float:
    """Resident memory right now (Linux /proc); falls back to peak RSS elsewhere."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576, 1)
    except (OSError, ValueError, IndexError):
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1_048_576 if sys.platform == "darwin" else 1024
        return round(peak / divisor, 1)


# --- app factory -------------------------------------------------------------------
def create_app(settings: ShopSettings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging("shop", settings.log_level, settings.log_file)
    db = Database(settings.database_url, settings.bad_database_url)
    faults = FaultRegistry()

    async def emit_runtime_stats() -> None:
        while True:
            log.info(
                "runtime stats",
                extra={
                    "event": "runtime_stats",
                    "rss_mb": current_rss_mb(),
                    "leak_mb": faults.leak_mb(),
                },
            )
            await asyncio.sleep(settings.stats_interval_s)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Demo service: create tables on start. OpsPilot's own schema uses Alembic.
        Base.metadata.create_all(db.engine)
        if settings.seed_data:
            with Session(db.engine) as session:
                if session.scalar(select(Product.id).limit(1)) is None:
                    session.add_all(Product(name=n, price=p, stock=s) for n, p, s in SEED_PRODUCTS)
                    session.commit()
        task = asyncio.create_task(emit_runtime_stats())
        log.info("shop started", extra={"event": "startup"})
        yield
        task.cancel()

    app = FastAPI(title="Shop (OpsPilot demo target)", version="1.4.0", lifespan=lifespan)
    app.state.faults = faults
    app.state.db = db

    # --- middleware: request id, one log line per request, safe 500s ------------------
    @app.middleware("http")
    async def request_logging(request: Request, call_next: Callable[..., Any]) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        error_type: str | None = None
        try:
            faults.on_request()
            response = await call_next(request)
        except Exception as exc:
            error_type = type(exc).__name__
            log.exception("unhandled error", extra={"event": "unhandled_error"})
            response = JSONResponse(
                status_code=500, content={"detail": "internal server error", "request_id": rid}
            )
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)  # template keeps cardinality low
        status = response.status_code
        level = (
            logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
        )
        log.log(
            level,
            f"{request.method} {path} {status}",
            extra={
                "event": "http_request",
                "method": request.method,
                "path": path,
                "status": status,
                "latency_ms": latency_ms,
                "error_type": error_type or getattr(request.state, "error_type", None),
            },
        )
        response.headers["X-Request-ID"] = rid
        request_id_var.reset(token)
        return response

    # --- dependencies -----------------------------------------------------------------
    def get_session() -> Iterator[Session]:
        yield from db.session(broken=faults.is_on("bad_config"))

    def get_slow_session(session: Session = Depends(get_session)) -> Session:  # noqa: B008
        if (delay := faults.db_delay_s()) > 0:
            if session.bind.dialect.name == "postgresql":  # type: ignore[union-attr]
                session.execute(text("SELECT pg_sleep(:s)"), {"s": delay})
            else:
                time.sleep(delay)
        return session

    def require_admin(x_admin_token: str = Header(default="")) -> None:
        expected = settings.admin_token.get_secret_value()
        if not expected or not hmac.compare_digest(x_admin_token, expected):
            raise HTTPException(status_code=401, detail="invalid admin token")

    # --- public API ---------------------------------------------------------------------
    @app.get("/health")
    def health(session: Session = Depends(get_session)) -> JSONResponse:  # noqa: B008
        try:
            session.execute(text("SELECT 1"))
        except OperationalError as exc:
            log.error(
                "database unreachable",
                extra={"event": "health_check", "error_type": type(exc).__name__},
            )
            return JSONResponse(status_code=503, content={"status": "unhealthy", "db": "down"})
        return JSONResponse(content={"status": "ok", "db": "ok"})

    @app.get("/products")
    def list_products(session: Session = Depends(get_slow_session)) -> list[dict[str, Any]]:  # noqa: B008
        rows = session.scalars(select(Product).order_by(Product.id)).all()
        return [
            {"id": p.id, "name": p.name, "price": float(p.price), "stock": p.stock} for p in rows
        ]

    @app.post("/orders", response_model=OrderOut, status_code=201)
    def create_order(
        body: OrderIn,
        request: Request,
        session: Session = Depends(get_slow_session),  # noqa: B008
    ) -> OrderOut:
        product = session.get(Product, body.product_id, with_for_update=True)
        if product is None:
            raise HTTPException(404, "product not found")
        if product.stock < body.quantity:
            request.state.error_type = "InsufficientStock"
            raise HTTPException(409, "insufficient stock")
        if faults.is_on("buggy_deploy"):
            total = calculate_total_v2(product.id, product.price, body.quantity)
        else:
            total = calculate_total(product.price, body.quantity)
        product.stock -= body.quantity
        order = Order(product_id=product.id, quantity=body.quantity, total=total)
        session.add(order)
        session.commit()
        return _order_out(order)

    @app.get("/orders", response_model=list[OrderOut])
    def list_orders(
        limit: int = Query(20, ge=1, le=100),
        session: Session = Depends(get_slow_session),  # noqa: B008
    ) -> list[OrderOut]:
        rows = session.scalars(
            select(Order).order_by(Order.created_at.desc(), Order.id.desc()).limit(limit)
        ).all()
        return [_order_out(o) for o in rows]

    @app.get("/orders/{order_id}", response_model=OrderOut)
    def get_order(order_id: int, session: Session = Depends(get_slow_session)) -> OrderOut:  # noqa: B008
        order = session.get(Order, order_id)
        if order is None:
            raise HTTPException(404, "order not found")
        return _order_out(order)

    # --- internal / admin API (token protected) -----------------------------------------
    @app.get("/internal/stats", dependencies=[Depends(require_admin)])
    def internal_stats() -> dict[str, Any]:
        return {
            "rss_mb": current_rss_mb(),
            "leak_mb": faults.leak_mb(),
            "faults": sorted(faults.active()),
        }

    @app.get("/admin/faults", dependencies=[Depends(require_admin)])
    def list_faults() -> dict[str, Any]:
        return {
            "available": list(FAULT_NAMES),
            "active": {
                n: {"params": s.params, "enabled_at": s.enabled_at.isoformat()}
                for n, s in faults.active().items()
            },
        }

    @app.post("/admin/faults/{name}", dependencies=[Depends(require_admin)])
    def enable_fault(name: str, body: FaultIn | None = None) -> dict[str, Any]:
        if name not in FAULT_NAMES:
            raise HTTPException(404, f"unknown fault: {name}")
        if settings.env == "production" and not settings.allow_faults:
            raise HTTPException(403, "faults are disabled in production")
        state = faults.enable(name, body.params if body else None)
        if name == "stock_exhaustion":
            pid = int(state.params["product_id"])
            with Session(db.engine) as session:
                product = session.get(Product, pid)
                if product is None:
                    faults.disable(name)
                    raise HTTPException(404, f"product {pid} not found")
                faults.saved_stock.setdefault(pid, product.stock)
                product.stock = 0
                session.commit()
        log.warning(f"fault enabled: {name}", extra={"event": "fault_enabled", "fault": name})
        return {"enabled": name, "params": state.params}

    @app.delete("/admin/faults/{name}", dependencies=[Depends(require_admin)])
    def disable_fault(name: str) -> dict[str, Any]:
        if name not in FAULT_NAMES:
            raise HTTPException(404, f"unknown fault: {name}")
        state = faults.disable(name)
        if name == "stock_exhaustion" and state is not None:
            pid = int(state.params["product_id"])
            if (saved := faults.saved_stock.pop(pid, None)) is not None:
                with Session(db.engine) as session:
                    session.execute(update(Product).where(Product.id == pid).values(stock=saved))
                    session.commit()
        log.warning(f"fault disabled: {name}", extra={"event": "fault_disabled", "fault": name})
        return {"disabled": name, "was_active": state is not None}

    return app
