"""The FastAPI app (#161, ADR-037): create_app(settings) builds the routes SERVING_ROUTES names."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import date as calendar_date

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from serving import forecasts, model_store
from serving.config import ROUTES, ConfigError, Settings
from serving.contract import Contract, cross_field_errors, feature_set_errors

log = logging.getLogger("serving")
# YYYY-MM-DD only. [0-9], not \d: \d matches any Unicode digit. date.fromisoformat alone also takes ISO week
# dates and the basic form, so the pattern comes first and the round trip below holds the rest.
FORECAST_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _configure_logging() -> None:
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


class Pages(StaticFiles):
    """The static page, for GET and HEAD only: any other method on a path no route serves is a 404, as in
    the model service, where nothing is mounted."""

    async def get_response(self, path, scope):
        if scope["method"] not in ("GET", "HEAD"):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)


class NotJson(ValueError):
    """A constant Python's json accepts that JSON does not have."""


def _reject_constant(name: str):
    raise NotJson(f"{name} is not allowed: it is not a JSON value")


def _unprocessable(errors: list[dict]) -> JSONResponse:
    return JSONResponse({"detail": errors}, status_code=422)


def create_app(settings: Settings, session_factory=forecasts.default_session) -> FastAPI:
    """session_factory makes the authorized session a gs:// FORECASTS_ROOT is read with, once, on first use."""
    if settings.routes not in ROUTES:
        raise ConfigError(f"SERVING_ROUTES is {settings.routes!r}; set it to one of {', '.join(ROUTES)}")
    _configure_logging()
    contract = Contract(settings.schemas_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.model = None
        app.state.reason = "the model is not loaded"
        if settings.serves_model:
            try:
                app.state.model = model_store.load_model(settings.model_dir, settings.model_version,
                                                         contract.run_id_pattern, contract.model_version_pattern)
            except model_store.ModelUnavailable as exc:
                app.state.reason = str(exc)
                log.error(json.dumps({"event": "model_not_loaded", "reason": app.state.reason}))
        app.state.session = None
        yield

    app = FastAPI(title="Boston Resilience Twin model service", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.get("/health")
    def health(request: Request):
        if not settings.serves_model:
            return {"status": "ok"}
        model = request.app.state.model
        if model is None:
            return JSONResponse({"status": "unavailable", "reason": request.app.state.reason}, status_code=503)
        return {"status": "ok", "model_version": model.model_version, "model_run_id": model.run_id}

    if settings.serves_model:
        @app.post("/predict")
        async def predict(request: Request):
            started = time.perf_counter()
            body = await request.body()
            status, payload, rows = await run_in_threadpool(_predict, request.app.state.model, body)
            log.info(json.dumps({"event": "predict", "status": status, "rows": rows,
                                 "model_version": settings.model_version,
                                 "latency_ms": round((time.perf_counter() - started) * 1000, 2)}))
            return JSONResponse(payload, status_code=status)

    def _predict(model, raw: bytes):
        """(status, payload, row count) for one request body."""
        if model is None:
            return 503, {"detail": "no model is loaded"}, 0
        try:
            body = json.loads(raw, parse_constant=_reject_constant)
        except NotJson as exc:
            return 422, {"detail": [{"path": "", "message": str(exc)}]}, 0
        except ValueError:
            return 422, {"detail": [{"path": "", "message": "the body is not valid JSON"}]}, 0
        errors = contract.schema_errors(body)
        if errors:
            return 422, {"detail": errors}, 0
        rows = body["rows"]
        errors = cross_field_errors(body) or feature_set_errors(rows, model.features)
        if errors:
            return 422, {"detail": errors}, len(rows)
        frame = pd.DataFrame({name: pd.Series([row[name] for row in rows], dtype=contract.dtypes[name])
                              for name in model.features}, columns=list(model.features))
        probability = model_store.probabilities(model, frame)
        predictions = [{"unit": row["unit"], "date": row["forecast_date"], "probability": float(p),
                        "prediction": int(p >= model.threshold), "threshold": model.threshold,
                        "model_version": model.model_version, "model_run_id": model.run_id,
                        "data_version": body["data_version"]}
                       for row, p in zip(rows, probability, strict=True)]
        return 200, {"contract": "predict/v1", "predictions": predictions}, len(rows)

    if settings.serves_web:
        @app.get("/forecasts/{date}")
        def forecast(date: str, request: Request):
            try:
                if not FORECAST_DATE.fullmatch(date) or calendar_date.fromisoformat(date).isoformat() != date:
                    raise ValueError
            except ValueError:
                return _unprocessable([{"path": "/date", "message": f"{date!r} is not a date as YYYY-MM-DD"}])
            state = request.app.state
            if settings.forecasts_root.startswith("gs://") and state.session is None:
                try:
                    state.session = session_factory()
                except Exception as exc:  # credentials that cannot be found, and the like
                    return JSONResponse({"detail": f"cannot read forecasts: {type(exc).__name__}"}, status_code=502)
            try:
                rows = forecasts.records(forecasts.fetch(settings.forecasts_root, date, state.session))
            except forecasts.ForecastNotFound as exc:
                return JSONResponse({"detail": str(exc)}, status_code=404)
            except forecasts.ForecastUnreadable as exc:
                return JSONResponse({"detail": str(exc)}, status_code=500)
            except forecasts.ForecastStorageError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=502)
            except Exception as exc:  # a bug: a 500, with the type in the log
                log.error(json.dumps({"event": "forecast_error", "exception": type(exc).__name__}))
                return JSONResponse({"detail": "internal error"}, status_code=500)
            return rows

        # Last, so the page shadows no other route. With no index.html, / is a 404.
        if settings.frontend_dir.is_dir():
            app.mount("/", Pages(directory=settings.frontend_dir, html=True), name="frontend")

    return app
