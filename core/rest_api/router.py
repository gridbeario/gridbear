"""REST API router — generic CRUD endpoints for all ORM models."""

import dataclasses
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from core.api_schemas import ApiResponse, api_error, api_ok
from core.orm.exceptions import IntegrityError, ValidationError
from core.orm.registry import Registry
from core.rest_api.acl import check_access, is_enabled, is_model_visible
from core.rest_api.auth import require_api_auth
from core.rest_api.serializer import serialize_record, serialize_value

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_model(model_path: str):
    """Parse '{schema}.{model}' and return the ORM model class or raise."""
    if "." not in model_path:
        raise HTTPException(
            status_code=400, detail="Model path must be '{schema}.{model}'"
        )
    schema, name = model_path.split(".", 1)
    model_cls = Registry.get_model(schema, name)
    if model_cls is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_path}' not found")
    return model_cls, schema, name


def _require_access(model_key: str, operation: str) -> None:
    """Raise 403 if the operation is not allowed on the model."""
    if not check_access(model_key, operation):
        raise HTTPException(
            status_code=403, detail=f"Access denied: {operation} on {model_key}"
        )


def _record_to_dict(record) -> dict:
    """Convert a record (dict or dataclass) to a plain dict."""
    if record is None:
        return {}
    if isinstance(record, dict):
        return record
    if dataclasses.is_dataclass(record) and not isinstance(record, type):
        return dataclasses.asdict(record)
    return dict(record)


def _field_type_name(field) -> str:
    """Get a lowercase type name from a Field instance."""
    return type(field).__name__.lower()


def _field_info(field) -> dict:
    """Serialize a Field's metadata for the fields endpoint."""
    info = {"type": _field_type_name(field)}
    if field.required:
        info["required"] = True
    if field.unique:
        info["unique"] = True
    if field.index:
        info["index"] = True
    if field.default is not None:
        info["default"] = serialize_value(field.default)
    # Check for auto_now / auto_now_add (DateTime fields)
    if getattr(field, "auto_now_add", False):
        info["auto_now_add"] = True
    if getattr(field, "auto_now", False):
        info["auto_now"] = True
    return info


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/models",
    response_model=ApiResponse[list[dict]],
    response_model_exclude_none=True,
)
async def list_models(_auth: dict = Depends(require_api_auth)):
    """List all ORM models the caller can see."""
    if not is_enabled():
        return api_error(403, "REST API is disabled", "forbidden")

    result = []
    for model_cls in Registry.get_visible_models():
        key = f"{model_cls._schema}.{model_cls._name}"
        if is_model_visible(key):
            result.append(
                {
                    "schema": model_cls._schema,
                    "model": model_cls._name,
                    "primary_key": model_cls._primary_key,
                }
            )
    return api_ok(data=result)


@router.get(
    "/models/{model_path}/fields",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def get_fields(model_path: str, _auth: dict = Depends(require_api_auth)):
    """Return field definitions for a model."""
    if not is_enabled():
        return api_error(403, "REST API is disabled", "forbidden")

    model_cls, schema, name = _resolve_model(model_path)
    _require_access(model_path, "read")

    fields = {}
    for fname, field in model_cls._fields.items():
        fields[fname] = _field_info(field)

    return api_ok(data=fields)


@router.post(
    "/models/{model_path}/search",
    response_model=ApiResponse[list[dict]],
    response_model_exclude_none=True,
)
async def search_records(
    model_path: str, request: Request, _auth: dict = Depends(require_api_auth)
):
    """Search records using an Odoo-style domain expression."""
    if not is_enabled():
        return api_error(403, "REST API is disabled", "forbidden")

    model_cls, schema, name = _resolve_model(model_path)
    _require_access(model_path, "read")

    body = await request.json() if await request.body() else {}
    domain = body.get("domain", [])
    fields_filter = body.get("fields")
    order = body.get("order", "")
    limit = body.get("limit", 0)
    offset = body.get("offset", 0)

    try:
        records = await model_cls.search(
            domain=domain,
            order=order,
            limit=limit,
            offset=offset,
        )
        total = await model_cls.count(domain=domain)
    except Exception:
        logger.exception("Search error on %s", model_path)
        return api_error(500, "An internal error occurred", "internal_error")

    pk = model_cls._primary_key
    data = []
    for rec in records:
        d = _record_to_dict(rec)
        # Ensure PK is always present
        if fields_filter and pk not in fields_filter:
            fields_with_pk = [pk] + list(fields_filter)
        else:
            fields_with_pk = fields_filter
        data.append(serialize_record(d, fields_with_pk))

    return api_ok(data=data, count=total)


@router.get(
    "/models/{model_path}/{record_id}",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def read_record(
    model_path: str,
    record_id: str,
    request: Request,
    _auth: dict = Depends(require_api_auth),
):
    """Read a single record by primary key."""
    if not is_enabled():
        return api_error(403, "REST API is disabled", "forbidden")

    model_cls, schema, name = _resolve_model(model_path)
    _require_access(model_path, "read")

    # Parse PK value (try int first for serial PKs)
    pk_value = _parse_pk(record_id)
    fields_filter = request.query_params.get("fields")
    fields_list = (
        [f.strip() for f in fields_filter.split(",")] if fields_filter else None
    )

    try:
        record = await model_cls.get(**{model_cls._primary_key: pk_value})
    except Exception:
        logger.exception("Read error on %s/%s", model_path, record_id)
        return api_error(500, "An internal error occurred", "internal_error")

    if record is None:
        return api_error(404, f"Record {record_id} not found", "not_found")

    data = serialize_record(_record_to_dict(record), fields_list)
    return api_ok(data=data)


@router.post(
    "/models/{model_path}",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
    status_code=201,
)
async def create_record(
    model_path: str, request: Request, _auth: dict = Depends(require_api_auth)
):
    """Create a new record."""
    if not is_enabled():
        return api_error(403, "REST API is disabled", "forbidden")

    model_cls, schema, name = _resolve_model(model_path)
    _require_access(model_path, "create")

    body = await request.json()
    values = body.get("values", {})
    if not values:
        return api_error(422, "No values provided", "validation_error")

    try:
        record = await model_cls.create(**values)
    except ValidationError:
        return api_error(422, "Invalid field values", "validation_error")
    except IntegrityError:
        return api_error(409, "Record conflicts with existing data", "conflict")
    except Exception:
        logger.exception("Create error on %s", model_path)
        return api_error(500, "An internal error occurred", "internal_error")

    data = serialize_record(_record_to_dict(record))
    return api_ok(data=data)


@router.put(
    "/models/{model_path}/{record_id}",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def update_record(
    model_path: str,
    record_id: str,
    request: Request,
    _auth: dict = Depends(require_api_auth),
):
    """Update a record by primary key."""
    if not is_enabled():
        return api_error(403, "REST API is disabled", "forbidden")

    model_cls, schema, name = _resolve_model(model_path)
    _require_access(model_path, "write")

    pk_value = _parse_pk(record_id)
    body = await request.json()
    values = body.get("values", {})
    if not values:
        return api_error(422, "No values provided", "validation_error")

    try:
        rows_updated = await model_cls.write(pk_value, **values)
    except ValidationError:
        return api_error(422, "Invalid field values", "validation_error")
    except IntegrityError:
        return api_error(409, "Record conflicts with existing data", "conflict")
    except Exception:
        logger.exception("Update error on %s/%s", model_path, record_id)
        return api_error(500, "An internal error occurred", "internal_error")

    if rows_updated == 0:
        return api_error(404, f"Record {record_id} not found", "not_found")

    # Re-read the updated record
    record = await model_cls.get(**{model_cls._primary_key: pk_value})
    data = serialize_record(_record_to_dict(record))
    return api_ok(data=data)


@router.delete(
    "/models/{model_path}/{record_id}",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def delete_record(
    model_path: str, record_id: str, _auth: dict = Depends(require_api_auth)
):
    """Delete a record by primary key."""
    if not is_enabled():
        return api_error(403, "REST API is disabled", "forbidden")

    model_cls, schema, name = _resolve_model(model_path)
    _require_access(model_path, "delete")

    pk_value = _parse_pk(record_id)

    try:
        rows_deleted = await model_cls.delete(pk_value)
    except IntegrityError:
        return api_error(409, "Record conflicts with existing data", "conflict")
    except Exception:
        logger.exception("Delete error on %s/%s", model_path, record_id)
        return api_error(500, "An internal error occurred", "internal_error")

    if rows_deleted == 0:
        return api_error(404, f"Record {record_id} not found", "not_found")

    return api_ok(data={"deleted": rows_deleted})


@router.post(
    "/models/{model_path}/count",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def count_records(
    model_path: str, request: Request, _auth: dict = Depends(require_api_auth)
):
    """Count records matching a domain."""
    if not is_enabled():
        return api_error(403, "REST API is disabled", "forbidden")

    model_cls, schema, name = _resolve_model(model_path)
    _require_access(model_path, "read")

    body = await request.json() if await request.body() else {}
    domain = body.get("domain", [])

    try:
        total = await model_cls.count(domain=domain)
    except Exception:
        logger.exception("Count error on %s", model_path)
        return api_error(500, "An internal error occurred", "internal_error")

    return api_ok(count=total)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_pk(raw: str):
    """Try to parse a PK value as int, fall back to string."""
    try:
        return int(raw)
    except ValueError:
        return raw
